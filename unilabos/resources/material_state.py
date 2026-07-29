"""OS 当前内存物料状态及只读传输快照。

ResourceTreeSet 仍是唯一可变权威。本模块只持有该对象的引用，并在 schedule
通道需要时生成不可变 JSON 快照；它不是第二份物料数据库。
"""

from __future__ import annotations

import json
import threading
import uuid
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from unilabos.resources.material_authoring import (
    MaterialAuthoringError,
    build_material_nodes,
)
from unilabos.resources.resource_tracker import ResourceTreeSet


class CurrentMaterialState:
    """持有 OS 当前 ResourceTreeSet，并生成版本化只读快照。"""

    def __init__(
        self,
        resources: ResourceTreeSet,
        *,
        source_id: str = "os-current",
    ) -> None:
        self._lock = threading.RLock()
        self._resources = resources
        self._source_id = Path(source_id).name or "os-current"
        self._creation_by_idempotency_key: dict[str, tuple[str, str]] = {}
        self._creation_source_by_operation: dict[str, str] = {}
        self._undo_idempotency_keys: set[str] = set()

    def replace(
        self,
        resources: ResourceTreeSet,
        *,
        source_id: str | None = None,
    ) -> None:
        """仅供 OS 启动/重载路径替换当前权威 ResourceTreeSet。"""

        with self._lock:
            self._resources = resources
            if source_id is not None:
                self._source_id = Path(source_id).name or "os-current"
            self._creation_by_idempotency_key.clear()
            self._creation_source_by_operation.clear()
            self._undo_idempotency_keys.clear()

    def create_material(
        self,
        template: dict[str, Any],
        command: dict[str, Any],
    ) -> dict[str, Any]:
        """在唯一 ResourceTreeSet 权威中执行幂等模板实例化。"""

        idempotency_key = str(
            command.get("idempotency_key") or ""
        ).strip()
        if not idempotency_key:
            raise MaterialAuthoringError("idempotency_key is required")
        with self._lock:
            cached = self._creation_by_idempotency_key.get(
                idempotency_key
            )
            if cached is None:
                try:
                    expected_revision = int(
                        command.get("expected_revision")
                    )
                except (TypeError, ValueError) as exc:
                    raise MaterialAuthoringError(
                        "expected_revision must be an integer"
                    ) from exc
                current_revision = int(self.snapshot()["revision"])
                if expected_revision != current_revision:
                    raise MaterialAuthoringError(
                        f"expected revision {expected_revision}, "
                        f"current revision is {current_revision}"
                    )
                nodes, source_node_id = build_material_nodes(
                    template,
                    command,
                    existing_names=[
                        node.res_content.name
                        for node in self._resources.all_nodes
                    ],
                )
                operation_id = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        (
                            f"unilabos:{self._source_id}:"
                            f"material-create:{idempotency_key}"
                        ),
                    )
                )
                for node in nodes:
                    config = (
                        node.get("config")
                        if isinstance(node.get("config"), dict)
                        else {}
                    )
                    config["creation_operation_id"] = operation_id
                    node["config"] = config
                created = ResourceTreeSet.from_raw_dict_list(nodes)
                self._resources.trees.extend(created.trees)
                cached = (source_node_id, operation_id)
                self._creation_by_idempotency_key[
                    idempotency_key
                ] = cached
                self._creation_source_by_operation[
                    operation_id
                ] = source_node_id
            source_node_id, operation_id = cached
            if source_node_id not in {
                node.res_content.id for node in self._resources.all_nodes
            }:
                raise MaterialAuthoringError(
                    "idempotent create result no longer exists"
                )
            return {
                "source_node_id": source_node_id,
                "creation_operation_id": operation_id,
                "snapshot": self.snapshot(),
            }

    def undo_create_material(
        self,
        command: dict[str, Any],
    ) -> dict[str, Any]:
        """按 revision 和创建操作幂等补偿删除新建资源树。"""

        idempotency_key = str(
            command.get("idempotency_key") or ""
        ).strip()
        if not idempotency_key:
            raise MaterialAuthoringError("idempotency_key is required")
        with self._lock:
            if idempotency_key in self._undo_idempotency_keys:
                return {"snapshot": self.snapshot()}
            try:
                expected_revision = int(command.get("expected_revision"))
            except (TypeError, ValueError) as exc:
                raise MaterialAuthoringError(
                    "expected_revision must be an integer"
                ) from exc
            current_revision = int(self.snapshot()["revision"])
            if expected_revision != current_revision:
                raise MaterialAuthoringError(
                    f"expected revision {expected_revision}, "
                    f"current revision is {current_revision}"
                )
            operation_id = str(
                command.get("creation_operation_id") or ""
            ).strip()
            source_node_id = str(
                command.get("source_node_id") or ""
            ).strip()
            if (
                not operation_id
                or self._creation_source_by_operation.get(operation_id)
                != source_node_id
            ):
                raise MaterialAuthoringError(
                    "creation operation does not own this material"
                )
            remaining = [
                tree
                for tree in self._resources.trees
                if tree.root_node.res_content.id != source_node_id
            ]
            if len(remaining) == len(self._resources.trees):
                raise MaterialAuthoringError("material does not exist")
            self._resources.trees[:] = remaining
            self._creation_source_by_operation.pop(operation_id, None)
            self._undo_idempotency_keys.add(idempotency_key)
            return {"snapshot": self.snapshot()}

    def snapshot(self) -> dict[str, Any]:
        """从当前 ResourceTreeSet 生成 schedule wire 快照。

        runtime-only ``host_node`` 不属于实验室 Material Graph，因此不公开。
        parent 统一使用稳定的源 node id，避免暴露 OS 内部对象引用。
        """

        with self._lock:
            nodes: list[dict[str, Any]] = []
            for instance in tuple(self._resources.all_nodes):
                resource = instance.res_content
                if (
                    resource.id == "host_node"
                    or resource.klass == "host_node"
                ):
                    continue
                node = resource.model_dump(
                    by_alias=True,
                    mode="json",
                )
                node["parent"] = (
                    resource.parent.id
                    if resource.parent is not None
                    and resource.parent.id != "host_node"
                    else None
                )
                pose = node.get("pose")
                if isinstance(pose, dict):
                    position = pose.get("position")
                    if isinstance(position, dict):
                        node["position"] = dict(position)
                nodes.append(node)
            source_id = self._source_id

        encoded = json.dumps(
            nodes,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return {
            "schema": "unilab/material-snapshot-v1",
            "source_id": source_id,
            "revision": max(zlib.crc32(encoded), 1),
            "modified_at": datetime.now(tz=timezone.utc).isoformat().replace(
                "+00:00",
                "Z",
            ),
            "nodes": nodes,
        }
