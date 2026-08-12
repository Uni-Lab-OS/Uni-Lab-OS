"""调试启动缺失输入分析、建议与不可变覆盖的唯一深模块。"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from unilabos.workflow.execution_plan import ExecutionPlanBuilder
from unilabos.workflow.json_codec import clone_json, encode_json
from unilabos.workflow.store import StoreConflict
from unilabos.workflow.task_input import (
    PreparedTaskInput,
    ResourceSlotResolver,
    TaskInputError,
    prepare_task_input,
    resolve_task_input_value,
)
from unilabos.workflow.workflow_io import (
    handle_value_schema,
    resource_slot_passthrough_is_compatible,
    schema_contains_resource_slot,
)

MaterialCandidates = Callable[[], Sequence[Mapping[str, Any]]]


@dataclass(frozen=True)
class DebugLaunchDecision:
    """一次权威预检的公开结果与仅供创建事务使用的冻结计划。"""

    status: str
    preflight_hash: str
    requirements: list[dict[str, Any]]
    diagnostics: list[dict[str, Any]]
    prepared: PreparedTaskInput | None
    launch_overrides: list[dict[str, Any]]

    def to_public_dict(
        self, *, workflow_uuid: str, workflow_revision: int
    ) -> dict[str, Any]:
        """返回不泄漏内部 ``PreparedTaskInput`` 的传输投影。"""

        return {
            "workflow_uuid": workflow_uuid,
            "workflow_revision": workflow_revision,
            "status": self.status,
            "preflight_hash": self.preflight_hash,
            "requirements": clone_json(self.requirements),
            "diagnostics": clone_json(self.diagnostics),
            "launch_overrides": clone_json(self.launch_overrides),
        }


class DebugLaunchPreflight:
    """收敛 scope、缺失绑定、库存建议与任务级覆盖的公开领域接口。"""

    def __init__(
        self,
        *,
        material_resolver: ResourceSlotResolver | None = None,
        material_candidates: MaterialCandidates | None = None,
    ) -> None:
        self._material_resolver = material_resolver
        self._material_candidates = material_candidates

    def evaluate(
        self,
        *,
        graph: Mapping[str, Any],
        raw_input: Mapping[str, Any],
        start_node_uuid: str,
        breakpoint_node_uuids: Sequence[str],
        launch_overrides: Sequence[Mapping[str, Any]],
    ) -> DebugLaunchDecision:
        """分析一次调试启动，并只在全部要求被确认后返回冻结计划。

        ``graph`` 是同一事务读取的已应用图；``raw_input`` 是工作流根输入；
        start/breakpoint 是本次不可变配置；``launch_overrides`` 只绑定本次预检
        产生的 requirement。任何物料建议都不写 Inventory Authority。
        """

        graph_copy = clone_json(dict(graph))
        plan, jobs = ExecutionPlanBuilder().build(
            graph_copy,
            run_mode="step",
            target_node_uuid=None,
        )
        prepared = prepare_task_input(
            graph=graph_copy,
            raw_input=raw_input,
            execution_plan=plan,
            jobs=jobs,
            resource_resolver=self._material_resolver,
            allow_missing_required=True,
        )
        scoped = scope_debug_task_input(
            prepared,
            start_node_uuid=start_node_uuid,
            breakpoint_node_uuids=breakpoint_node_uuids,
        )
        reference_graph = clone_json(graph_copy)
        for node in reference_graph.get("nodes", []):
            if isinstance(node, dict):
                node["disabled"] = False
        reference_plan, _reference_jobs = ExecutionPlanBuilder().build(
            reference_graph,
            run_mode="step",
            target_node_uuid=None,
        )
        requirements = self._requirements(
            graph=graph_copy,
            scoped=scoped,
            reference_plan=reference_plan,
        )
        preflight_hash = self._preflight_hash(
            graph=graph_copy,
            start_node_uuid=start_node_uuid,
            breakpoint_node_uuids=breakpoint_node_uuids,
            requirements=requirements,
        )
        return self._apply_overrides(
            scoped=scoped,
            requirements=requirements,
            preflight_hash=preflight_hash,
            raw_overrides=launch_overrides,
        )

    def _requirements(
        self,
        *,
        graph: Mapping[str, Any],
        scoped: PreparedTaskInput,
        reference_plan: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """找出活动计划中没有值提供者的全部必填目标连接点。"""

        scoped_plan = scoped.execution_plan
        plan_nodes = _index(scoped_plan.get("nodes", []), "uuid")
        runtime_handles = _index(scoped_plan.get("handles", []), "uuid")
        template_handles = _index(graph.get("handle_templates", []), "uuid")
        snapshot_nodes = _index(graph.get("nodes", []), "uuid")
        incoming = _incoming_value_edges(scoped_plan.get("edges", []))
        reference_incoming = _incoming_value_edges(reference_plan.get("edges", []))
        requirements: list[dict[str, Any]] = []
        for runtime_uuid, runtime_handle in sorted(runtime_handles.items()):
            if runtime_handle.get("io_type") != "target" or not runtime_handle.get(
                "required"
            ):
                continue
            node_uuid = str(runtime_handle.get("node_uuid") or "")
            node = plan_nodes.get(node_uuid)
            if node is None:
                continue
            data_key = str(runtime_handle.get("data_key") or "").split("@@@")[-1]
            param = node.get("param")
            static_provider = (
                isinstance(param, Mapping) and param.get(data_key) is not None
            )
            if static_provider or incoming.get(runtime_uuid):
                continue
            template_handle_uuid = str(runtime_handle.get("template_handle_uuid") or "")
            template_handle = template_handles.get(template_handle_uuid)
            if template_handle is None:
                raise StoreConflict("debug target handle is outside workflow snapshot")
            schema = handle_value_schema(template_handle).to_dict()
            boundary_edges = reference_incoming.get(runtime_uuid, [])
            upstream_nodes = _upstream_nodes(
                boundary_edges,
                snapshot_nodes=snapshot_nodes,
            )
            reason = (
                "disabled_node"
                if any(item["disabled"] for item in upstream_nodes)
                else "start_scope"
            )
            requirement_id = _stable_id("requirement", node_uuid, template_handle_uuid)
            material = schema_contains_resource_slot(schema)
            requirement: dict[str, Any] = {
                "id": requirement_id,
                "kind": "material" if material else "value",
                "reason": reason,
                "required": True,
                "target": {
                    "node_uuid": node_uuid,
                    "node_name": str(
                        (snapshot_nodes.get(node_uuid) or {}).get("name") or node_uuid
                    ),
                    "handle_uuid": template_handle_uuid,
                    "data_key": data_key,
                    "display_name": str(
                        template_handle.get("display_name") or data_key
                    ),
                },
                "schema": schema,
                "upstream_nodes": upstream_nodes,
                "suggestions": [],
            }
            if material:
                allowed = _allowed_templates(schema)
                requirement["allowed_resource_template_uuids"] = allowed
                inferred_uuid, through = _infer_same_material(
                    boundary_edges=boundary_edges,
                    reference_plan=reference_plan,
                    graph=graph,
                )
                requirement["suggestions"] = self._material_suggestions(
                    requirement_id=requirement_id,
                    allowed_templates=allowed,
                    inferred_uuid=inferred_uuid,
                    through_node_uuids=through,
                )
            requirements.append(requirement)
        return requirements

    def _material_suggestions(
        self,
        *,
        requirement_id: str,
        allowed_templates: list[str],
        inferred_uuid: str | None,
        through_node_uuids: list[str],
    ) -> list[dict[str, Any]]:
        """从当前实验室库存事实生成兼容候选，不改写任何事实。"""

        if self._material_candidates is None:
            return []
        try:
            candidates = self._material_candidates()
        except Exception as exc:  # noqa: BLE001 - 权威读取失败必须关闭式暴露
            raise TaskInputError("调试启动无法读取物料权威") from exc
        suggestions: list[dict[str, Any]] = []
        for raw in candidates:
            if not isinstance(raw, Mapping):
                continue
            material_uuid = str(raw.get("uuid") or "")
            template_uuid = str(raw.get("resource_template_uuid") or "")
            if not material_uuid or (
                allowed_templates and template_uuid not in allowed_templates
            ):
                continue
            current_site = raw.get("current_site")
            site = (
                clone_json(dict(current_site))
                if isinstance(current_site, Mapping)
                else None
            )
            suggestions.append(
                {
                    "id": _stable_id("suggestion", requirement_id, material_uuid),
                    "material_uuid": material_uuid,
                    "material_name": str(raw.get("name") or material_uuid),
                    "resource_template_uuid": template_uuid,
                    "recommended": material_uuid == inferred_uuid,
                    "requires_confirmation": True,
                    "actual": {
                        "site": site,
                        "status": raw.get("inventory_status"),
                    },
                    "inferred_target": {
                        "kind": "same_material_passthrough"
                        if material_uuid == inferred_uuid
                        else "selected_inventory_candidate",
                        "through_node_uuids": through_node_uuids
                        if material_uuid == inferred_uuid
                        else [],
                        "site": None,
                        "status": None,
                    },
                }
            )
        suggestions.sort(
            key=lambda item: (
                not item["recommended"],
                item["material_name"],
                item["material_uuid"],
            )
        )
        return suggestions

    def _apply_overrides(
        self,
        *,
        scoped: PreparedTaskInput,
        requirements: list[dict[str, Any]],
        preflight_hash: str,
        raw_overrides: Sequence[Mapping[str, Any]],
    ) -> DebugLaunchDecision:
        """校验并冻结本次覆盖；缺项或非法项继续返回可引导诊断。"""

        requirement_by_id = {item["id"]: item for item in requirements}
        overrides_by_id: dict[str, Mapping[str, Any]] = {}
        diagnostics: list[dict[str, Any]] = []
        for raw in raw_overrides:
            if not isinstance(raw, Mapping):
                diagnostics.append(
                    {
                        "code": "launch_override_invalid",
                        "message": "调试启动覆盖必须是对象",
                    }
                )
                continue
            requirement_id = str(raw.get("requirement_id") or "")
            if (
                requirement_id not in requirement_by_id
                or requirement_id in overrides_by_id
            ):
                diagnostics.append(
                    {
                        "code": "launch_override_invalid",
                        "message": "调试启动覆盖引用未知或重复要求",
                        "requirement_id": requirement_id,
                    }
                )
                continue
            overrides_by_id[requirement_id] = raw

        plan = clone_json(scoped.execution_plan)
        jobs = clone_json(scoped.jobs)
        nodes = _index(plan.get("nodes", []), "uuid")
        jobs_by_node = _index(jobs, "workflow_node_uuid")
        frozen_overrides: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        for requirement in requirements:
            requirement_id = requirement["id"]
            raw = overrides_by_id.get(requirement_id)
            if raw is None:
                unresolved.append(requirement)
                continue
            if requirement["kind"] == "material" and raw.get("confirmed") is not True:
                diagnostics.append(
                    {
                        "code": "material_confirmation_required",
                        "message": "物料事实或推断建议必须由用户可见确认",
                        "requirement_id": requirement_id,
                    }
                )
                unresolved.append(requirement)
                continue
            try:
                value = resolve_task_input_value(
                    requirement["schema"],
                    raw.get("value"),
                    resource_resolver=self._material_resolver,
                )
            except TaskInputError:
                diagnostics.append(
                    {
                        "code": "launch_override_value_invalid",
                        "message": "补充值不符合目标 Schema 或当前库存事实",
                        "requirement_id": requirement_id,
                    }
                )
                unresolved.append(requirement)
                continue
            target = requirement["target"]
            node_uuid = target["node_uuid"]
            data_key = target["data_key"]
            node = nodes.get(node_uuid)
            job = jobs_by_node.get(node_uuid)
            if (
                node is None
                or job is None
                or not isinstance(node.get("param"), dict)
                or not isinstance(job.get("param"), dict)
            ):
                raise StoreConflict("debug launch override target is not an active job")
            node["param"][data_key] = clone_json(value)
            job["param"][data_key] = clone_json(value)
            frozen_overrides.append(
                {
                    "requirement_id": requirement_id,
                    "target_node_uuid": node_uuid,
                    "target_handle_uuid": target["handle_uuid"],
                    "value": clone_json(value),
                    "confirmed": raw.get("confirmed") is True,
                }
            )
        if diagnostics or unresolved:
            return DebugLaunchDecision(
                status="needs_input",
                preflight_hash=preflight_hash,
                requirements=clone_json(unresolved),
                diagnostics=diagnostics,
                prepared=None,
                launch_overrides=frozen_overrides,
            )
        plan["debug_launch_overrides"] = clone_json(frozen_overrides)
        return DebugLaunchDecision(
            status="ready",
            preflight_hash=preflight_hash,
            requirements=[],
            diagnostics=[],
            prepared=PreparedTaskInput(
                workflow_snapshot=scoped.workflow_snapshot,
                resolved_input=scoped.resolved_input,
                execution_plan=plan,
                jobs=jobs,
            ),
            launch_overrides=frozen_overrides,
        )

    @staticmethod
    def _preflight_hash(
        *,
        graph: Mapping[str, Any],
        start_node_uuid: str,
        breakpoint_node_uuids: Sequence[str],
        requirements: Sequence[Mapping[str, Any]],
    ) -> str:
        payload = {
            "workflow_revision": (graph.get("workflow") or {}).get("revision"),
            "start_node_uuid": start_node_uuid,
            "breakpoint_node_uuids": sorted(breakpoint_node_uuids),
            "requirements": list(requirements),
        }
        return (
            "sha256:" + hashlib.sha256(encode_json(payload, sort_keys=True)).hexdigest()
        )


def scope_debug_task_input(
    prepared: PreparedTaskInput,
    *,
    start_node_uuid: str,
    breakpoint_node_uuids: Sequence[str],
) -> PreparedTaskInput:
    """只保留从调试起点可达的活动子图，不对缺失值做隐式重接。"""

    plan = clone_json(prepared.execution_plan)
    nodes = [dict(node) for node in plan.get("nodes", [])]
    node_ids = {str(node.get("uuid") or "") for node in nodes}
    if start_node_uuid not in node_ids:
        raise StoreConflict("debug start node is not enabled and executable")
    snapshot_nodes = prepared.workflow_snapshot.get("nodes", [])
    enabled_snapshot_ids = {
        str(node.get("uuid") or "")
        for node in snapshot_nodes
        if isinstance(node, Mapping) and node.get("disabled") is not True
    }
    if any(
        node_uuid not in enabled_snapshot_ids for node_uuid in breakpoint_node_uuids
    ):
        raise StoreConflict("debug breakpoint node is not enabled")
    outgoing: dict[str, list[str]] = {}
    for edge in plan.get("edges", []):
        if not isinstance(edge, Mapping):
            continue
        outgoing.setdefault(str(edge.get("source_node_uuid") or ""), []).append(
            str(edge.get("target_node_uuid") or "")
        )
    reachable: set[str] = set()
    pending = [start_node_uuid]
    while pending:
        current = pending.pop()
        if current in reachable:
            continue
        reachable.add(current)
        pending.extend(outgoing.get(current, []))
    plan["nodes"] = [node for node in nodes if str(node.get("uuid")) in reachable]
    plan["edges"] = [
        edge
        for edge in plan.get("edges", [])
        if str(edge.get("source_node_uuid")) in reachable
        and str(edge.get("target_node_uuid")) in reachable
    ]
    plan["handles"] = [
        handle
        for handle in plan.get("handles", [])
        if str(handle.get("node_uuid")) in reachable
    ]
    jobs = [
        job for job in prepared.jobs if str(job.get("workflow_node_uuid")) in reachable
    ]
    if not jobs:
        raise StoreConflict("debug task has no reachable jobs")
    return PreparedTaskInput(
        workflow_snapshot=prepared.workflow_snapshot,
        resolved_input=prepared.resolved_input,
        execution_plan=plan,
        jobs=jobs,
    )


def _incoming_value_edges(raw_edges: Any) -> dict[str, list[Mapping[str, Any]]]:
    result: dict[str, list[Mapping[str, Any]]] = {}
    if not isinstance(raw_edges, list):
        return result
    for edge in raw_edges:
        if not isinstance(edge, Mapping) or edge.get("dependency_only") is True:
            continue
        result.setdefault(str(edge.get("target_handle_uuid") or ""), []).append(edge)
    return result


def _index(raw: Any, key: str) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, list):
        raise StoreConflict("debug launch projection must be an object list")
    result: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            raise StoreConflict("debug launch projection contains a non-object")
        identity = str(item.get(key) or "")
        if not identity or identity in result:
            raise StoreConflict(
                "debug launch projection identity is missing or duplicated"
            )
        result[identity] = dict(item)
    return result


def _upstream_nodes(
    edges: Sequence[Mapping[str, Any]],
    *,
    snapshot_nodes: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for edge in edges:
        node_uuid = str(edge.get("source_node_uuid") or "")
        node = snapshot_nodes.get(node_uuid)
        if node is None:
            continue
        item = {
            "node_uuid": node_uuid,
            "node_name": str(node.get("name") or node_uuid),
            "disabled": node.get("disabled") is True,
        }
        if item not in result:
            result.append(item)
    return result


def _allowed_templates(schema: Mapping[str, Any]) -> list[str]:
    if "$slot" in schema:
        raw = schema.get("allowed_resource_template_uuids", [])
        return [str(value) for value in raw] if isinstance(raw, list) else []
    members = schema.get("anyOf")
    if isinstance(members, list):
        for member in members:
            if isinstance(member, Mapping) and "$slot" in member:
                return _allowed_templates(member)
    return []


def _infer_same_material(
    *,
    boundary_edges: Sequence[Mapping[str, Any]],
    reference_plan: Mapping[str, Any],
    graph: Mapping[str, Any],
) -> tuple[str | None, list[str]]:
    """沿显式同物料 passthrough 合同反向寻找冻结输入物料身份。"""

    if len(boundary_edges) != 1:
        return None, []
    plan_nodes = _index(reference_plan.get("nodes", []), "uuid")
    plan_handles = _index(reference_plan.get("handles", []), "uuid")
    snapshot_nodes = _index(graph.get("nodes", []), "uuid")
    template_handles = _index(graph.get("handle_templates", []), "uuid")
    incoming = _incoming_value_edges(reference_plan.get("edges", []))
    edge = boundary_edges[0]
    through: list[str] = []
    visited: set[str] = set()
    while True:
        node_uuid = str(edge.get("source_node_uuid") or "")
        if not node_uuid or node_uuid in visited:
            return None, []
        visited.add(node_uuid)
        node = snapshot_nodes.get(node_uuid)
        plan_node = plan_nodes.get(node_uuid)
        source_runtime = plan_handles.get(str(edge.get("source_handle_uuid") or ""))
        if node is None or plan_node is None or source_runtime is None:
            return None, []
        source_template_uuid = str(source_runtime.get("template_handle_uuid") or "")
        unilab = (
            node.get("meta_data", {}).get("unilab", {})
            if isinstance(node.get("meta_data"), Mapping)
            else {}
        )
        passthroughs = (
            unilab.get("material_passthrough_handles", {})
            if isinstance(unilab, Mapping)
            else {}
        )
        target_template_uuid = (
            passthroughs.get(source_template_uuid)
            if isinstance(passthroughs, Mapping)
            else None
        )
        source_template = template_handles.get(source_template_uuid)
        target_template = template_handles.get(str(target_template_uuid or ""))
        if (
            source_template is None
            or target_template is None
            or not resource_slot_passthrough_is_compatible(
                handle_value_schema(target_template),
                handle_value_schema(source_template),
            )
        ):
            return None, []
        through.append(node_uuid)
        target_runtime = next(
            (
                handle
                for handle in plan_handles.values()
                if handle.get("node_uuid") == node_uuid
                and handle.get("template_handle_uuid") == target_template_uuid
            ),
            None,
        )
        if target_runtime is None:
            return None, []
        data_key = str(target_runtime.get("data_key") or "").split("@@@")[-1]
        param = plan_node.get("param")
        value = param.get(data_key) if isinstance(param, Mapping) else None
        if isinstance(value, Mapping) and isinstance(value.get("uuid"), str):
            return str(value["uuid"]), through
        providers = incoming.get(str(target_runtime.get("uuid") or ""), [])
        if len(providers) != 1:
            return None, []
        edge = providers[0]


def _stable_id(*parts: str) -> str:
    payload = ":".join(parts).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


__all__ = [
    "DebugLaunchDecision",
    "DebugLaunchPreflight",
    "MaterialCandidates",
    "scope_debug_task_input",
]
