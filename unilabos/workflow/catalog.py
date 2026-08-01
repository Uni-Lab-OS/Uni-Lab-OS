"""Authority-scoped Workflow template Catalog deep module。

显式 importer 用 :meth:`TemplateCatalog.replace` 发布完整模板 aggregate；compiler
只能在 :meth:`TemplateCatalog.snapshot` guard 内读取不可变快照。该模块不发现
Registry、不访问网络，也不从合同样例合成 Handle。
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal
from uuid import UUID, uuid4

from unilabos.workflow.catalog_keys import (
    normalize_catalog_business_name as _business_name,
)
from unilabos.workflow.json_codec import decode_json_bytes, encode_json
from unilabos.workflow.store import WorkflowStore, utc_now

_NODE_REQUIRED_STRINGS = (
    "resource_template_uuid",
    "name",
    "display_name",
    "type",
    "node_type",
)
_NODE_JSON_OBJECTS = ("meta_data", "goal", "goal_default", "feedback", "result")
_NODE_OPTIONAL_STRINGS = (
    "description",
    "class",
    "icon",
    "header",
    "footer",
)
_NODE_INPUT_FIELDS = {
    "uuid",
    *_NODE_REQUIRED_STRINGS,
    *_NODE_JSON_OBJECTS,
    *_NODE_OPTIONAL_STRINGS,
    "schema",
    "create_time",
    "update_time",
    "deleted_at",
}
_HANDLE_REQUIRED_STRINGS = ("handle_key", "io_type", "display_name", "type")
_HANDLE_OPTIONAL_STRINGS = ("description", "data_source", "data_key")
_HANDLE_INPUT_FIELDS = {
    "uuid",
    "workflow_node_template_uuid",
    *_HANDLE_REQUIRED_STRINGS,
    *_HANDLE_OPTIONAL_STRINGS,
    "meta_data",
    "required",
    "create_time",
    "update_time",
    "deleted_at",
}
_NODE_JSON_COLUMNS = {"meta_data", "goal", "goal_default", "feedback", "result"}
_HANDLE_JSON_COLUMNS = {"meta_data"}


class TemplateCatalogError(RuntimeError):
    """Catalog 稳定 domain error。"""

    code = "template_catalog_mismatch"

    def __init__(self, path: str):
        super().__init__(self.code)
        self.path = path


class TemplateCatalogImportError(TemplateCatalogError, ValueError):
    """完整 import payload 或持久身份不合法。"""


class TemplateCatalogUnavailable(TemplateCatalogError):
    """指定 authority 从未完成一次 Catalog replace。"""

    code = "template_catalog_unavailable"


class TemplateCatalogMismatch(TemplateCatalogError, LookupError):
    """可用 Catalog 内缺少或包含不一致的模板身份。"""


class TemplateCatalogStale(TemplateCatalogError):
    """调用者观察到的 fingerprint 已不是当前 fingerprint。"""

    code = "template_catalog_conflict"


@dataclass(frozen=True)
class CatalogAuthority:
    """一个 Graph Authority 的稳定 Catalog partition。"""

    authority_id: str
    kind: Literal["local", "backend"]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.authority_id, str)
            or not self.authority_id
            or self.authority_id.strip() != self.authority_id
            or self.kind not in ("local", "backend")
        ):
            raise TemplateCatalogImportError("/authority")


class LocalResourceTemplateIdentityResolver:
    """为 local Graph Authority 持久分配 ResourceTemplate UUID。"""

    def __init__(self, store: WorkflowStore, authority: CatalogAuthority) -> None:
        if authority.kind != "local":
            raise TemplateCatalogImportError("/authority/kind")
        self._store = store
        self._authority = authority

    def __call__(self, source_identity: str) -> str:
        if (
            not isinstance(source_identity, str)
            or not source_identity
            or source_identity.strip() != source_identity
        ):
            raise TemplateCatalogImportError("/resource_templates/source_identity")
        with self._store.catalog_guard():
            with self._store.transaction() as conn:
                row = conn.execute(
                    """
                    SELECT resource_template_uuid
                    FROM workflow_resource_template_identity
                    WHERE authority_id = ? AND source_identity = ?
                    """,
                    (self._authority.authority_id, source_identity),
                ).fetchone()
                if row is not None:
                    return str(row["resource_template_uuid"])
                identity = str(uuid4())
                now = utc_now()
                conn.execute(
                    """
                    INSERT INTO workflow_resource_template_identity(
                        authority_id, source_identity, resource_template_uuid,
                        create_time, update_time
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        self._authority.authority_id,
                        source_identity,
                        identity,
                        now,
                        now,
                    ),
                )
                return identity


@dataclass(frozen=True)
class NodeTemplateImport:
    """一个 NodeTemplate 及其显式 Handle aggregate。"""

    template: Mapping[str, object]
    handles: Sequence[Mapping[str, object]]


@dataclass(frozen=True)
class TemplateCatalogSnapshot:
    """一个 compiler 可安全持有的 detached immutable Catalog 值。"""

    authority: CatalogAuthority
    fingerprint: str
    node_templates: tuple[Mapping[str, Any], ...]
    handle_templates: tuple[Mapping[str, Any], ...]
    _nodes_by_uuid: Mapping[str, Mapping[str, Any]] = field(
        repr=False,
        compare=False,
    )
    _handles_by_uuid: Mapping[str, Mapping[str, Any]] = field(
        repr=False,
        compare=False,
    )

    def require_node(self, template_uuid: str) -> Mapping[str, Any]:
        try:
            return self._nodes_by_uuid[template_uuid]
        except (KeyError, TypeError):
            raise TemplateCatalogMismatch("/node_templates/uuid") from None

    def require_handle(
        self,
        handle_uuid: str,
        *,
        node_template_uuid: str,
    ) -> Mapping[str, Any]:
        try:
            handle = self._handles_by_uuid[handle_uuid]
        except (KeyError, TypeError):
            raise TemplateCatalogMismatch("/handle_templates/uuid") from None
        if handle["workflow_node_template_uuid"] != node_template_uuid:
            raise TemplateCatalogMismatch("/handle_templates/parent")
        return handle

    def assert_fingerprint(self, expected: str) -> None:
        if expected != self.fingerprint:
            raise TemplateCatalogStale("/authority/fingerprint")


@dataclass(frozen=True)
class _NormalizedHandle:
    fields: dict[str, Any]
    business_key: tuple[str, str]
    requested_uuid: str | None


@dataclass(frozen=True)
class _NormalizedNode:
    fields: dict[str, Any]
    business_key: tuple[str, str]
    requested_uuid: str | None
    handles: tuple[_NormalizedHandle, ...]


class TemplateCatalog:
    """持久模板写入与稳定 compiler snapshot 的唯一 facade。"""

    def __init__(self, store: WorkflowStore):
        if not isinstance(store, WorkflowStore):
            raise TypeError("TemplateCatalog requires WorkflowStore")
        self._store = store

    def replace(
        self,
        authority: CatalogAuthority,
        templates: Sequence[NodeTemplateImport],
    ) -> TemplateCatalogSnapshot:
        normalized = _normalize_import(authority, templates)
        try:
            with self._store.catalog_guard(), self._store.transaction() as conn:
                metadata = conn.execute(
                    """
                    SELECT authority_kind
                    FROM workflow_template_catalog
                    WHERE authority_id = ?
                    """,
                    (authority.authority_id,),
                ).fetchone()
                if (
                    metadata is not None
                    and metadata["authority_kind"] != authority.kind
                ):
                    raise TemplateCatalogImportError("/authority/kind")

                retained_nodes: list[str] = []
                retained_handles: list[str] = []
                for node in normalized:
                    node_uuid = self._upsert_node(conn, authority, node)
                    retained_nodes.append(node_uuid)
                    for handle in node.handles:
                        retained_handles.append(
                            self._upsert_handle(
                                conn,
                                authority,
                                node_uuid=node_uuid,
                                handle=handle,
                            )
                        )

                _soft_delete_omitted(
                    conn,
                    table="workflow_handle_template",
                    authority_id=authority.authority_id,
                    retained=retained_handles,
                )
                _soft_delete_omitted(
                    conn,
                    table="workflow_node_template",
                    authority_id=authority.authority_id,
                    retained=retained_nodes,
                )
                _, node_rows, handle_rows = self._store._read_template_catalog_rows(
                    authority.authority_id,
                    conn=conn,
                )
                fingerprint = _fingerprint(authority, node_rows, handle_rows)
                conn.execute(
                    """
                    INSERT INTO workflow_template_catalog(
                        authority_id, authority_kind, fingerprint, update_time
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(authority_id) DO UPDATE SET
                        authority_kind = excluded.authority_kind,
                        fingerprint = excluded.fingerprint,
                        update_time = excluded.update_time
                    """,
                    (authority.authority_id, authority.kind, fingerprint, utc_now()),
                )
                return _make_snapshot(
                    authority,
                    fingerprint=fingerprint,
                    node_rows=node_rows,
                    handle_rows=handle_rows,
                )
        except TemplateCatalogError:
            raise
        except sqlite3.Error:
            raise TemplateCatalogImportError("/authority/catalog") from None

    @contextmanager
    def snapshot(
        self,
        authority: CatalogAuthority,
    ) -> Iterator[TemplateCatalogSnapshot]:
        with self._store.catalog_guard():
            try:
                metadata, node_rows, handle_rows = (
                    self._store._read_template_catalog_rows(authority.authority_id)
                )
            except sqlite3.Error:
                raise TemplateCatalogMismatch("/authority/catalog") from None
            if metadata is None:
                raise TemplateCatalogUnavailable("/authority/catalog")
            if metadata["authority_kind"] != authority.kind:
                raise TemplateCatalogMismatch("/authority/kind")
            fingerprint = _fingerprint(authority, node_rows, handle_rows)
            if metadata["fingerprint"] != fingerprint:
                raise TemplateCatalogMismatch("/authority/fingerprint")
            yield _make_snapshot(
                authority,
                fingerprint=fingerprint,
                node_rows=node_rows,
                handle_rows=handle_rows,
            )

    def _upsert_node(
        self,
        conn: sqlite3.Connection,
        authority: CatalogAuthority,
        node: _NormalizedNode,
    ) -> str:
        resource_template_uuid, normalized_name = node.business_key
        active_candidates = conn.execute(
            """
            SELECT * FROM workflow_node_template
            WHERE authority_id = ?
              AND resource_template_uuid = ?
              AND deleted_at IS NULL
            """,
            (authority.authority_id, resource_template_uuid),
        ).fetchall()
        active_matches = [
            row
            for row in active_candidates
            if _business_name(row["name"]) == normalized_name
        ]
        if len(active_matches) > 1:
            raise TemplateCatalogImportError("/node_templates/business_key")
        active = active_matches[0] if active_matches else None

        if authority.kind == "local":
            template_uuid = active["uuid"] if active is not None else str(uuid4())
        else:
            assert node.requested_uuid is not None
            template_uuid = node.requested_uuid
            historical = conn.execute(
                """
                SELECT * FROM workflow_node_template
                WHERE authority_id = ? AND uuid = ?
                """,
                (authority.authority_id, template_uuid),
            ).fetchone()
            collision = conn.execute(
                """
                SELECT 1 FROM workflow_node_template
                WHERE authority_id <> ? AND uuid = ?
                """,
                (authority.authority_id, template_uuid),
            ).fetchone()
            if collision is not None:
                raise TemplateCatalogImportError("/node_templates/uuid")
            if historical is not None and (
                historical["resource_template_uuid"] != resource_template_uuid
                or _business_name(historical["name"]) != normalized_name
            ):
                raise TemplateCatalogImportError("/node_templates/uuid")
            if active is not None and active["uuid"] != template_uuid:
                _soft_delete_node(conn, authority.authority_id, active["uuid"])

        existing = conn.execute(
            """
            SELECT uuid, create_time FROM workflow_node_template
            WHERE authority_id = ? AND uuid = ?
            """,
            (authority.authority_id, template_uuid),
        ).fetchone()
        now = utc_now()
        values = _node_sql_values(authority.authority_id, node.fields)
        if existing is None:
            conn.execute(
                """
                INSERT INTO workflow_node_template(
                    uuid, create_time, update_time, deleted_at, description,
                    meta_data, authority_id, resource_template_uuid, name,
                    display_name, class, goal, goal_default, feedback, result,
                    schema, type, icon, header, footer, node_type
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?)
                """,
                (template_uuid, now, now, *values),
            )
        else:
            conn.execute(
                """
                UPDATE workflow_node_template
                SET update_time = ?, deleted_at = NULL, description = ?,
                    meta_data = ?, authority_id = ?, resource_template_uuid = ?,
                    name = ?, display_name = ?, class = ?, goal = ?,
                    goal_default = ?, feedback = ?, result = ?, schema = ?,
                    type = ?, icon = ?, header = ?, footer = ?, node_type = ?
                WHERE authority_id = ? AND uuid = ?
                """,
                (now, *values, authority.authority_id, template_uuid),
            )
        return template_uuid

    def _upsert_handle(
        self,
        conn: sqlite3.Connection,
        authority: CatalogAuthority,
        *,
        node_uuid: str,
        handle: _NormalizedHandle,
    ) -> str:
        normalized_key, io_type = handle.business_key
        active_candidates = conn.execute(
            """
            SELECT * FROM workflow_handle_template
            WHERE authority_id = ?
              AND workflow_node_template_uuid = ?
              AND io_type = ?
              AND deleted_at IS NULL
            """,
            (authority.authority_id, node_uuid, io_type),
        ).fetchall()
        active_matches = [
            row
            for row in active_candidates
            if _business_name(row["handle_key"]) == normalized_key
        ]
        if len(active_matches) > 1:
            raise TemplateCatalogImportError("/handle_templates/business_key")
        active = active_matches[0] if active_matches else None

        if authority.kind == "local":
            handle_uuid = active["uuid"] if active is not None else str(uuid4())
        else:
            assert handle.requested_uuid is not None
            handle_uuid = handle.requested_uuid
            historical = conn.execute(
                """
                SELECT * FROM workflow_handle_template
                WHERE authority_id = ? AND uuid = ?
                """,
                (authority.authority_id, handle_uuid),
            ).fetchone()
            collision = conn.execute(
                """
                SELECT 1 FROM workflow_handle_template
                WHERE authority_id <> ? AND uuid = ?
                """,
                (authority.authority_id, handle_uuid),
            ).fetchone()
            if collision is not None:
                raise TemplateCatalogImportError("/handle_templates/uuid")
            if historical is not None and (
                historical["workflow_node_template_uuid"] != node_uuid
                or _business_name(historical["handle_key"]) != normalized_key
                or historical["io_type"] != io_type
            ):
                raise TemplateCatalogImportError("/handle_templates/uuid")
            if active is not None and active["uuid"] != handle_uuid:
                _soft_delete_handle(conn, authority.authority_id, active["uuid"])

        existing = conn.execute(
            """
            SELECT uuid FROM workflow_handle_template
            WHERE authority_id = ? AND uuid = ?
            """,
            (authority.authority_id, handle_uuid),
        ).fetchone()
        now = utc_now()
        values = _handle_sql_values(authority.authority_id, node_uuid, handle.fields)
        if existing is None:
            conn.execute(
                """
                INSERT INTO workflow_handle_template(
                    uuid, create_time, update_time, deleted_at, description,
                    meta_data, authority_id, workflow_node_template_uuid,
                    handle_key, io_type, display_name, type, required,
                    data_source, data_key
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (handle_uuid, now, now, *values),
            )
        else:
            conn.execute(
                """
                UPDATE workflow_handle_template
                SET update_time = ?, deleted_at = NULL, description = ?,
                    meta_data = ?, authority_id = ?,
                    workflow_node_template_uuid = ?, handle_key = ?, io_type = ?,
                    display_name = ?, type = ?, required = ?, data_source = ?,
                    data_key = ?
                WHERE authority_id = ? AND uuid = ?
                """,
                (now, *values, authority.authority_id, handle_uuid),
            )
        return handle_uuid


def _normalize_import(
    authority: CatalogAuthority,
    templates: Sequence[NodeTemplateImport],
) -> tuple[_NormalizedNode, ...]:
    if not isinstance(authority, CatalogAuthority):
        raise TemplateCatalogImportError("/authority")
    if isinstance(templates, (str, bytes)) or not isinstance(templates, Sequence):
        raise TemplateCatalogImportError("/node_templates")

    normalized: list[_NormalizedNode] = []
    node_keys: set[tuple[str, str]] = set()
    node_uuids: set[str] = set()
    handle_uuids: set[str] = set()
    for index, item in enumerate(templates):
        path = f"/node_templates/{index}"
        if not isinstance(item, NodeTemplateImport):
            raise TemplateCatalogImportError(path)
        fields, requested_uuid = _normalize_node_fields(
            authority,
            item.template,
            path,
        )
        node_key = (fields["resource_template_uuid"], _business_name(fields["name"]))
        if node_key in node_keys:
            raise TemplateCatalogImportError("/node_templates/business_key")
        node_keys.add(node_key)
        if requested_uuid is not None:
            if requested_uuid in node_uuids:
                raise TemplateCatalogImportError("/node_templates/uuid")
            node_uuids.add(requested_uuid)

        if isinstance(item.handles, (str, bytes)) or not isinstance(
            item.handles, Sequence
        ):
            raise TemplateCatalogImportError(f"{path}/handles")
        handles: list[_NormalizedHandle] = []
        handle_keys: set[tuple[str, str]] = set()
        for handle_index, raw_handle in enumerate(item.handles):
            handle_path = f"/handle_templates/{index}/{handle_index}"
            handle_fields, handle_uuid = _normalize_handle_fields(
                authority,
                raw_handle,
                handle_path,
                requested_parent_uuid=requested_uuid,
            )
            handle_key = (
                _business_name(handle_fields["handle_key"]),
                handle_fields["io_type"],
            )
            if handle_key in handle_keys:
                raise TemplateCatalogImportError("/handle_templates/business_key")
            handle_keys.add(handle_key)
            if handle_uuid is not None:
                if handle_uuid in handle_uuids:
                    raise TemplateCatalogImportError("/handle_templates/uuid")
                handle_uuids.add(handle_uuid)
            handles.append(
                _NormalizedHandle(
                    fields=handle_fields,
                    business_key=handle_key,
                    requested_uuid=handle_uuid,
                )
            )
        normalized.append(
            _NormalizedNode(
                fields=fields,
                business_key=node_key,
                requested_uuid=requested_uuid,
                handles=tuple(handles),
            )
        )
    return tuple(normalized)


def _normalize_node_fields(
    authority: CatalogAuthority,
    raw: Mapping[str, object],
    path: str,
) -> tuple[dict[str, Any], str | None]:
    values = _plain_mapping(raw, path)
    if not set(values).issubset(_NODE_INPUT_FIELDS):
        raise TemplateCatalogImportError(path)
    _reject_deleted(values, path)
    requested_uuid = _identity_uuid(authority, values.get("uuid"), f"{path}/uuid")
    fields: dict[str, Any] = {}
    for key in _NODE_REQUIRED_STRINGS:
        fields[key] = _required_string(values.get(key), f"{path}/{key}")
    fields["resource_template_uuid"] = _uuid_value(
        fields["resource_template_uuid"],
        f"{path}/resource_template_uuid",
    )
    for key in _NODE_JSON_OBJECTS:
        default = {} if key == "meta_data" else None
        fields[key] = _json_object(values.get(key, default), f"{path}/{key}")
    for key in _NODE_OPTIONAL_STRINGS:
        fields[key] = _optional_string(values.get(key), f"{path}/{key}")
    fields["schema"] = _schema_value(values.get("schema"), f"{path}/schema")
    return fields, requested_uuid


def _schema_value(value: object, path: str) -> str | dict[str, Any] | None:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return _json_object(value, path)
    raise TemplateCatalogImportError(path)


def _normalize_handle_fields(
    authority: CatalogAuthority,
    raw: Mapping[str, object],
    path: str,
    *,
    requested_parent_uuid: str | None,
) -> tuple[dict[str, Any], str | None]:
    values = _plain_mapping(raw, path)
    if not set(values).issubset(_HANDLE_INPUT_FIELDS):
        raise TemplateCatalogImportError(path)
    _reject_deleted(values, path)
    requested_uuid = _identity_uuid(authority, values.get("uuid"), f"{path}/uuid")
    supplied_parent = values.get("workflow_node_template_uuid")
    if supplied_parent is not None:
        parent = _uuid_value(supplied_parent, f"{path}/workflow_node_template_uuid")
        if requested_parent_uuid is None or parent != requested_parent_uuid:
            raise TemplateCatalogImportError("/handle_templates/parent")

    fields: dict[str, Any] = {}
    for key in _HANDLE_REQUIRED_STRINGS:
        fields[key] = _required_string(values.get(key), f"{path}/{key}")
    if fields["io_type"] not in ("source", "target"):
        raise TemplateCatalogImportError("/handle_templates/io_type")
    fields["description"] = _optional_string(
        values.get("description"),
        f"{path}/description",
    )
    fields["meta_data"] = _json_object(
        values.get("meta_data", {}),
        f"{path}/meta_data",
    )
    if type(values.get("required")) is not bool:
        raise TemplateCatalogImportError(f"{path}/required")
    fields["required"] = values["required"]
    for key in ("data_source", "data_key"):
        fields[key] = _optional_string(values.get(key), f"{path}/{key}")
    return fields, requested_uuid


def _plain_mapping(value: object, path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TemplateCatalogImportError(path)
    values = dict(value)
    if any(not isinstance(key, str) for key in values):
        raise TemplateCatalogImportError(path)
    return values


def _reject_deleted(values: Mapping[str, Any], path: str) -> None:
    if values.get("deleted_at") is not None:
        raise TemplateCatalogImportError(f"{path}/deleted_at")


def _identity_uuid(
    authority: CatalogAuthority,
    value: object,
    path: str,
) -> str | None:
    if authority.kind == "local":
        if value is not None:
            raise TemplateCatalogImportError(path)
        return None
    if value is None:
        raise TemplateCatalogImportError(path)
    return _uuid_value(value, path)


def _uuid_value(value: object, path: str) -> str:
    if not isinstance(value, str):
        raise TemplateCatalogImportError(path)
    try:
        return str(UUID(value))
    except (ValueError, AttributeError):
        raise TemplateCatalogImportError(path) from None


def _required_string(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TemplateCatalogImportError(path)
    return value


def _optional_string(value: object, path: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TemplateCatalogImportError(path)
    return value


def _json_object(value: object, path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TemplateCatalogImportError(path)
    try:
        normalized = decode_json_bytes(encode_json(dict(value), sort_keys=True))
    except (TypeError, ValueError, UnicodeError):
        raise TemplateCatalogImportError(path) from None
    if not isinstance(normalized, dict):
        raise TemplateCatalogImportError(path)
    return normalized


def _node_sql_values(authority_id: str, fields: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        fields["description"],
        _encode_json(fields["meta_data"]),
        authority_id,
        fields["resource_template_uuid"],
        fields["name"],
        fields["display_name"],
        fields["class"],
        _encode_json(fields["goal"]),
        _encode_json(fields["goal_default"]),
        _encode_json(fields["feedback"]),
        _encode_json(fields["result"]),
        _persisted_schema(fields["schema"]),
        fields["type"],
        fields["icon"],
        fields["header"],
        fields["footer"],
        fields["node_type"],
    )


def _handle_sql_values(
    authority_id: str,
    node_uuid: str,
    fields: Mapping[str, Any],
) -> tuple[Any, ...]:
    return (
        fields["description"],
        _encode_json(fields["meta_data"]),
        authority_id,
        node_uuid,
        fields["handle_key"],
        fields["io_type"],
        fields["display_name"],
        fields["type"],
        int(fields["required"]),
        fields["data_source"],
        fields["data_key"],
    )


def _encode_json(value: Any) -> str:
    return encode_json(value, sort_keys=True).decode("utf-8")


def _persisted_schema(value: Any) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return _encode_json(value)


def _soft_delete_node(
    conn: sqlite3.Connection,
    authority_id: str,
    template_uuid: str,
) -> None:
    now = utc_now()
    conn.execute(
        """
        UPDATE workflow_handle_template
        SET deleted_at = ?, update_time = ?
        WHERE authority_id = ?
          AND workflow_node_template_uuid = ?
          AND deleted_at IS NULL
        """,
        (now, now, authority_id, template_uuid),
    )
    conn.execute(
        """
        UPDATE workflow_node_template
        SET deleted_at = ?, update_time = ?
        WHERE authority_id = ? AND uuid = ? AND deleted_at IS NULL
        """,
        (now, now, authority_id, template_uuid),
    )


def _soft_delete_handle(
    conn: sqlite3.Connection,
    authority_id: str,
    handle_uuid: str,
) -> None:
    now = utc_now()
    conn.execute(
        """
        UPDATE workflow_handle_template
        SET deleted_at = ?, update_time = ?
        WHERE authority_id = ? AND uuid = ? AND deleted_at IS NULL
        """,
        (now, now, authority_id, handle_uuid),
    )


def _soft_delete_omitted(
    conn: sqlite3.Connection,
    *,
    table: Literal["workflow_node_template", "workflow_handle_template"],
    authority_id: str,
    retained: Sequence[str],
) -> None:
    now = utc_now()
    if retained:
        marks = ",".join("?" for _ in retained)
        conn.execute(
            f"""
            UPDATE {table}
            SET deleted_at = ?, update_time = ?
            WHERE authority_id = ? AND deleted_at IS NULL
              AND uuid NOT IN ({marks})
            """,
            (now, now, authority_id, *retained),
        )
    else:
        conn.execute(
            f"""
            UPDATE {table}
            SET deleted_at = ?, update_time = ?
            WHERE authority_id = ? AND deleted_at IS NULL
            """,
            (now, now, authority_id),
        )


def _fingerprint(
    authority: CatalogAuthority,
    node_rows: Sequence[Mapping[str, Any]],
    handle_rows: Sequence[Mapping[str, Any]],
) -> str:
    _validate_persisted_rows(authority, node_rows, handle_rows)
    payload = {
        "version": 1,
        "authority": {
            "authority_id": authority.authority_id,
            "kind": authority.kind,
        },
        "node_templates": [_semantic_node(row) for row in node_rows],
        "handle_templates": [_semantic_handle(row) for row in handle_rows],
    }
    try:
        canonical = encode_json(payload, sort_keys=True)
    except (TypeError, ValueError):
        raise TemplateCatalogMismatch("/authority/catalog") from None
    digest = hashlib.sha256(canonical).hexdigest()
    return f"sha256:{digest}"


def _validate_persisted_rows(
    authority: CatalogAuthority,
    node_rows: Sequence[Mapping[str, Any]],
    handle_rows: Sequence[Mapping[str, Any]],
) -> None:
    node_uuids: set[str] = set()
    node_keys: set[tuple[str, str]] = set()
    for row in node_rows:
        if row.get("authority_id") != authority.authority_id:
            raise TemplateCatalogMismatch("/node_templates/authority")
        if row.get("deleted_at") is not None:
            raise TemplateCatalogMismatch("/node_templates/deleted_at")
        _persisted_uuid(row.get("uuid"), "/node_templates/uuid")
        resource_uuid = _persisted_uuid(
            row.get("resource_template_uuid"),
            "/node_templates/resource_template_uuid",
        )
        for column in (
            "create_time",
            "update_time",
            "name",
            "display_name",
            "type",
            "node_type",
        ):
            _persisted_required_string(
                row.get(column),
                f"/node_templates/{column}",
            )
        for column in _NODE_OPTIONAL_STRINGS:
            _persisted_optional_string(
                row.get(column),
                f"/node_templates/{column}",
            )
        _decode_schema(row.get("schema"))
        name = row["name"]
        node_uuid = row["uuid"]
        node_key = (resource_uuid, _business_name(name))
        if node_uuid in node_uuids or node_key in node_keys:
            raise TemplateCatalogMismatch("/node_templates/business_key")
        node_uuids.add(node_uuid)
        node_keys.add(node_key)
        for column in _NODE_JSON_COLUMNS:
            if not isinstance(_decode_column(row.get(column)), dict):
                raise TemplateCatalogMismatch(f"/node_templates/{column}")

    handle_uuids: set[str] = set()
    handle_keys: set[tuple[str, str, str]] = set()
    for row in handle_rows:
        if row.get("authority_id") != authority.authority_id:
            raise TemplateCatalogMismatch("/handle_templates/authority")
        if row.get("deleted_at") is not None:
            raise TemplateCatalogMismatch("/handle_templates/deleted_at")
        handle_uuid = _persisted_uuid(
            row.get("uuid"),
            "/handle_templates/uuid",
        )
        parent_uuid = _persisted_uuid(
            row.get("workflow_node_template_uuid"),
            "/handle_templates/parent",
        )
        if parent_uuid not in node_uuids:
            raise TemplateCatalogMismatch("/handle_templates/parent")
        for column in (
            "create_time",
            "update_time",
            "handle_key",
            "display_name",
            "type",
        ):
            _persisted_required_string(
                row.get(column),
                f"/handle_templates/{column}",
            )
        for column in _HANDLE_OPTIONAL_STRINGS:
            _persisted_optional_string(
                row.get(column),
                f"/handle_templates/{column}",
            )
        handle_key = row["handle_key"]
        io_type = row.get("io_type")
        if io_type not in ("source", "target"):
            raise TemplateCatalogMismatch("/handle_templates/io_type")
        identity = (parent_uuid, _business_name(handle_key), io_type)
        if handle_uuid in handle_uuids or identity in handle_keys:
            raise TemplateCatalogMismatch("/handle_templates/business_key")
        handle_uuids.add(handle_uuid)
        handle_keys.add(identity)
        required = row.get("required")
        if type(required) is not int or required not in (0, 1):
            raise TemplateCatalogMismatch("/handle_templates/required")
        if not isinstance(_decode_column(row.get("meta_data")), dict):
            raise TemplateCatalogMismatch("/handle_templates/meta_data")


def _persisted_uuid(value: object, path: str) -> str:
    if not isinstance(value, str):
        raise TemplateCatalogMismatch(path)
    try:
        normalized = str(UUID(value))
    except (ValueError, AttributeError):
        raise TemplateCatalogMismatch(path) from None
    if normalized != value:
        raise TemplateCatalogMismatch(path)
    return normalized


def _persisted_required_string(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TemplateCatalogMismatch(path)
    return value


def _persisted_optional_string(value: object, path: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise TemplateCatalogMismatch(path)
    return value


def _semantic_node(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "uuid": row["uuid"],
        "description": row["description"],
        "meta_data": _decode_column(row["meta_data"]),
        "resource_template_uuid": row["resource_template_uuid"],
        "name": row["name"],
        "display_name": row["display_name"],
        "class": row["class"],
        "goal": _decode_column(row["goal"]),
        "goal_default": _decode_column(row["goal_default"]),
        "feedback": _decode_column(row["feedback"]),
        "result": _decode_column(row["result"]),
        "schema": _decode_schema(row["schema"]),
        "type": row["type"],
        "icon": row["icon"],
        "header": row["header"],
        "footer": row["footer"],
        "node_type": row["node_type"],
    }


def _semantic_handle(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "uuid": row["uuid"],
        "description": row["description"],
        "meta_data": _decode_column(row["meta_data"]),
        "workflow_node_template_uuid": row["workflow_node_template_uuid"],
        "handle_key": row["handle_key"],
        "io_type": row["io_type"],
        "display_name": row["display_name"],
        "type": row["type"],
        "required": bool(row["required"]),
        "data_source": row["data_source"],
        "data_key": row["data_key"],
    }


def _decode_column(value: Any) -> Any:
    if not isinstance(value, str):
        raise TemplateCatalogMismatch("/authority/catalog")
    try:
        return decode_json_bytes(value.encode("utf-8"))
    except (UnicodeError, ValueError):
        raise TemplateCatalogMismatch("/authority/catalog") from None


def _decode_schema(value: Any) -> Any:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TemplateCatalogMismatch("/node_templates/schema")
    if not value.startswith("{"):
        return value
    decoded = _decode_column(value)
    if not isinstance(decoded, dict):
        raise TemplateCatalogMismatch("/node_templates/schema")
    return decoded


def _make_snapshot(
    authority: CatalogAuthority,
    *,
    fingerprint: str,
    node_rows: Sequence[Mapping[str, Any]],
    handle_rows: Sequence[Mapping[str, Any]],
) -> TemplateCatalogSnapshot:
    nodes = tuple(_freeze_json(_snapshot_node(row)) for row in node_rows)
    handles = tuple(_freeze_json(_snapshot_handle(row)) for row in handle_rows)
    node_index = MappingProxyType({node["uuid"]: node for node in nodes})
    handle_index = MappingProxyType({handle["uuid"]: handle for handle in handles})
    return TemplateCatalogSnapshot(
        authority=authority,
        fingerprint=fingerprint,
        node_templates=nodes,
        handle_templates=handles,
        _nodes_by_uuid=node_index,
        _handles_by_uuid=handle_index,
    )


def _snapshot_node(row: Mapping[str, Any]) -> dict[str, Any]:
    value = _semantic_node(row)
    value["create_time"] = row["create_time"]
    value["update_time"] = row["update_time"]
    return value


def _snapshot_handle(row: Mapping[str, Any]) -> dict[str, Any]:
    value = _semantic_handle(row)
    value["create_time"] = row["create_time"]
    value["update_time"] = row["update_time"]
    return value


def _freeze_json(value: Any) -> Any:
    """无递归地把 detached JSON 转为 mapping-proxy/tuple。"""

    if not isinstance(value, (dict, list)):
        return value
    frozen: dict[int, Any] = {}
    stack: list[tuple[Any, bool]] = [(value, False)]
    while stack:
        current, expanded = stack.pop()
        if not isinstance(current, (dict, list)):
            continue
        identity = id(current)
        if identity in frozen:
            continue
        if not expanded:
            stack.append((current, True))
            children = current.values() if isinstance(current, dict) else current
            for child in children:
                if isinstance(child, (dict, list)) and id(child) not in frozen:
                    stack.append((child, False))
            continue
        if isinstance(current, dict):
            frozen[identity] = MappingProxyType(
                {
                    key: frozen[id(child)] if isinstance(child, (dict, list)) else child
                    for key, child in current.items()
                }
            )
        else:
            frozen[identity] = tuple(
                frozen[id(child)] if isinstance(child, (dict, list)) else child
                for child in current
            )
    return frozen[id(value)]


__all__ = [
    "CatalogAuthority",
    "LocalResourceTemplateIdentityResolver",
    "NodeTemplateImport",
    "TemplateCatalog",
    "TemplateCatalogError",
    "TemplateCatalogImportError",
    "TemplateCatalogMismatch",
    "TemplateCatalogSnapshot",
    "TemplateCatalogStale",
    "TemplateCatalogUnavailable",
]
