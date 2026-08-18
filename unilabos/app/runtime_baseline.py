"""冻结并暴露当前 OS 实际选择的物料基线。

本模块只拥有当前 OS 代已解析图的不可变投影；PostgreSQL 增量对齐仍由正式
Backend API 独占，OS 不直接访问数据库。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from unilabos.app.scheduler.inventory.resource_graph_bootstrap import (
    ResourceGraphBootstrapError,
    _compile_projection,
    _template_aliases,
    _with_implicit_host_executor,
)
from unilabos.registry.template_snapshot import RegistryTemplateSnapshot


class RuntimeBaselineError(RuntimeError):
    """当前 OS selected graph 无法生成唯一、明确的运行前复位基线。"""


_lock = threading.RLock()
_baseline: dict[str, Any] | None = None
_baseline_error: str | None = None


def freeze_runtime_baseline(
    *,
    resource_tree_set: Any,
    registry: Any,
    source_id: str,
    graph_fingerprint: str,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """编译并冻结当前 OS 代实际解析的 selected graph。"""

    source_name = Path(str(source_id or "").strip()).name
    if not source_name:
        raise RuntimeBaselineError("selected graph 来源身份不完整")
    try:
        raw_trees = resource_tree_set.dump()
        normalized_graph_fingerprint = _graph_fingerprint(
            graph_fingerprint,
            raw_trees,
        )
        registry_snapshot = RegistryTemplateSnapshot.from_registry(registry)
        raw_trees = _with_implicit_host_executor(
            raw_trees,
            registry_snapshot=registry_snapshot,
            source_name=source_name,
        )
        projection = _compile_projection(
            raw_trees,
            source_name,
            _template_aliases(registry_snapshot),
        )
    except (AttributeError, TypeError, ValueError, ResourceGraphBootstrapError) as error:
        raise RuntimeBaselineError(
            f"selected graph baseline is invalid: {error}"
        ) from error

    source_id_by_material_uuid = {
        str(material["uuid"]): str(material["meta_data"]["source_node_id"])
        for material in projection["materials"]
    }
    position_by_material_uuid = {
        str(position["material_uuid"]): position
        for position in projection["relative_positions"]
    }
    materials: list[dict[str, Any]] = []
    for material in projection["materials"]:
        material_uuid = str(material["uuid"])
        position = position_by_material_uuid.get(material_uuid)
        materials.append(
            {
                "material_uuid": material_uuid,
                "source_node_id": source_id_by_material_uuid[material_uuid],
                "template_name": material["template_name"],
                "parent_source_node_id": source_id_by_material_uuid.get(
                    str(material.get("parent_uuid") or "")
                ),
                "class": material["class"],
                "type": material["type"],
                "barcode": material["barcode"],
                "name": material["name"],
                "description": material["description"],
                "config": copy.deepcopy(material["config"]),
                "relative_position": _position_payload(position),
            }
        )
    sites = _site_payloads(
        projection,
        source_id_by_material_uuid,
        registry_snapshot,
    )
    detached = {
        "schema_version": "unilab.runtime-baseline/v1",
        "source_graph_id": source_name,
        "selected_graph_fingerprint": normalized_graph_fingerprint,
        "registry_fingerprint": registry_snapshot.fingerprint,
        "materials": sorted(materials, key=lambda item: item["source_node_id"]),
        "sites": sorted(
            sites,
            key=lambda item: (item["owner_source_node_id"], item["name"]),
        ),
    }
    baseline_fingerprint = "sha256:" + hashlib.sha256(
        json.dumps(
            detached,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    frozen = {**detached, "baseline_fingerprint": baseline_fingerprint}
    if output_path:
        _write_runtime_baseline(Path(output_path), frozen)
    with _lock:
        global _baseline, _baseline_error
        _baseline = copy.deepcopy(frozen)
        _baseline_error = None
    return copy.deepcopy(frozen)


def freeze_runtime_baseline_if_valid(
    *,
    resource_tree_set: Any,
    registry: Any,
    source_id: str,
    graph_fingerprint: str,
    output_path: str | Path | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """尽力冻结复位基线，但不让基线歧义阻断 OS 的正常设备启动。"""

    global _baseline, _baseline_error
    try:
        baseline = freeze_runtime_baseline(
            resource_tree_set=resource_tree_set,
            registry=registry,
            source_id=source_id,
            graph_fingerprint=graph_fingerprint,
            output_path=output_path,
        )
    except Exception as error:  # noqa: BLE001 - 基线是可选复位能力，不能阻断 OS 启动。
        message = str(error)
        target = Path(output_path) if output_path else None
        if target is not None:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
        with _lock:
            _baseline = None
            _baseline_error = message
        return None, message
    return baseline, None


def _write_runtime_baseline(target: Path, baseline: Mapping[str, Any]) -> None:
    """将当前 OS 代基线原子发布给 Workspace Host。"""

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(
            baseline,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(target)


def get_runtime_baseline() -> dict[str, Any]:
    """返回当前 OS 代已冻结基线的隔离副本。"""

    with _lock:
        if _baseline is None:
            raise RuntimeBaselineError(
                _baseline_error or "selected graph baseline is not ready"
            )
        return copy.deepcopy(_baseline)


def _graph_fingerprint(graph_fingerprint: str, raw_trees: object) -> str:
    """优先采用 Workspace Host 指纹，否则按实际解析输入生成内容指纹。"""

    normalized = str(graph_fingerprint or "").strip()
    if not normalized:
        try:
            normalized = hashlib.sha256(
                json.dumps(
                    raw_trees,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        except (TypeError, ValueError) as error:
            raise RuntimeBaselineError("selected graph 无法生成稳定内容指纹") from error
    if not normalized.startswith("sha256:"):
        normalized = f"sha256:{normalized}"
    return normalized


def _position_payload(position: Mapping[str, Any] | None) -> dict[str, float] | None:
    if position is None:
        return None
    return {
        key: float(position[key])
        for key in (
            "position_x",
            "position_y",
            "position_z",
            "depth",
            "length",
            "width",
            "scale_x",
            "scale_y",
            "scale_z",
            "rotation_x",
            "rotation_y",
            "rotation_z",
        )
    }


def _site_payloads(
    projection: Mapping[str, Any],
    source_id_by_material_uuid: Mapping[str, str],
    registry_snapshot: RegistryTemplateSnapshot,
) -> list[dict[str, Any]]:
    """以模板正式 Site 为主，合并 selected graph 中明确的占用事实。"""

    definitions_by_template = {
        str(definition["id"]): list(definition.get("available_sites") or [])
        for definition in registry_snapshot.detached_definitions()
    }
    projected_by_owner_name: dict[tuple[str, str], Mapping[str, Any]] = {}
    for site in projection["sites"]:
        owner_uuid = str(site["material_uuid"])
        identity = (owner_uuid, str(site["name"]).strip().casefold())
        if identity in projected_by_owner_name:
            raise RuntimeBaselineError(
                f"selected graph Site 名称不唯一: {site['name']}"
            )
        projected_by_owner_name[identity] = site
    sites: list[dict[str, Any]] = []
    consumed: set[tuple[str, str]] = set()
    for material in projection["materials"]:
        owner_uuid = str(material["uuid"])
        owner_node_id = source_id_by_material_uuid[owner_uuid]
        definitions = definitions_by_template.get(str(material["template_name"]), [])
        for sort_order, definition in enumerate(definitions):
            name = str(definition["label"])
            identity = (owner_uuid, name.casefold())
            projected = projected_by_owner_name.get(identity)
            consumed.add(identity)
            sites.append(
                _site_payload(
                    owner_node_id,
                    name,
                    source_id_by_material_uuid,
                    projected,
                    definition={**copy.deepcopy(definition), "sort_order": sort_order},
                )
            )
    for identity, projected in projected_by_owner_name.items():
        if identity in consumed:
            continue
        owner_uuid, _ = identity
        sites.append(
            _site_payload(
                source_id_by_material_uuid[owner_uuid],
                str(projected["name"]),
                source_id_by_material_uuid,
                projected,
                definition=None,
            )
        )
    return sites


def _site_payload(
    owner_node_id: str,
    name: str,
    source_id_by_material_uuid: Mapping[str, str],
    projected: Mapping[str, Any] | None,
    *,
    definition: Mapping[str, Any] | None,
) -> dict[str, Any]:
    occupant_uuid = (
        str(projected.get("occupied_material_uuid") or "")
        if projected is not None
        else ""
    )
    return {
        "source_site_id": f"{owner_node_id}:{name}",
        "owner_source_node_id": owner_node_id,
        "name": name,
        "occupied_source_node_id": source_id_by_material_uuid.get(occupant_uuid),
        "definition": copy.deepcopy(definition),
    }
