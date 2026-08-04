"""设备注册表模板投影（Registry Template Projection）的 SQLite 适配器。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from unilabos.workflow.store import StoreConflict, WorkflowStore, utc_now


class TemplateProjectionIdentityConflict(StoreConflict):
    """模板 UUID、活动业务唯一键或父子身份发生冲突。"""


@dataclass(frozen=True, slots=True)
class RegistryTemplateProjectionGeneration:
    """一个已原子提交的设备注册表模板投影代际。

    字段说明：``authority_id`` 标识投影权威（Authority）；``generation`` 是本地
    单调代际号；``node_templates`` 与 ``handle_templates`` 是活动节点和连接点
    （Handle）模板全集；``resource_template_symbols`` 是资源模板
    （ResourceTemplate）源码身份到 UUID 的完整映射。
    """

    authority_id: str
    generation: int
    node_templates: list[dict[str, Any]]
    handle_templates: list[dict[str, Any]]
    resource_template_symbols: dict[str, str]


class RegistryTemplateProjectionStore:
    """把完整设备注册表模板代际原子写入现有工作流模板表。"""

    def __init__(self, workflow_store: WorkflowStore) -> None:
        """绑定本地工作流存储。

        参数说明：``workflow_store`` 持有唯一 SQLite 连接和写事务；本适配器不再
        创建第二个数据库，也不自行推导数据库路径。返回：无；构造时只确保 OS
        私有投影代际表存在。异常：SQLite 初始化失败原样传播并关闭式失败。
        """

        self._workflow_store = workflow_store
        self._initialize_schema()

    def replace(
        self,
        *,
        authority_id: str,
        node_templates: Sequence[Mapping[str, Any]],
        handle_templates: Sequence[Mapping[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """原子替换一个权威（Authority）的完整活动模板投影。

        参数说明：``authority_id`` 是投影来源；``node_templates`` 和
        ``handle_templates`` 是一次完整刷新。返回：已解析稳定 UUID 且不含
        时间戳的节点与连接点（Handle）语义实体；本兼容接口会发布空资源模板
        （ResourceTemplate）源码身份映射。异常：参数非法抛出 ``ValueError``，
        模板身份冲突抛出 ``TemplateProjectionIdentityConflict``，事务整体回滚。
        """

        committed = self.replace_generation(
            authority_id=authority_id,
            node_templates=node_templates,
            handle_templates=handle_templates,
            resource_template_symbols={},
        )
        return committed.node_templates, committed.handle_templates

    def replace_generation(
        self,
        *,
        authority_id: str,
        node_templates: Sequence[Mapping[str, Any]],
        handle_templates: Sequence[Mapping[str, Any]],
        resource_template_symbols: Mapping[str, str],
    ) -> RegistryTemplateProjectionGeneration:
        """原子替换一个权威（Authority）的完整模板与资源身份代际。

        参数说明：``authority_id`` 是投影来源；``node_templates`` 与
        ``handle_templates`` 是本轮完整节点和连接点（Handle）集合；
        ``resource_template_symbols`` 是资源模板（ResourceTemplate）源码身份到
        UUID 的完整映射。返回：同一 SQLite 事务提交后重新读取的完整投影代际。
        异常：空权威、非法资源身份映射抛出 ``ValueError``；模板身份冲突抛出
        ``TemplateProjectionIdentityConflict``，整个事务回滚且旧代际保持不变。
        """

        if not isinstance(authority_id, str) or not authority_id.strip():
            raise ValueError("模板投影权威不能为空")
        # ``node_candidates`` 与 ``handle_candidates`` 是脱离调用方容器的候选代际。
        node_candidates = [dict(candidate) for candidate in node_templates]
        handle_candidates = [dict(candidate) for candidate in handle_templates]
        symbol_candidates = _resource_template_symbol_mapping(resource_template_symbols)
        now = utc_now()
        with self._workflow_store.transaction() as connection:
            # ``node_identities`` 把本轮活动业务唯一键映射到持久 UUID。
            node_identities: dict[tuple[str, str], str] = {}
            active_node_uuids: list[str] = []
            for candidate in node_candidates:
                business_key = self._node_business_key(candidate)
                if business_key in node_identities:
                    raise TemplateProjectionIdentityConflict(
                        "完整模板投影包含重复节点活动业务唯一键"
                    )
                template_uuid = self._upsert_node(
                    connection,
                    authority_id=authority_id,
                    candidate=candidate,
                    business_key=business_key,
                    now=now,
                )
                node_identities[business_key] = template_uuid
                active_node_uuids.append(template_uuid)

            active_handle_uuids: list[str] = []
            seen_handle_keys: set[tuple[str, str, str]] = set()
            for candidate in handle_candidates:
                parent_key = self._handle_parent_key(candidate)
                try:
                    parent_uuid = node_identities[parent_key]
                except KeyError:
                    raise TemplateProjectionIdentityConflict(
                        "句柄模板引用了本轮投影之外的节点模板"
                    ) from None
                business_key = self._handle_business_key(candidate, parent_uuid)
                if business_key in seen_handle_keys:
                    raise TemplateProjectionIdentityConflict(
                        "完整模板投影包含重复句柄活动业务唯一键"
                    )
                seen_handle_keys.add(business_key)
                active_handle_uuids.append(
                    self._upsert_handle(
                        connection,
                        authority_id=authority_id,
                        candidate=candidate,
                        parent_uuid=parent_uuid,
                        business_key=business_key,
                        now=now,
                    )
                )

            self._soft_delete_omitted(
                connection,
                authority_id=authority_id,
                active_node_uuids=active_node_uuids,
                active_handle_uuids=active_handle_uuids,
                now=now,
            )
            generation = self._upsert_generation(
                connection,
                authority_id=authority_id,
                resource_template_symbols=symbol_candidates,
                now=now,
            )
            nodes, handles = self._load(connection, authority_id=authority_id)
            return RegistryTemplateProjectionGeneration(
                authority_id=authority_id,
                generation=generation,
                node_templates=nodes,
                handle_templates=handles,
                resource_template_symbols=dict(symbol_candidates),
            )

    def load(
        self,
        *,
        authority_id: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """读取一个权威当前已提交的完整活动投影。

        参数说明：``authority_id`` 用于隔离来源。返回：当前节点与连接点
        （Handle）活动投影，不访问设备注册表（Registry）或网络。异常：空权威
        抛出 ``ValueError``；持久资源身份损坏抛出
        ``TemplateProjectionIdentityConflict``，不返回部分数据。
        """

        generation = self.load_generation(authority_id=authority_id)
        return generation.node_templates, generation.handle_templates

    def load_generation(
        self,
        *,
        authority_id: str,
    ) -> RegistryTemplateProjectionGeneration:
        """读取一个权威当前提交的完整设备注册表模板投影代际。

        参数说明：``authority_id`` 隔离不同投影来源。返回：在同一数据库视图中
        读取的节点、连接点（Handle）、资源模板（ResourceTemplate）源码身份映射
        和单调代际号；尚未发布时返回代际 ``0`` 与三个空集合。异常：持久映射被
        破坏时抛出 ``TemplateProjectionIdentityConflict``，不返回部分快照。
        """

        if not isinstance(authority_id, str) or not authority_id.strip():
            raise ValueError("模板投影权威不能为空")
        with self._workflow_store.transaction() as connection:
            nodes, handles = self._load(connection, authority_id=authority_id)
            row = connection.execute(
                """
                SELECT generation, resource_template_symbols
                FROM registry_template_projection_generation
                WHERE authority_id = ?
                """,
                (authority_id,),
            ).fetchone()
            if row is None:
                generation = 0
                symbols: dict[str, str] = {}
            else:
                generation = int(row["generation"])
                try:
                    raw_symbols = json.loads(row["resource_template_symbols"])
                    symbols = _resource_template_symbol_mapping(raw_symbols)
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    raise TemplateProjectionIdentityConflict(
                        "资源模板源码身份投影已损坏"
                    ) from error
            return RegistryTemplateProjectionGeneration(
                authority_id=authority_id,
                generation=generation,
                node_templates=nodes,
                handle_templates=handles,
                resource_template_symbols=symbols,
            )

    def _initialize_schema(self) -> None:
        """建立设备侧私有的设备注册表模板投影代际表。

        参数：无。返回：无；表只保存 OS 本地投影权威、代际与资源模板
        （ResourceTemplate）源码身份映射，不改变后端（Backend）共享逻辑表。
        异常：SQLite 建表失败原样传播，调用方不得继续使用不完整投影。
        """

        with self._workflow_store.transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS registry_template_projection_generation (
                    authority_id TEXT PRIMARY KEY,
                    generation INTEGER NOT NULL CHECK(generation >= 1),
                    resource_template_symbols TEXT NOT NULL,
                    create_time TEXT NOT NULL,
                    update_time TEXT NOT NULL
                )
                """
            )

    @staticmethod
    def _upsert_generation(
        connection: Any,
        *,
        authority_id: str,
        resource_template_symbols: Mapping[str, str],
        now: str,
    ) -> int:
        """在模板替换事务内推进并保存完整资源身份代际。

        参数说明：``connection`` 是唯一写事务；``authority_id`` 是投影来源；
        ``resource_template_symbols`` 是已经规范化的完整资源模板
        （ResourceTemplate）源码身份映射；``now`` 是统一事务时间。返回：首次为
        ``1``、以后单调递增的代际号。异常：SQLite 写入冲突原样传播并回滚整个
        模板替换事务。
        """

        row = connection.execute(
            """
            SELECT generation
            FROM registry_template_projection_generation
            WHERE authority_id = ?
            """,
            (authority_id,),
        ).fetchone()
        next_generation = 1 if row is None else int(row["generation"]) + 1
        connection.execute(
            """
            INSERT INTO registry_template_projection_generation(
                authority_id, generation, resource_template_symbols,
                create_time, update_time
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(authority_id) DO UPDATE SET
                generation = excluded.generation,
                resource_template_symbols = excluded.resource_template_symbols,
                update_time = excluded.update_time
            """,
            (
                authority_id,
                next_generation,
                _json(resource_template_symbols),
                now,
                now,
            ),
        )
        return next_generation

    def close(self) -> None:
        """关闭本适配器拥有的工作流存储连接。"""

        self._workflow_store.close()

    @staticmethod
    def _node_business_key(candidate: Mapping[str, Any]) -> tuple[str, str]:
        """取得节点模板的大小写敏感活动业务唯一键。

        参数说明：``candidate`` 是节点模板候选；返回值严格采用后端（Backend）
        当前规范 ``(resource_template_uuid, name)``，不做 trim 或大小写折叠。
        """

        resource_template_uuid = candidate.get("resource_template_uuid")
        name = candidate.get("name")
        if not isinstance(resource_template_uuid, str) or not resource_template_uuid:
            raise TemplateProjectionIdentityConflict("节点模板缺少资源模板 UUID")
        if not isinstance(name, str) or not name:
            raise TemplateProjectionIdentityConflict("节点模板缺少动作业务名")
        return resource_template_uuid, name

    @staticmethod
    def _handle_parent_key(candidate: Mapping[str, Any]) -> tuple[str, str]:
        """取得句柄模板候选引用的节点活动业务唯一键。

        参数说明：``candidate`` 是尚未解析父 UUID 的句柄候选；返回值由节点资源
        模板 UUID 和动作业务名组成。
        """

        parent_key = candidate.get("node_business_key")
        if (
            not isinstance(parent_key, (list, tuple))
            or len(parent_key) != 2
            or not all(isinstance(part, str) and part for part in parent_key)
        ):
            raise TemplateProjectionIdentityConflict("句柄模板缺少节点业务身份")
        return str(parent_key[0]), str(parent_key[1])

    @staticmethod
    def _handle_business_key(
        candidate: Mapping[str, Any],
        parent_uuid: str,
    ) -> tuple[str, str, str]:
        """取得句柄模板的大小写敏感活动业务唯一键。

        参数说明：``candidate`` 提供连接点名和方向，``parent_uuid`` 是本轮解析的
        节点模板 UUID；返回后端（Backend）规范三元组。
        """

        handle_key = candidate.get("handle_key")
        io_type = candidate.get("io_type")
        if not isinstance(handle_key, str) or not handle_key:
            raise TemplateProjectionIdentityConflict("句柄模板缺少连接点业务名")
        if io_type not in {"source", "target"}:
            raise TemplateProjectionIdentityConflict(
                "句柄模板方向必须是 source 或 target"
            )
        return parent_uuid, handle_key, str(io_type)

    def _upsert_node(
        self,
        connection: Any,
        *,
        authority_id: str,
        candidate: Mapping[str, Any],
        business_key: tuple[str, str],
        now: str,
    ) -> str:
        """按显式 UUID 或活动业务唯一键写入一个节点模板。

        参数说明：``connection`` 是当前唯一写事务；其余参数描述来源、候选、身份
        和事务时间。返回值是本轮节点模板稳定 UUID。
        """

        explicit_uuid = candidate.get("uuid")
        existing_by_key = connection.execute(
            """
            SELECT * FROM workflow_node_template
            WHERE resource_template_uuid = ? AND name = ? AND deleted_at IS NULL
            """,
            business_key,
        ).fetchone()
        existing_by_uuid = None
        if explicit_uuid is not None:
            if not isinstance(explicit_uuid, str) or not explicit_uuid:
                raise TemplateProjectionIdentityConflict("节点模板显式 UUID 非法")
            existing_by_uuid = connection.execute(
                "SELECT * FROM workflow_node_template WHERE uuid = ?",
                (explicit_uuid,),
            ).fetchone()
            if (
                existing_by_uuid is not None
                and (
                    existing_by_uuid["resource_template_uuid"],
                    existing_by_uuid["name"],
                )
                != business_key
            ):
                raise TemplateProjectionIdentityConflict(
                    "节点模板 UUID 不得改绑业务身份"
                )
            if existing_by_key is not None and existing_by_key["uuid"] != explicit_uuid:
                raise TemplateProjectionIdentityConflict(
                    "节点活动业务唯一键已有其他 UUID"
                )
            template_uuid = explicit_uuid
        elif existing_by_key is not None:
            template_uuid = str(existing_by_key["uuid"])
        else:
            template_uuid = str(uuid4())

        values = self._node_values(candidate)
        existing = existing_by_uuid or existing_by_key
        if existing is None:
            connection.execute(
                """
                INSERT INTO workflow_node_template(
                    uuid, create_time, update_time, deleted_at, description,
                    meta_data, authority_id, resource_template_uuid, name,
                    display_name, class, goal, goal_default, feedback, result,
                    schema, type, icon, header, footer, node_type
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (template_uuid, now, now, *values[:2], authority_id, *values[2:]),
            )
        else:
            connection.execute(
                """
                UPDATE workflow_node_template
                SET update_time = ?, deleted_at = NULL, description = ?, meta_data = ?,
                    authority_id = ?, resource_template_uuid = ?, name = ?,
                    display_name = ?, class = ?, goal = ?, goal_default = ?,
                    feedback = ?, result = ?, schema = ?, type = ?, icon = ?,
                    header = ?, footer = ?, node_type = ?
                WHERE uuid = ?
                """,
                (now, *values[:2], authority_id, *values[2:], template_uuid),
            )
        return template_uuid

    def _upsert_handle(
        self,
        connection: Any,
        *,
        authority_id: str,
        candidate: Mapping[str, Any],
        parent_uuid: str,
        business_key: tuple[str, str, str],
        now: str,
    ) -> str:
        """按显式 UUID 或活动业务唯一键写入一个句柄模板。

        参数说明：``parent_uuid`` 是已解析节点身份；返回值是句柄模板稳定 UUID。
        其他参数与节点写入使用同一事务和身份规则。
        """

        explicit_uuid = candidate.get("uuid")
        existing_by_key = connection.execute(
            """
            SELECT * FROM workflow_handle_template
            WHERE workflow_node_template_uuid = ? AND handle_key = ?
              AND io_type = ? AND deleted_at IS NULL
            """,
            business_key,
        ).fetchone()
        existing_by_uuid = None
        if explicit_uuid is not None:
            if not isinstance(explicit_uuid, str) or not explicit_uuid:
                raise TemplateProjectionIdentityConflict("句柄模板显式 UUID 非法")
            existing_by_uuid = connection.execute(
                "SELECT * FROM workflow_handle_template WHERE uuid = ?",
                (explicit_uuid,),
            ).fetchone()
            if (
                existing_by_uuid is not None
                and (
                    existing_by_uuid["workflow_node_template_uuid"],
                    existing_by_uuid["handle_key"],
                    existing_by_uuid["io_type"],
                )
                != business_key
            ):
                raise TemplateProjectionIdentityConflict(
                    "句柄模板 UUID 不得改绑业务身份"
                )
            if existing_by_key is not None and existing_by_key["uuid"] != explicit_uuid:
                raise TemplateProjectionIdentityConflict(
                    "句柄活动业务唯一键已有其他 UUID"
                )
            handle_uuid = explicit_uuid
        elif existing_by_key is not None:
            handle_uuid = str(existing_by_key["uuid"])
        else:
            handle_uuid = str(uuid4())

        values = self._handle_values(candidate, parent_uuid=parent_uuid)
        existing = existing_by_uuid or existing_by_key
        if existing is None:
            connection.execute(
                """
                INSERT INTO workflow_handle_template(
                    uuid, create_time, update_time, deleted_at, description,
                    meta_data, authority_id, workflow_node_template_uuid,
                    handle_key, io_type, display_name, type, required,
                    data_source, data_key
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (handle_uuid, now, now, *values[:2], authority_id, *values[2:]),
            )
        else:
            connection.execute(
                """
                UPDATE workflow_handle_template
                SET update_time = ?, deleted_at = NULL, description = ?, meta_data = ?,
                    authority_id = ?, workflow_node_template_uuid = ?, handle_key = ?,
                    io_type = ?, display_name = ?, type = ?, required = ?,
                    data_source = ?, data_key = ?
                WHERE uuid = ?
                """,
                (now, *values[:2], authority_id, *values[2:], handle_uuid),
            )
        return handle_uuid

    @staticmethod
    def _node_values(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
        """把节点候选转换为数据库列顺序。

        参数说明：``candidate`` 是已通过身份检查的节点模板；返回值不含 UUID、
        时间和权威字段，JSON 领域字段采用稳定编码。
        """

        return (
            candidate.get("description"),
            _json(candidate.get("meta_data") or {}),
            candidate["resource_template_uuid"],
            candidate["name"],
            candidate.get("display_name") or candidate["name"],
            candidate.get("class"),
            _json(candidate.get("goal") or {}),
            _json(candidate.get("goal_default") or {}),
            _json(candidate.get("feedback") or {}),
            _json(candidate.get("result") or {}),
            _schema_text(candidate.get("schema")),
            candidate.get("type") or "UniLabJsonCommand",
            candidate.get("icon"),
            candidate.get("header"),
            candidate.get("footer"),
            candidate.get("node_type") or "device_action",
        )

    @staticmethod
    def _handle_values(
        candidate: Mapping[str, Any],
        *,
        parent_uuid: str,
    ) -> tuple[Any, ...]:
        """把句柄候选转换为数据库列顺序。

        参数说明：``candidate`` 是句柄模板，``parent_uuid`` 是已解析父节点身份；
        返回值不含 UUID、时间和权威字段。
        """

        return (
            candidate.get("description"),
            _json(candidate.get("meta_data") or {}),
            parent_uuid,
            candidate["handle_key"],
            candidate["io_type"],
            candidate.get("display_name") or candidate["handle_key"],
            candidate.get("type") or "any",
            int(bool(candidate.get("required"))),
            candidate.get("data_source"),
            candidate.get("data_key"),
        )

    @staticmethod
    def _soft_delete_omitted(
        connection: Any,
        *,
        authority_id: str,
        active_node_uuids: Sequence[str],
        active_handle_uuids: Sequence[str],
        now: str,
    ) -> None:
        """软删除完整刷新中被遗漏的同权威模板成员。

        参数说明：两个 UUID 集合是本轮完整活动代；``now`` 是统一事务时间。
        """

        _soft_delete_rows(
            connection,
            table="workflow_handle_template",
            authority_id=authority_id,
            active_uuids=active_handle_uuids,
            now=now,
        )
        _soft_delete_rows(
            connection,
            table="workflow_node_template",
            authority_id=authority_id,
            active_uuids=active_node_uuids,
            now=now,
        )

    @staticmethod
    def _load(
        connection: Any,
        *,
        authority_id: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """在一个数据库视图内装载节点与句柄语义实体。

        参数说明：``connection`` 是同一事务连接，``authority_id`` 限定投影来源；
        返回值排除时间戳，避免目录指纹随刷新时间漂移。
        """

        node_rows = connection.execute(
            """
            SELECT * FROM workflow_node_template
            WHERE authority_id = ? AND deleted_at IS NULL
            ORDER BY uuid
            """,
            (authority_id,),
        ).fetchall()
        handle_rows = connection.execute(
            """
            SELECT * FROM workflow_handle_template
            WHERE authority_id = ? AND deleted_at IS NULL
            ORDER BY uuid
            """,
            (authority_id,),
        ).fetchall()
        nodes = [
            {
                "uuid": row["uuid"],
                "create_time": row["create_time"],
                "update_time": row["update_time"],
                "description": row["description"],
                "meta_data": _load_json(row["meta_data"], {}),
                "resource_template_uuid": row["resource_template_uuid"],
                "name": row["name"],
                "display_name": row["display_name"],
                "class": row["class"],
                "goal": _load_json(row["goal"], {}),
                "goal_default": _load_json(row["goal_default"], {}),
                "feedback": _load_json(row["feedback"], {}),
                "result": _load_json(row["result"], {}),
                "schema": _load_schema(row["schema"]),
                "type": row["type"],
                "icon": row["icon"],
                "header": row["header"],
                "footer": row["footer"],
                "node_type": row["node_type"],
            }
            for row in node_rows
        ]
        handles = [
            {
                "uuid": row["uuid"],
                "create_time": row["create_time"],
                "update_time": row["update_time"],
                "description": row["description"],
                "meta_data": _load_json(row["meta_data"], {}),
                "workflow_node_template_uuid": row["workflow_node_template_uuid"],
                "handle_key": row["handle_key"],
                "io_type": row["io_type"],
                "display_name": row["display_name"],
                "type": row["type"],
                "required": bool(row["required"]),
                "data_source": row["data_source"],
                "data_key": row["data_key"],
            }
            for row in handle_rows
        ]
        return nodes, handles


def _soft_delete_rows(
    connection: Any,
    *,
    table: str,
    authority_id: str,
    active_uuids: Sequence[str],
    now: str,
) -> None:
    """对一个固定模板表执行按完整集合的软删除。

    参数说明：``table`` 只由本模块传入两个常量表名；``active_uuids`` 是保留集合，
    空集合表示该权威成功发布空投影。
    """

    if table not in {"workflow_node_template", "workflow_handle_template"}:
        raise ValueError("不允许的模板投影表")
    if active_uuids:
        placeholders = ",".join("?" for _ in active_uuids)
        connection.execute(
            f"""
            UPDATE {table}
            SET deleted_at = ?, update_time = ?
            WHERE authority_id = ? AND deleted_at IS NULL
              AND uuid NOT IN ({placeholders})
            """,
            (now, now, authority_id, *active_uuids),
        )
    else:
        connection.execute(
            f"""
            UPDATE {table}
            SET deleted_at = ?, update_time = ?
            WHERE authority_id = ? AND deleted_at IS NULL
            """,
            (now, now, authority_id),
        )


def _resource_template_symbol_mapping(raw_mapping: Any) -> dict[str, str]:
    """分离并验证资源模板源码身份映射的持久化形状。

    参数说明：``raw_mapping`` 是编译结果或 SQLite JSON 解码值。返回：按源码身份
    稳定排序的新字典；非对象、空身份、非字符串 UUID 或多个源码身份复用同一
    UUID 时抛出 ``TypeError`` 或 ``ValueError``。本函数只保护持久化结构，
    Python 源码身份与 UUID 的领域规范由工作流创作目录（Authoring Catalog）在
    写入前验证。
    """

    if not isinstance(raw_mapping, Mapping):
        raise TypeError("资源模板源码身份映射必须是对象")
    normalized: dict[str, str] = {}
    seen_uuids: set[str] = set()
    for raw_symbol, raw_uuid in sorted(
        raw_mapping.items(),
        key=lambda item: str(item[0]),
    ):
        if not isinstance(raw_symbol, str) or not raw_symbol:
            raise ValueError("资源模板源码身份不能为空")
        if not isinstance(raw_uuid, str) or not raw_uuid:
            raise ValueError("资源模板源码身份必须映射到 UUID 字符串")
        if raw_uuid in seen_uuids:
            raise ValueError("资源模板 UUID 不得绑定多个源码身份")
        normalized[raw_symbol] = raw_uuid
        seen_uuids.add(raw_uuid)
    return normalized


def _json(value: Any) -> str:
    """把领域 JSON 值编码为稳定文本。

    参数说明：``value`` 是节点或句柄模板字段；返回排序且禁止 NaN 的 JSON。
    """

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _load_json(value: str | None, fallback: Any) -> Any:
    """读取数据库 JSON 文本。

    参数说明：``value`` 是可空文本，``fallback`` 是空值默认；返回解码后的值。
    """

    if value is None or value == "":
        return fallback
    return json.loads(value)


def _schema_text(value: Any) -> str | None:
    """把动作 JSON Schema 转换为数据库文本。

    参数说明：``value`` 可为空、文本或 JSON 对象；返回可持久化文本。
    """

    if value is None or isinstance(value, str):
        return value
    return _json(value)


def _load_schema(value: str | None) -> Any:
    """把数据库 Schema 文本恢复为 JSON 对象或原文本。

    参数说明：``value`` 是可空 Schema 列；合法 JSON 返回解码值，旧普通文本原样
    返回，以维持历史兼容。
    """

    if value is None or value == "":
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


__all__ = [
    "RegistryTemplateProjectionGeneration",
    "RegistryTemplateProjectionStore",
    "TemplateProjectionIdentityConflict",
]
