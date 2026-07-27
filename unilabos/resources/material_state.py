"""OS 当前内存物料状态及只读传输快照。

ResourceTreeSet 仍是唯一可变权威。本模块只持有该对象的引用，并在 schedule
通道需要时生成不可变 JSON 快照；它不是第二份物料数据库。
"""

from __future__ import annotations

import json
import threading
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
