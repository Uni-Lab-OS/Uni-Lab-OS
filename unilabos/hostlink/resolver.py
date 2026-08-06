"""Host 本地资源解析：物料数据库未命中或不可用时的兼容兜底。

原链路：slave → ROS service ``/resources/get`` → host → 云端
``/edge/material/query``。现在 Host 优先查询其 Edge 物料数据库服务；尚未导入
数据库的配置树和设备运行时资源仍由本模块兜底解析。

本模块对 ResourceTreeSet 采用 duck-typing（只用 ``trees`` /
``tree.get_all_nodes()`` / ``node.res_content`` / ``node.children``），
不 import resource_tracker，因此可以在无 ROS / 无 pylabrobot 环境下测试。

返回形状与旧云端接口一致：扁平 raw dict 列表（``model_dump(by_alias=True)``），
设备端继续用 ``ResourceTreeSet.from_raw_dict_list`` 解析，旧调用方零改动。
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any, Callable, Dict, List, Optional


class ResourceNotFound(Exception):
    """uuid / id 在本地树中不存在。"""


def _node_dict(node: Any) -> Dict[str, Any]:
    return node.res_content.model_dump(by_alias=True)


def _collect_subtree(node: Any, with_children: bool) -> List[Dict[str, Any]]:
    result = [_node_dict(node)]
    if with_children:
        for child in getattr(node, "children", []) or []:
            result.extend(_collect_subtree(child, True))
    return result


class LocalResourceResolver:
    """在 host 的 ResourceTreeSet 上按 uuid / id 解析子树。

    tree_set_getter 每次调用取最新树（host 的树会随注册/挂载变化），
    避免持有过期引用；加锁防止与树更新并发迭代冲突（粗粒度即可，
    查询频率远低于树规模）。
    """

    def __init__(self, tree_set_getter: Callable[[], Any]):
        self._get_tree_set = tree_set_getter
        self._lock = threading.Lock()

    def resolve(
        self,
        uuid: Optional[str] = None,
        res_id: Optional[str] = None,
        with_children: bool = True,
    ) -> List[Dict[str, Any]]:
        """按 uuid（优先）或 id 查找节点，返回扁平 raw dict 列表。"""
        if not uuid and not res_id:
            raise ValueError("resolve requires uuid or res_id")
        with self._lock:
            tree_set = self._get_tree_set()
            if tree_set is None:
                raise ResourceNotFound("local resource tree not ready")
            node = self._find(tree_set, uuid=uuid, res_id=res_id)
            if node is None:
                raise ResourceNotFound(f"resource not found: uuid={uuid!r} id={res_id!r}")
            return _collect_subtree(node, with_children)

    def dump_all(self) -> List[Dict[str, Any]]:
        """整树导出（调试/全量同步用）。"""
        with self._lock:
            tree_set = self._get_tree_set()
            if tree_set is None:
                return []
            return [
                _node_dict(node)
                for tree in tree_set.trees
                for node in tree.get_all_nodes()
            ]

    @staticmethod
    def _find(tree_set: Any, uuid: Optional[str], res_id: Optional[str]) -> Any:
        for tree in tree_set.trees:
            for node in tree.get_all_nodes():
                content = node.res_content
                if uuid and getattr(content, "uuid", None) == uuid:
                    return node
                if res_id and not uuid and getattr(content, "id", None) == res_id:
                    return node
        return None


class InventoryResourceAliasResolver:
    """Resolve durable Inventory UUIDs through ResourceTree source identities.

    Resource graph nodes without explicit UUIDs may receive a new runtime UUID
    after an OS restart, while Inventory intentionally preserves the UUID first
    admitted into its durable store.  ``meta_data.source_node_id`` is the stable
    join key between those two representations.  Returned nodes are rebound to
    Inventory UUIDs so downstream ResourceSlot conversion and material
    accounting use the same identity.
    """

    def __init__(
        self,
        local_resolver: LocalResourceResolver,
        inventory_snapshot_getter: Callable[[], Mapping[str, Any]],
    ) -> None:
        self._local_resolver = local_resolver
        self._get_inventory_snapshot = inventory_snapshot_getter

    def resolve(self, *, uuid: str, with_children: bool = True) -> List[Dict[str, Any]]:
        try:
            return self._local_resolver.resolve(
                uuid=uuid,
                with_children=with_children,
            )
        except ResourceNotFound as local_error:
            snapshot = self._get_inventory_snapshot()
            materials = snapshot.get("materials", []) if isinstance(snapshot, Mapping) else []
            if not isinstance(materials, list):
                raise local_error

            active_materials = [
                material
                for material in materials
                if isinstance(material, Mapping) and material.get("deleted_at") is None
            ]
            by_uuid = {
                str(material.get("uuid")): material
                for material in active_materials
                if material.get("uuid")
            }
            target = by_uuid.get(uuid)
            if target is None:
                raise local_error
            target_meta = target.get("meta_data")
            source_node_id = (
                str(target_meta.get("source_node_id"))
                if isinstance(target_meta, Mapping) and target_meta.get("source_node_id")
                else ""
            )
            if not source_node_id:
                raise local_error

            runtime_nodes = self._local_resolver.resolve(
                res_id=source_node_id,
                with_children=with_children,
            )
            by_source_node_id = {}
            for material in active_materials:
                meta_data = material.get("meta_data")
                if isinstance(meta_data, Mapping) and meta_data.get("source_node_id"):
                    by_source_node_id[str(meta_data["source_node_id"])] = material

            rebound_nodes: List[Dict[str, Any]] = []
            for runtime_node in runtime_nodes:
                rebound = dict(runtime_node)
                material = by_source_node_id.get(str(runtime_node.get("id") or ""))
                if material is not None:
                    rebound["uuid"] = str(material["uuid"])
                    rebound["parent_uuid"] = material.get("parent_uuid")
                    rebound["resource_template_uuid"] = material.get(
                        "resource_template_uuid"
                    )
                    rebound["meta_data"] = dict(material.get("meta_data") or {})
                rebound_nodes.append(rebound)
            return rebound_nodes


__all__ = [
    "InventoryResourceAliasResolver",
    "LocalResourceResolver",
    "ResourceNotFound",
]
