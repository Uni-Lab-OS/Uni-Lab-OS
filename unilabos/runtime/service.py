"""Generic OS RuntimeService: source compile, dispatch, and durable projections."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, Protocol

from unilabos.runtime.estimated_timeline import (
    EstimatedTimelineBuilder,
    ObservedGanttBuilder,
)
from unilabos.runtime.event_store import SQLiteEventJournal
from unilabos.runtime.profile_loader import LoadedProfile, ProfileValidationError
from unilabos.scheduler.dag_model import DagValidationError, NodeState, TaskDag
from unilabos.scheduler.dag_wire import serialize_task_dag
from unilabos.scheduler.resource_lock import ResourceLockManager
from unilabos.workflow.bindings import binding_node_dependencies, matches_json_type
from unilabos.workflow.canonical import (
    WorkflowRevision,
    revalidate_workflow_revision,
)
from unilabos.workflow.dag_compile import (
    WorkflowCompileError,
    compile_workflow_revision,
    materialize_action_contracts,
)


class RuntimeSchedule(Protocol):
    def on_job_status(self, callback: Any) -> None: ...

    async def submit_dag(self, dag: TaskDag) -> Any: ...

    def get_run(self, task_id: str) -> Any | None: ...

    async def cancel_task(self, task_id: str, job_id: str = "") -> None: ...

    async def reconcile_run(
        self,
        run_id: str,
        decision: dict[str, str],
    ) -> dict[str, str]: ...


class RuntimeConflictError(RuntimeError):
    """A requested state transition conflicts with persisted physical truth."""


def _is_record(value: Any) -> bool:
    return isinstance(value, Mapping)


def _run_status(handle: Any) -> str:
    if getattr(handle, "dispatch_state", "") == "unknown":
        return "dispatch_unknown"
    states = list(handle.node_states.values())
    if "reconciling" in states:
        return "reconciling"
    if "cancel_requested" in states:
        return "cancel_requested"
    if not handle.finished:
        return "running" if NodeState.RUNNING in states else "pending"
    if NodeState.FAILED in states:
        return "failed"
    if NodeState.CANCELLED in states:
        return "cancelled"
    return "completed"


def _normalize_workflow_parameters(
    revision: WorkflowRevision,
    supplied: Any,
) -> dict[str, Any]:
    if supplied is None:
        supplied = {}
    if not isinstance(supplied, Mapping):
        raise DagValidationError("workflow parameters must be an object")
    values = dict(supplied)
    if revision.parameters is None:
        return values
    contracts = {parameter.name: parameter for parameter in revision.parameters}
    unknown = sorted(set(values) - set(contracts))
    if unknown:
        raise DagValidationError(
            "UNKNOWN_WORKFLOW_PARAMETER: "
            f"workflow does not declare parameter {unknown[0]!r}"
        )
    normalized: dict[str, Any] = {}
    for parameter in revision.parameters:
        if parameter.name in values:
            value = values[parameter.name]
        elif parameter.required:
            raise DagValidationError(
                "MISSING_WORKFLOW_PARAMETER: "
                f"required workflow parameter {parameter.name!r} is missing"
            )
        else:
            value = parameter.default
        if not matches_json_type(value, parameter.type):
            raise DagValidationError(
                "WORKFLOW_PARAMETER_TYPE_MISMATCH: "
                f"parameter {parameter.name!r} expected {parameter.type!r}, "
                f"got {type(value).__name__}"
            )
        normalized[parameter.name] = value
    return normalized


class RuntimeService:
    """The sole generic entry from source submission to executable TaskDag."""

    def __init__(
        self,
        schedule: RuntimeSchedule,
        *,
        journal: SQLiteEventJournal | None = None,
        action_catalog: Mapping[str, Mapping[str, Any]] | None = None,
        profiles: Mapping[str, LoadedProfile] | None = None,
        resource_lock_manager: ResourceLockManager | None = None,
    ) -> None:
        self._schedule = schedule
        self._journal = journal
        self._profiles = dict(profiles or {})
        self._action_catalog: dict[str, Mapping[str, Any]] = {}
        for profile in self._profiles.values():
            self._action_catalog.update(profile.action_catalog)
        self._action_catalog.update(action_catalog or {})
        # Kept only for W2 composition compatibility.  Do not retain a bridge
        # lock manager: physical fences are resolved through RuntimeSchedule by
        # the execution OS authority.
        del resource_lock_manager
        self._active_runs: dict[str, Any] = {}
        self._memory_submissions: dict[str, dict[str, Any]] = {}
        self._workflow: dict[str, Any] = {
            "definition": {"id": "quick-debug", "name": "Quick Debug"},
            "revision": {"id": "quick-debug-empty", "nodes": [], "edges": []},
        }
        self._schedule.on_job_status(self._on_job_status)

    def set_workflow_revision(
        self,
        revision: WorkflowRevision,
        *,
        content_hash: str | None = None,
    ) -> None:
        """Install one Canonical revision and derive its read-only UI view."""

        revision = revalidate_workflow_revision(revision)
        if content_hash is not None and content_hash != revision.content_hash:
            raise ValueError(
                "workflow projection content_hash does not match Canonical revision"
            )
        self._workflow = self._revision_projection(revision)

    def get_workflow(self) -> dict[str, Any]:
        return self._workflow

    async def start_run(self, body: dict[str, Any]) -> dict[str, str]:
        revision = self._parse_source(body)
        normalized_parameters = _normalize_workflow_parameters(
            revision,
            body.get("parameters"),
        )
        run_id = uuid.uuid4().hex
        try:
            executable_revision = materialize_action_contracts(
                revision,
                action_catalog=self._action_catalog,
            )
            dag = compile_workflow_revision(
                executable_revision,
                task_id=run_id,
                action_catalog=self._action_catalog,
                runtime_parameters=normalized_parameters,
            )
        except WorkflowCompileError as exc:
            raise DagValidationError(f"{exc.code}: {exc}") from exc

        source = dict(body["source"])
        profile_ref = str(body.get("profile_ref") or "")
        compiled_dag = serialize_task_dag(dag)
        if self._journal is not None:
            self._journal.record_run_submission(
                run_id=run_id,
                source=source,
                profile_ref=profile_ref,
                compiled_dag=compiled_dag,
            )
        else:
            self._memory_submissions[run_id] = {
                "source": source,
                "profile_ref": profile_ref,
                "compiled_dag": compiled_dag,
                "status": "pending",
            }

        try:
            handle = await self._schedule.submit_dag(dag)
        except Exception:
            # A transport exception cannot prove whether the OS accepted the
            # TaskDag.  Preserve the run id and surface that uncertainty so a
            # retry cannot silently duplicate physical work.
            self._set_status(run_id, "dispatch_unknown")
            self.set_workflow_revision(
                executable_revision,
                content_hash=dag.workflow_revision_hash,
            )
            return {"id": run_id, "status": "dispatch_unknown"}
        self._active_runs[run_id] = handle
        self.set_workflow_revision(
            executable_revision,
            content_hash=dag.workflow_revision_hash,
        )
        return {"id": run_id, "status": "pending"}

    def get_run(self, run_id: str) -> dict[str, str] | None:
        submission = (
            self._journal.load_run_submission(run_id)
            if self._journal is not None
            else None
        )
        handle = self._active_runs.get(run_id) or self._schedule.get_run(run_id)
        if handle is not None:
            status = _run_status(handle)
            if self._journal is not None and submission is not None:
                persisted_status = submission.status
                has_unknown = self._journal.has_open_unknown_fence(run_id)
                if persisted_status in {"completed", "failed", "cancelled"}:
                    return {
                        "id": run_id,
                        "status": "reconciling" if has_unknown else persisted_status,
                    }
                if status in {"completed", "failed", "cancelled"}:
                    # Live transport projection is not allowed to outrun the
                    # durable executor-owned terminal event.
                    status = "reconciling" if has_unknown else "running"
            self._set_status(run_id, status)
            return {"id": run_id, "status": status}
        if submission is not None:
            status = submission.status
            if (
                status in {"completed", "failed", "cancelled"}
                and self._journal is not None
                and self._journal.has_open_unknown_fence(run_id)
            ):
                status = "reconciling"
            return {"id": run_id, "status": status}
        memory = self._memory_submissions.get(run_id)
        if memory is None:
            return None
        return {"id": run_id, "status": str(memory["status"])}

    def get_events(self, run_id: str) -> list[dict[str, Any]] | None:
        if self.get_run(run_id) is None:
            return None
        if self._journal is None:
            return []
        return [
            {
                "type": event.type,
                "nodeId": event.node_id,
                "timestamp": event.timestamp,
                "payload": event.payload,
            }
            for event in self._journal.list_events(run_id)
        ]

    def get_timeline(self, run_id: str) -> dict[str, Any] | None:
        dag = self._load_dag(run_id)
        if dag is None:
            return None
        estimated = EstimatedTimelineBuilder().build(dag)
        source_events: list[Any] = (
            list(self._journal.list_events(run_id)) if self._journal is not None else []
        )
        observed = ObservedGanttBuilder().build(source_events)
        return {
            "estimated": {
                "kind": "estimated",
                "label": "预计（未排程）",
                "resourceGuaranteeLabel": "无资源保证",
                "isResourceConstrained": False,
                "basis": estimated.basis,
                "workflowRevisionHash": estimated.workflow_revision_hash,
                "items": [
                    {
                        "nodeId": item.node_id,
                        "earliestStartOffsetS": item.earliest_start_offset_s,
                        "estimatedEndOffsetS": item.estimated_end_offset_s,
                    }
                    for item in estimated.items
                ],
            },
            "observed": {
                "source": "run_events",
                "items": [
                    {
                        "nodeId": item.node_id,
                        "startedAt": item.started_at,
                        "endedAt": item.ended_at,
                        "terminal": item.terminal,
                    }
                    for item in observed.items
                ],
            },
        }

    async def cancel_run(self, run_id: str) -> dict[str, str] | None:
        if self.get_run(run_id) is None:
            return None
        if self._schedule.get_run(run_id) is None:
            raise RuntimeConflictError(
                "run is not attached to the current OS session; reconcile first"
            )
        await self._schedule.cancel_task(run_id)
        self._set_status(run_id, "cancel_requested")
        return {"id": run_id, "status": "cancel_requested"}

    async def reconcile_run(
        self,
        run_id: str,
        body: dict[str, Any],
    ) -> dict[str, str]:
        if self.get_run(run_id) is None:
            raise KeyError(run_id)
        lease_id = str(body.get("lease_id") or "")
        resolution = str(body.get("resolution") or "")
        actor = str(body.get("actor") or "")
        reason = str(body.get("reason") or "")
        if resolution != "confirmed_safe" or not actor or not reason:
            raise RuntimeConflictError(
                "reconcile requires confirmed_safe, actor, and reason"
            )
        try:
            result = await self._schedule.reconcile_run(
                run_id,
                {
                    "lease_id": lease_id,
                    "resolution": resolution,
                    "actor": actor,
                    "reason": reason,
                },
            )
        except (RuntimeError, TimeoutError) as exc:
            raise RuntimeConflictError(str(exc)) from exc
        result_status = str(result.get("status") or "")
        if result_status not in {
            "reconciled",
            "completed",
            "failed",
            "cancelled",
        }:
            raise RuntimeConflictError(
                str(result.get("code") or "execution OS rejected reconcile")
            )
        projected = self.get_run(run_id)
        if projected is not None and projected["status"] != "reconciling":
            return projected
        if result_status in {"completed", "failed", "cancelled"}:
            return {"id": run_id, "status": result_status}
        return {"id": run_id, "status": "reconciled"}

    def _parse_source(self, body: dict[str, Any]) -> WorkflowRevision:
        if "workflow" in body:
            raise DagValidationError(
                "预编译 workflow/TaskDag 不允许进入 Runtime API；请提交 source"
            )
        source = body.get("source")
        if not _is_record(source):
            raise DagValidationError("source 必须是对象")
        source_format = str(source.get("format") or "")
        payload = source.get("payload")
        if not _is_record(payload):
            raise DagValidationError("source.payload 必须是对象")
        if source_format == "canonical_workflow_v2":
            try:
                return WorkflowRevision.model_validate(payload)
            except ValueError as exc:
                raise DagValidationError(str(exc)) from exc
        if source_format == "legacy_recipe":
            profile_ref = str(body.get("profile_ref") or "")
            profile = self._profiles.get(profile_ref)
            if profile is None:
                raise DagValidationError(
                    f"未安装或未加载 Profile: {profile_ref or '-'}"
                )
            try:
                return profile.import_legacy_source(
                    payload,
                    parameters=dict(body.get("parameters") or {}),
                )
            except ProfileValidationError as exc:
                raise DagValidationError(str(exc)) from exc
        if source_format == "profile_workflow":
            profile_ref = str(body.get("profile_ref") or "")
            profile = self._profiles.get(profile_ref)
            if profile is None:
                raise DagValidationError(
                    f"未安装或未加载 Profile: {profile_ref or '-'}"
                )
            dependencies = source.get("dependencies") or []
            if not isinstance(dependencies, list):
                raise DagValidationError("source.dependencies 必须是列表")
            by_name: dict[str, Mapping[str, Any]] = {}
            for dependency in dependencies:
                if not _is_record(dependency):
                    raise DagValidationError("workflow dependency 必须是对象")
                name = str(dependency.get("name") or "")
                if not name or name in by_name:
                    raise DagValidationError(
                        f"workflow dependency name 无效或重复: {name or '-'}"
                    )
                by_name[name] = dependency

            def resolve_dependency(name: str) -> Mapping[str, Any]:
                try:
                    return by_name[name]
                except KeyError as exc:
                    raise ProfileValidationError(
                        f"workflow dependency is missing: {name}"
                    ) from exc

            try:
                return profile.import_workflow_source(
                    payload,
                    parameters=dict(body.get("parameters") or {}),
                    resolver=resolve_dependency,
                    source_artifact=source.get("artifact"),
                )
            except ProfileValidationError as exc:
                raise DagValidationError(str(exc)) from exc
        raise DagValidationError(f"不支持的 source.format: {source_format or '-'}")

    def _load_dag(self, run_id: str) -> TaskDag | None:
        handle = self._active_runs.get(run_id) or self._schedule.get_run(run_id)
        if handle is not None:
            return handle.dag
        if self._journal is not None:
            submission = self._journal.load_run_submission(run_id)
            if submission is not None:
                return TaskDag.from_message(submission.compiled_dag)
        memory = self._memory_submissions.get(run_id)
        if memory is None:
            return None
        return TaskDag.from_message(memory["compiled_dag"])

    def _set_status(self, run_id: str, status: str) -> None:
        # Terminal status is committed atomically with the executor's one
        # run_terminal event.  Transport projections never author terminals.
        if status in {"completed", "failed", "cancelled"}:
            return
        if self._journal is not None:
            self._journal.update_run_status(run_id=run_id, status=status)
        memory = self._memory_submissions.get(run_id)
        if memory is not None:
            memory["status"] = status

    def _on_job_status(self, data: dict[str, Any]) -> None:
        run_id = str(data.get("task_id") or "")
        handle = self._active_runs.get(run_id)
        if handle is not None:
            self._set_status(run_id, _run_status(handle))

    @staticmethod
    def _revision_projection(
        revision: WorkflowRevision,
        *,
        content_hash: str | None = None,
    ) -> dict[str, Any]:
        nodes = []
        for invocation in revision.invocations:
            device_id, _, action = invocation.action_ref.rpartition(".")
            nodes.append(
                {
                    "id": invocation.node_id,
                    "label": invocation.name or action or invocation.node_id,
                    "deviceId": device_id,
                    "action": action,
                }
            )
        edges_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
        for edge in revision.control_edges:
            payload: dict[str, Any] = {
                "source": edge.source,
                "target": edge.target,
            }
            if edge.branch is not None:
                payload["branch"] = edge.branch
            edges_by_pair[(edge.source, edge.target)] = payload
        for edge in [
            *revision.data_edges,
            *revision.material_edges,
            *revision.constraint_edges,
        ]:
            edges_by_pair.setdefault(
                (edge.source, edge.target),
                {"source": edge.source, "target": edge.target},
            )
        for invocation in revision.invocations:
            for binding in invocation.input_bindings.values():
                for source_node_id in binding_node_dependencies(binding):
                    edges_by_pair.setdefault(
                        (source_node_id, invocation.node_id),
                        {
                            "source": source_node_id,
                            "target": invocation.node_id,
                        },
                    )
        edges = list(edges_by_pair.values())
        source_artifact = None
        if revision.source_artifact is not None:
            source_artifact = {
                "format": revision.source_artifact.format,
                "text": revision.source_artifact.text,
                "uri": revision.source_artifact.uri,
                "contentHash": revision.source_artifact.content_hash,
            }
        return {
            "definition": {
                "id": revision.workflow_id,
                "name": revision.workflow_id,
            },
            "revision": {
                "id": revision.revision_id,
                "contentHash": content_hash or revision.content_hash,
                # UI nodes/edges below are derived render projections only.
                # Whole-workflow execution must resubmit this lossless source
                # rather than reconstructing semantics from the visual view.
                "canonical": revision.model_dump(mode="json"),
                "nodes": nodes,
                "edges": edges,
                "sourceArtifact": source_artifact,
            },
        }
