"""聚合包目录（PackageCatalog）的不可变注册表快照（Registry Snapshot）。"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

import rfc8785

from .catalog import PackageAsset, PackageCatalog, PackageDefinition

DefinitionKind = Literal["device", "resource", "workflow"]


class RegistrySnapshotError(ValueError):
    """表示完整注册表快照（Registry Snapshot）无法安全建立或发布。"""


@dataclass(frozen=True, slots=True)
class RegistryAsset:
    """带软件包命名空间的静态资产身份。"""

    namespace: str
    logical_path: str
    digest: str
    size: int

    @classmethod
    def from_package_asset(
        cls,
        *,
        namespace: str,
        asset: PackageAsset,
    ) -> RegistryAsset:
        """把软件包资产投影为跨包唯一的注册表资产。

        参数：``namespace`` 是软件包规范命名空间；``asset`` 是目录内静态资产。
        返回：保留来源命名空间和完整性摘要的不可变资产身份。
        异常：无；包目录（PackageCatalog）已验证资产字段。
        """

        return cls(
            namespace=namespace,
            logical_path=asset.logical_path,
            digest=asset.digest,
            size=asset.size,
        )

    @property
    def fqid(self) -> str:
        """返回资产的跨包规范身份。

        参数：无。
        返回：由软件包命名空间和逻辑路径组成的稳定身份。
        异常：无。
        """

        return f"{self.namespace}:{self.logical_path}"


@dataclass(frozen=True, slots=True)
class RegistryActivationPlan:
    """物理图（Graph）从完整目录有限选择出的激活计划。"""

    snapshot_fingerprint: str
    devices: tuple[PackageDefinition, ...]
    resources: tuple[PackageDefinition, ...]
    node_definitions: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """冻结节点到规范定义身份的映射。

        参数：无；使用构造时字段。
        返回：无；替换为按节点身份排序的只读映射。
        异常：无。
        """

        object.__setattr__(
            self,
            "node_definitions",
            MappingProxyType(dict(sorted(self.node_definitions.items()))),
        )

    @property
    def selected_definition_fqids(self) -> tuple[str, ...]:
        """返回本计划实际选择的规范定义身份。

        参数：无。
        返回：设备和资源定义去重后的稳定排序元组。
        异常：无。
        """

        return tuple(sorted({item.fqid for item in (*self.devices, *self.resources)}))


@dataclass(frozen=True, slots=True)
class RegistrySnapshot:
    """一次完整、不可变且尚未导入作者模块的注册表定义代际。"""

    fingerprint: str
    package_catalogs: tuple[PackageCatalog, ...]
    devices: tuple[PackageDefinition, ...]
    resources: tuple[PackageDefinition, ...]
    workflows: tuple[PackageDefinition, ...]
    assets: tuple[RegistryAsset, ...]
    _definitions_by_kind: Mapping[str, Mapping[str, PackageDefinition]] = field(
        repr=False,
        compare=False,
    )
    _short_identities: Mapping[str, Mapping[str, tuple[PackageDefinition, ...]]] = (
        field(repr=False, compare=False)
    )

    def resolve(
        self,
        kind: DefinitionKind,
        identity: str,
    ) -> PackageDefinition:
        """解析规范全限定身份或全代唯一兼容短名。

        参数：``kind`` 是设备、资源或工作流定义种类；``identity`` 是规范 FQID
        或遗留短身份。
        返回：快照中原有的同一个不可变定义对象，不创建别名定义。
        异常：种类或身份非法、定义不存在、短身份歧义时抛出
        ``RegistrySnapshotError`` 并关闭式拒绝。
        """

        if kind not in self._definitions_by_kind:
            raise RegistrySnapshotError(f"不支持的注册表定义种类: {kind}")
        if not isinstance(identity, str) or not identity.strip():
            raise RegistrySnapshotError("注册表定义身份不能为空")
        normalized_identity = identity.strip()
        # ``canonical_definition`` 是规范 FQID 精确命中的定义，绝不降级短名。
        canonical_definition = self._definitions_by_kind[kind].get(normalized_identity)
        if canonical_definition is not None:
            return canonical_definition
        if "." in normalized_identity:
            raise RegistrySnapshotError(f"{kind} 规范定义不存在: {normalized_identity}")
        # ``short_candidates`` 是整代快照中共享同一遗留短身份的定义集合。
        short_candidates = self._short_identities[kind].get(
            normalized_identity,
            (),
        )
        if not short_candidates:
            raise RegistrySnapshotError(
                f"{kind} 注册表定义不存在: {normalized_identity}"
            )
        if len(short_candidates) > 1:
            raise RegistrySnapshotError(
                f"{kind} 注册表短身份存在歧义: {normalized_identity}"
            )
        return short_candidates[0]

    def select(self, graph_data: Mapping[str, Any]) -> RegistryActivationPlan:
        """根据物理图（Graph）选择有限设备和资源定义。

        参数：``graph_data`` 是启动时已固定观察的物理图 JSON 对象。
        返回：只含图实际引用的软件包设备和资源的不可变激活计划。
        异常：图结构、社区规范身份或遗留短身份无效时抛出
        ``RegistrySnapshotError``；内置非软件包身份留给原注册表解析。
        """

        if not isinstance(graph_data, Mapping):
            raise RegistrySnapshotError("物理图必须是对象")
        raw_nodes = graph_data.get("nodes", ())
        if not isinstance(raw_nodes, (list, tuple)):
            raise RegistrySnapshotError("物理图 nodes 必须是数组")
        selected_devices: dict[str, PackageDefinition] = {}
        selected_resources: dict[str, PackageDefinition] = {}
        node_definitions: dict[str, str] = {}
        for node_index, raw_node in enumerate(raw_nodes):
            if not isinstance(raw_node, Mapping):
                raise RegistrySnapshotError("物理图节点必须是对象")
            raw_identity = raw_node.get("class")
            if not isinstance(raw_identity, str) or not raw_identity.strip():
                continue
            identity = raw_identity.strip()
            raw_node_type = raw_node.get("type")
            # ``node_kind`` 兼容历史图中缺失 ``type`` 的设备节点；只有明确的非设备
            # 类型才按资源定义解析，避免旧设备被错误投影为资源。
            node_kind: Literal["device", "resource"] = (
                "device" if raw_node_type in (None, "device") else "resource"
            )
            # 非社区且未被软件包短名索引收录的身份属于内置注册表，本快照不接管。
            if (
                not identity.startswith("community.")
                and identity not in self._short_identities[node_kind]
            ):
                continue
            definition = self.resolve(node_kind, identity)
            destination = (
                selected_devices if node_kind == "device" else selected_resources
            )
            destination[definition.fqid] = definition
            node_identity = str(raw_node.get("id") or node_index)
            node_definitions[node_identity] = definition.fqid
        return RegistryActivationPlan(
            snapshot_fingerprint=self.fingerprint,
            devices=tuple(selected_devices[key] for key in sorted(selected_devices)),
            resources=tuple(
                selected_resources[key] for key in sorted(selected_resources)
            ),
            node_definitions=node_definitions,
        )

    def publish(self, registry: Any) -> None:
        """把完整设备和资源集合原子并入现有注册表（Registry）。

        参数：``registry`` 提供设备与资源注册表映射；产品注册表可实现
        ``publish_package_snapshot`` 以在内部锁内完成真正原子替换。
        返回：无；成功后保留内置定义并一次发布完整软件包定义。
        异常：注册表形状、身份冲突或替换失败时传播异常；通用 Adapter 会尽力恢复
        两个原映射，产品运行路径必须使用内部原子接缝。
        """

        publish_snapshot = getattr(registry, "publish_package_snapshot", None)
        if callable(publish_snapshot):
            publish_snapshot(self)
            return
        try:
            original_devices = copy.deepcopy(dict(registry.device_type_registry))
            original_resources = copy.deepcopy(dict(registry.resource_type_registry))
        except (AttributeError, TypeError) as error:
            raise RegistrySnapshotError("注册表必须提供设备和资源定义映射") from error
        candidate_devices, candidate_resources = self.registry_candidates(
            original_devices,
            original_resources,
        )
        try:
            registry.device_type_registry = candidate_devices
            registry.resource_type_registry = candidate_resources
        except Exception:
            _restore_registry_mapping(
                registry,
                attribute="device_type_registry",
                original=original_devices,
            )
            _restore_registry_mapping(
                registry,
                attribute="resource_type_registry",
                original=original_resources,
            )
            raise

    def registry_candidates(
        self,
        original_devices: Mapping[str, Any],
        original_resources: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """构造可在注册表锁内一次发布的完整候选集合。

        参数：``original_devices`` 与 ``original_resources`` 是发布前的完整设备和
        资源注册表事实。
        返回：保留内置定义、移除上一代软件包托管项并加入本代完整目录的两个新
        字典；返回容器不与输入共享可变值。
        异常：输入不是映射、软件包定义缺少注册表条目或与未托管定义冲突时抛出
        ``RegistrySnapshotError``；实时注册表不会被本方法修改。
        """

        if not isinstance(original_devices, Mapping) or not isinstance(
            original_resources,
            Mapping,
        ):
            raise RegistrySnapshotError("注册表候选输入必须是设备和资源映射")
        # ``candidate_devices`` 与 ``candidate_resources`` 先完整构造，供产品注册表
        # 在同一把锁中校验并替换，避免设备与资源出现跨代部分发布。
        candidate_devices = _merge_registry_definitions(
            originals=original_devices,
            definitions=self.devices,
            catalogs=self.package_catalogs,
        )
        candidate_resources = _merge_registry_definitions(
            originals=original_resources,
            definitions=self.resources,
            catalogs=self.package_catalogs,
        )
        return candidate_devices, candidate_resources

    def to_dict(self) -> dict[str, Any]:
        """返回完整注册表快照（Registry Snapshot）的可序列化查询对象。

        参数：无。
        返回：包含软件包、定义、资产和指纹的新字典，不共享冻结容器。
        异常：无。
        """

        return {
            "assets": [
                {
                    "digest": item.digest,
                    "fqid": item.fqid,
                    "logical_path": item.logical_path,
                    "namespace": item.namespace,
                    "size": item.size,
                }
                for item in self.assets
            ],
            "devices": [item.to_dict() for item in self.devices],
            "fingerprint": self.fingerprint,
            "packages": [item.to_dict() for item in self.package_catalogs],
            "resources": [item.to_dict() for item in self.resources],
            "workflows": [item.to_dict() for item in self.workflows],
        }


def compile_registry_snapshot(
    catalogs: Iterable[PackageCatalog],
) -> RegistrySnapshot:
    """聚合并完整校验一代软件包注册表快照（Registry Snapshot）。

    参数：``catalogs`` 是本次启动显式授权且已完整编译的包目录（PackageCatalog）
    集合。
    返回：顺序稳定、完整可查询且不会导入作者模块的不可变注册表快照。
    异常：目录类型、命名空间、FQID、工作流 UUID 或资产身份冲突时抛出
    ``RegistrySnapshotError``，不产生可发布的部分结果。
    """

    try:
        package_catalogs = tuple(
            sorted(
                catalogs,
                key=lambda item: (item.namespace, item.catalog_digest),
            )
        )
    except (AttributeError, TypeError) as error:
        raise RegistrySnapshotError("输入必须全部是包目录（PackageCatalog）") from error
    if any(not isinstance(item, PackageCatalog) for item in package_catalogs):
        raise RegistrySnapshotError("输入必须全部是包目录（PackageCatalog）")
    namespaces: set[str] = set()
    definitions_by_kind: dict[str, dict[str, PackageDefinition]] = {
        "device": {},
        "resource": {},
        "workflow": {},
    }
    short_identities: dict[str, dict[str, list[PackageDefinition]]] = {
        "device": {},
        "resource": {},
        "workflow": {},
    }
    workflow_uuids: set[str] = set()
    assets_by_identity: dict[str, RegistryAsset] = {}
    for catalog in package_catalogs:
        if catalog.namespace in namespaces:
            raise RegistrySnapshotError(f"软件包命名空间重复: {catalog.namespace}")
        namespaces.add(catalog.namespace)
        for kind, definitions in (
            ("device", catalog.definitions.devices),
            ("resource", catalog.definitions.resources),
            ("workflow", catalog.definitions.workflows),
        ):
            for definition in definitions:
                if definition.kind != kind:
                    raise RegistrySnapshotError("软件包定义种类与目录集合不一致")
                if definition.fqid in definitions_by_kind[kind]:
                    raise RegistrySnapshotError(
                        f"注册表规范身份重复: {definition.fqid}"
                    )
                definitions_by_kind[kind][definition.fqid] = definition
                short_identities[kind].setdefault(definition.id, []).append(definition)
                if kind == "workflow":
                    workflow_uuid = definition.details.get("workflow_uuid")
                    if isinstance(workflow_uuid, str) and workflow_uuid:
                        if workflow_uuid in workflow_uuids:
                            raise RegistrySnapshotError(
                                f"工作流 UUID 重复: {workflow_uuid}"
                            )
                        workflow_uuids.add(workflow_uuid)
        for package_asset in catalog.assets:
            registry_asset = RegistryAsset.from_package_asset(
                namespace=catalog.namespace,
                asset=package_asset,
            )
            if registry_asset.fqid in assets_by_identity:
                raise RegistrySnapshotError(
                    f"注册表资产身份重复: {registry_asset.fqid}"
                )
            assets_by_identity[registry_asset.fqid] = registry_asset

    frozen_definitions = MappingProxyType(
        {
            kind: MappingProxyType(dict(sorted(definitions.items())))
            for kind, definitions in definitions_by_kind.items()
        }
    )
    frozen_short_identities = MappingProxyType(
        {
            kind: MappingProxyType(
                {
                    short_identity: tuple(
                        sorted(definitions, key=lambda item: item.fqid)
                    )
                    for short_identity, definitions in sorted(short_map.items())
                }
            )
            for kind, short_map in short_identities.items()
        }
    )
    # ``fingerprint`` 只依赖规范软件包摘要集合，不依赖调用顺序或绝对路径。
    fingerprint = (
        "sha256:"
        + hashlib.sha256(
            rfc8785.dumps(
                {
                    "catalogs": [
                        {
                            "catalog_digest": item.catalog_digest,
                            "namespace": item.namespace,
                        }
                        for item in package_catalogs
                    ],
                    "schema_version": "1",
                }
            )
        ).hexdigest()
    )
    return RegistrySnapshot(
        fingerprint=fingerprint,
        package_catalogs=package_catalogs,
        devices=tuple(definitions_by_kind["device"].values()),
        resources=tuple(definitions_by_kind["resource"].values()),
        workflows=tuple(definitions_by_kind["workflow"].values()),
        assets=tuple(assets_by_identity.values()),
        _definitions_by_kind=frozen_definitions,
        _short_identities=frozen_short_identities,
    )


def _merge_registry_definitions(
    *,
    originals: Mapping[str, Any],
    definitions: tuple[PackageDefinition, ...],
    catalogs: tuple[PackageCatalog, ...],
) -> dict[str, Any]:
    """构造保留内置项并替换旧软件包项的完整注册表候选。

    参数：``originals`` 是当前实时注册表；``definitions`` 是本代某类完整定义；
    ``catalogs`` 提供每项定义所属的目录摘要。
    返回：尚未发布、与实时字典无共享容器的完整候选映射。
    异常：软件包身份与未托管内置身份冲突时抛出 ``RegistrySnapshotError``。
    """

    # ``catalog_digests`` 允许每个定义携带其来源目录的稳定证据。
    catalog_digests = {
        catalog.namespace: catalog.catalog_digest for catalog in catalogs
    }
    candidate = {
        key: copy.deepcopy(value)
        for key, value in originals.items()
        if not (isinstance(value, Mapping) and value.get("package_definition_fqid"))
    }
    for definition in definitions:
        if definition.fqid in candidate:
            raise RegistrySnapshotError(
                f"软件包定义与既有注册表身份冲突: {definition.fqid}"
            )
        details = definition.to_dict()["details"]
        registry_entry = details.get("registry_entry")
        if not isinstance(registry_entry, dict):
            raise RegistrySnapshotError(f"软件包定义缺少注册表条目: {definition.fqid}")
        entry = copy.deepcopy(registry_entry)
        entry["id"] = definition.fqid
        # ``source_fqid`` 是产品模板投影使用的 Python 源码身份，必须保持
        # ``module:symbol``；软件包内的规范定义身份由独立字段承担。
        entry["source_fqid"] = f"{definition.module}:{definition.symbol}"
        entry["package_definition_fqid"] = definition.fqid
        entry["content_hash"] = definition.content_hash
        entry["package_catalog_digest"] = catalog_digests[
            definition.fqid.rsplit(".", 1)[0]
        ]
        candidate[definition.fqid] = entry
    return candidate


def _restore_registry_mapping(
    registry: Any,
    *,
    attribute: str,
    original: Mapping[str, Any],
) -> None:
    """在通用发布 Adapter 失败后恢复一个注册表集合。

    参数：``registry`` 是通用注册表对象；``attribute`` 是待恢复属性；``original``
    是发布前分离副本。
    返回：无；若目标已经等于原值则不重复写入。
    异常：恢复失败会被吞掉，让调用者仍收到最初发布异常；产品路径不得依赖此
    通用 Adapter 获得强原子保证。
    """

    try:
        current = getattr(registry, attribute)
        if dict(current) == dict(original):
            return
        setattr(registry, attribute, copy.deepcopy(dict(original)))
    except Exception:  # noqa: BLE001 - 回滚不得遮蔽最初的发布异常。
        return


__all__ = [
    "RegistryActivationPlan",
    "RegistryAsset",
    "RegistrySnapshot",
    "RegistrySnapshotError",
    "compile_registry_snapshot",
]
