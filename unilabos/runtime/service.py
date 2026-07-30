"""Generic OS RuntimeService: source compile, dispatch, and durable projections."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Protocol

from unilabos.runtime.estimated_timeline import (
    EstimatedTimelineBuilder,
    ObservedGanttBuilder,
)
from unilabos.runtime.event_store import SQLiteEventJournal
from unilabos.runtime.workflow_store import (
    WorkflowDocumentStore,
    WorkflowRevisionConflict,
)
from unilabos.scheduler.dag_model import DagValidationError, NodeState, TaskDag
from unilabos.scheduler.dag_wire import serialize_task_dag
from unilabos.scheduler.python_fallback import (
    python_fallback_capabilities,
    validate_python_fallback_dag,
)
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

if TYPE_CHECKING:
    from unilabos.runtime.profile_loader import LoadedProfile


class RuntimeSchedule(Protocol):
    def on_job_status(self, callback: Any) -> None: ...

    def on_run_terminal(self, callback: Any) -> None: ...

    async def submit_dag(self, dag: TaskDag) -> Any: ...

    def get_run(self, task_id: str) -> Any | None: ...

    async def cancel_task(self, task_id: str, job_id: str = "") -> None: ...

    async def reconcile_run(
        self,
        run_id: str,
        decision: dict[str, str],
    ) -> dict[str, str]: ...

    def on_debug_event(self, callback: Any) -> None: ...

    async def debug_command(
        self,
        run_id: str,
        command: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


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
        workflow_store: WorkflowDocumentStore | None = None,
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
        self._memory_events: dict[str, list[dict[str, Any]]] = {}
        self._memory_sequence = 0
        self._workflow_store = workflow_store
        self._workflow: dict[str, Any] = {
            "definition": {"id": "quick-debug", "name": "Quick Debug"},
            "revision": {"id": "quick-debug-empty", "nodes": [], "edges": []},
        }
        self._schedule.on_job_status(self._on_job_status)
        on_run_terminal = getattr(self._schedule, "on_run_terminal", None)
        if callable(on_run_terminal):
            on_run_terminal(self._on_run_terminal)
        on_debug_event = getattr(self._schedule, "on_debug_event", None)
        if callable(on_debug_event):
            on_debug_event(self._on_debug_event)

    def get_capabilities(self) -> dict[str, Any]:
        """Describe the active Python engine without implying Layer-B support."""

        return python_fallback_capabilities()

    def replace_action_catalog(
        self,
        action_catalog: Mapping[str, Mapping[str, Any]],
    ) -> None:
        """Atomically replace contracts used by future validation/compilation."""

        self._action_catalog = {
            str(action_ref): dict(definition)
            for action_ref, definition in action_catalog.items()
        }

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

    def get_workflow(self, workflow_id: str | None = None) -> dict[str, Any] | None:
        if workflow_id is not None and self._workflow_store is not None:
            stored = self._workflow_store.load(workflow_id)
            if stored is not None:
                return self._revision_projection(stored)
        if workflow_id is not None:
            definition = self._workflow.get("definition")
            current_id = (
                str(definition.get("id") or "")
                if isinstance(definition, Mapping)
                else ""
            )
            if current_id != workflow_id:
                return None
        return self._workflow

    def save_workflow(
        self,
        revision_payload: dict[str, Any],
        *,
        expected_revision_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            revision = WorkflowRevision.model_validate(revision_payload)
            revision = revalidate_workflow_revision(revision)
            executable = materialize_action_contracts(
                revision,
                action_catalog=self._action_catalog,
            )
            dag = compile_workflow_revision(
                executable,
                task_id="workflow-validation",
                action_catalog=self._action_catalog,
            )
            validate_python_fallback_dag(dag)
        except (ValueError, WorkflowCompileError) as exc:
            raise DagValidationError(str(exc)) from exc
        if self._workflow_store is not None:
            try:
                revision = self._workflow_store.save(
                    revision,
                    expected_revision_id=expected_revision_id,
                )
            except WorkflowRevisionConflict:
                raise
        self.set_workflow_revision(revision)
        return self._revision_projection(revision)

    def validate_workflow(
        self,
        revision_payload: dict[str, Any],
        *,
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            revision = WorkflowRevision.model_validate(revision_payload)
            normalized = _normalize_workflow_parameters(revision, parameters)
            executable = materialize_action_contracts(
                revision,
                action_catalog=self._action_catalog,
            )
            dag = compile_workflow_revision(
                executable,
                task_id="workflow-validation",
                action_catalog=self._action_catalog,
                runtime_parameters=normalized,
            )
            validate_python_fallback_dag(dag)
        except (ValueError, DagValidationError, WorkflowCompileError) as exc:
            code, _, message = str(exc).partition(":")
            return {
                "valid": False,
                "issues": [
                    {
                        "code": code if message else "INVALID_WORKFLOW",
                        "message": message.strip() if message else str(exc),
                        "severity": "error",
                    }
                ],
            }
        return {
            "valid": True,
            "issues": [],
            "workflowRevisionHash": dag.workflow_revision_hash,
            "nodeCount": len(dag.nodes),
            "edgeCount": len(dag.edges),
        }

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
            validate_python_fallback_dag(dag)
        except WorkflowCompileError as exc:
            raise DagValidationError(f"{exc.code}: {exc}") from exc
        debug = body.get("debug")
        if debug is not None:
            if not isinstance(debug, Mapping):
                raise DagValidationError("debug must be an object")
            breakpoints = [str(item) for item in debug.get("breakpoints") or []]
            unknown = sorted(set(breakpoints) - set(dag.nodes))
            if unknown:
                raise DagValidationError(
                    f"UNKNOWN_BREAKPOINT_NODE: {unknown[0]}"
                )
            start_node_id = str(debug.get("start_node_id") or "")
            if start_node_id and start_node_id not in dag.nodes:
                raise DagValidationError(
                    f"UNKNOWN_START_NODE: {start_node_id}"
                )
            dag.debug = {
                "pause_on_start": bool(debug.get("pause_on_start", False)),
                "breakpoints": breakpoints,
                **(
                    {"start_node_id": start_node_id}
                    if start_node_id
                    else {}
                ),
            }

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
            self._append_runtime_event(
                run_id=run_id,
                event_type="run.submitted",
                payload={
                    "workflowRevisionHash": dag.workflow_revision_hash,
                },
            )

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
        return {
            "id": run_id,
            "status": "pending",
            "workflowRevisionHash": dag.workflow_revision_hash,
        }

    def get_run(self, run_id: str) -> dict[str, Any] | None:
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
                    result: dict[str, Any] = {
                        "id": run_id,
                        "status": "reconciling" if has_unknown else persisted_status,
                    }
                    if (
                        getattr(handle, "debug", None)
                        and handle.debug.get("enabled") is True
                    ):
                        result["debug"] = dict(handle.debug)
                    return result
                if status in {"completed", "failed", "cancelled"}:
                    # Live transport projection is not allowed to outrun the
                    # durable executor-owned terminal event.
                    status = "reconciling" if has_unknown else "running"
            self._set_status(run_id, status)
            result = {"id": run_id, "status": status}
            if (
                getattr(handle, "debug", None)
                and handle.debug.get("enabled") is True
            ):
                result["debug"] = dict(handle.debug)
            return result
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

    def get_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        public_names: bool = False,
    ) -> list[dict[str, Any]] | None:
        if self.get_run(run_id) is None:
            return None
        if self._journal is None:
            return [
                dict(event)
                for event in self._memory_events.get(run_id, [])
                if int(event["seq"]) > after_sequence
            ]
        return [
            {
                "seq": event.sequence,
                "runId": event.run_id,
                "type": (
                    self._public_event_type(event.type)
                    if public_names
                    else event.type
                ),
                "nodeId": event.node_id,
                "timestamp": event.timestamp,
                "payload": event.payload,
            }
            for event in self._journal.list_events(
                run_id,
                after_sequence=after_sequence,
            )
        ]

    def get_nodes(self, run_id: str) -> list[dict[str, Any]] | None:
        dag = self._load_dag(run_id)
        if dag is None:
            return None
        handle = self._active_runs.get(run_id) or self._schedule.get_run(run_id)
        result: list[dict[str, Any]] = []
        for node_id, node in dag.nodes.items():
            state: Any = (
                handle.node_states.get(node_id, NodeState.PENDING)
                if handle is not None
                else NodeState.PENDING
            )
            projection = (
                self._journal.load_node_projection(run_id, node_id)
                if self._journal is not None
                else None
            )
            if projection is not None:
                # The executor-owned journal is the durable source of truth.
                # Schedule transports can legitimately lag for OS-internal
                # control nodes (branch/join) and skipped branch nodes because
                # those nodes never produce a physical-device status message.
                # Never let that lag overwrite a committed terminal result.
                state = {
                    "succeeded": "success",
                    "failed": "failed",
                    "cancelled": "cancelled",
                    "skipped": "skipped",
                    "running": "running",
                }.get(projection.state, projection.state)
            result.append(
                {
                    "nodeId": node_id,
                    "sourceNodeId": node.source_node_id or node_id,
                    "nodeType": node.node_type,
                    "deviceId": node.device_id,
                    "action": node.action,
                    "state": state.value if isinstance(state, NodeState) else str(state),
                    "result": projection.result if projection is not None else {},
                    "attempt": projection.attempt if projection is not None else 0,
                }
            )
        return result

    def get_node(self, run_id: str, node_id: str) -> dict[str, Any] | None:
        nodes = self.get_nodes(run_id)
        if nodes is None:
            return None
        return next((node for node in nodes if node["nodeId"] == node_id), None)

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

    async def debug_command(
        self,
        run_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        if self.get_run(run_id) is None:
            raise KeyError(run_id)
        if self._schedule.get_run(run_id) is None:
            raise RuntimeConflictError("RUN_NOT_ACTIVE")
        command = str(body.get("command") or "")
        payload = body.get("payload")
        if payload is not None and not isinstance(payload, Mapping):
            raise DagValidationError("debug command payload must be an object")
        try:
            return await self._schedule.debug_command(
                run_id,
                command,
                dict(payload or {}),
            )
        except (RuntimeError, TimeoutError) as exc:
            raise RuntimeConflictError(str(exc)) from exc

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
        if projected is not None and projected["status"] in {
            "completed",
            "failed",
            "cancelled",
        }:
            return projected
        if result_status in {"completed", "failed", "cancelled"}:
            return {"id": run_id, "status": result_status}
        # The execution authority has durably resolved and released the
        # unknown fence.  A stale persisted run projection can still say
        # ``pending`` after a process restart because no live schedule handle
        # exists to advance that old run.  Do not let that unrelated
        # projection hide a successful reconcile acknowledgement.
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
        if payload is None and source_format == "workflow_revision_v2":
            payload = source.get("revision")
        if not _is_record(payload):
            raise DagValidationError("source.payload 必须是对象")
        if source_format in {"canonical_workflow_v2", "workflow_revision_v2"}:
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
            except ValueError as exc:
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
                    raise ValueError(
                        f"workflow dependency is missing: {name}"
                    ) from exc

            try:
                return profile.import_workflow_source(
                    payload,
                    parameters=dict(body.get("parameters") or {}),
                    resolver=resolve_dependency,
                    source_artifact=source.get("artifact"),
                )
            except ValueError as exc:
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
        feedback = data.get("feedback_data")
        return_info = data.get("return_info")
        status = str(data.get("status") or "")
        if isinstance(feedback, Mapping) and feedback:
            self._append_runtime_event(
                run_id=run_id,
                event_type="node.feedback",
                node_id=str(data.get("job_id") or "") or None,
                payload=dict(feedback),
            )
        if status in {"success", "failed", "cancelled"} and isinstance(
            return_info, Mapping
        ):
            self._append_runtime_event(
                run_id=run_id,
                event_type=(
                    "node.result" if status == "success" else "node.exception"
                ),
                node_id=str(data.get("job_id") or "") or None,
                payload=dict(return_info),
            )
        elif status in {"cancelled", "skipped"}:
            self._append_runtime_event(
                run_id=run_id,
                event_type=f"node.{status}",
                node_id=str(data.get("job_id") or "") or None,
                payload={},
            )

    def _on_run_terminal(self, data: dict[str, Any]) -> None:
        """Persist the terminal declaration authored by the OS executor."""

        run_id = str(data.get("run_id") or data.get("task_id") or "")
        terminal = str(data.get("status") or data.get("terminal") or "")
        if not run_id or terminal not in {"completed", "failed", "cancelled"}:
            return
        if self._journal is not None:
            if self._journal.load_run_submission(run_id) is None:
                return
            self._journal.record_run_terminal(run_id=run_id, terminal=terminal)
            return
        memory = self._memory_submissions.get(run_id)
        if memory is None:
            return
        if memory["status"] in {"completed", "failed", "cancelled"}:
            return
        memory["status"] = terminal
        self._append_runtime_event(
            run_id=run_id,
            event_type=f"run_{terminal}",
        )

    def _on_debug_event(self, data: dict[str, Any]) -> None:
        run_id = str(data.get("run_id") or "")
        event_type = str(data.get("type") or "debug.state_changed")
        payload = data.get("payload")
        self._append_runtime_event(
            run_id=run_id,
            event_type=event_type,
            node_id=(
                str(payload.get("pausedBeforeNodeId") or "") or None
                if isinstance(payload, Mapping)
                else None
            ),
            payload=dict(payload) if isinstance(payload, Mapping) else {},
        )

    def _append_runtime_event(
        self,
        *,
        run_id: str,
        event_type: str,
        node_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if not run_id:
            return
        if self._journal is not None:
            self._journal.append_runtime_event(
                run_id=run_id,
                event_type=event_type,
                node_id=node_id,
                payload=payload,
            )
            return
        self._memory_sequence += 1
        self._memory_events.setdefault(run_id, []).append(
            {
                "seq": self._memory_sequence,
                "runId": run_id,
                "type": event_type,
                "nodeId": node_id,
                "timestamp": 0.0,
                "payload": dict(payload or {}),
            }
        )

    @staticmethod
    def _public_event_type(event_type: str) -> str:
        aliases = {
            "run_submitted": "run.created",
            "run_status_changed": "run.status",
            "run_completed": "run.status",
            "run_failed": "run.status",
            "run_cancelled": "run.status",
            "node_started": "node.started",
            "node_succeeded": "node.result",
            "node_success": "node.result",
            "node_failed": "node.exception",
            "node_cancelled": "node.cancelled",
            "node_skipped": "node.skipped",
        }
        return aliases.get(event_type, event_type)

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
