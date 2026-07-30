"""Application service for the local Backend-shaped Workflow authority."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import threading
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Protocol, Tuple
from uuid import uuid4

from pydantic import ValidationError

from unilabos.workflow.models import (
    CandidateCompilation,
    WorkflowEdgeWrite,
    WorkflowNodeWrite,
    validate_uuid,
)
from unilabos.workflow.store import (
    StoreConflict,
    StoreNotFound,
    StoreRevisionConflict,
    WorkflowStore,
    utc_now,
)

_ERRORS = {
    "invalid_input": (400, "提交内容格式不正确"),
    "not_found": (404, "请求的资源不存在"),
    "conflict": (409, "资源已发生冲突，请刷新后重试"),
    "workflow_not_found": (404, "工作流不存在或已被删除"),
    "draft_hash_conflict": (
        409,
        "草稿已被其他程序修改，请查看差异后再保存",
    ),
    "workflow_revision_conflict": (
        409,
        "工作流已在其他位置更新，请刷新并重新确认本次修改",
    ),
    "candidate_hash_conflict": (
        409,
        "预览结果已变化，请重新检查 DAG 和源码差异",
    ),
    "template_catalog_conflict": (
        409,
        "设备动作模板已更新，请重新编译并检查工作流",
    ),
    "candidate_not_ready": (409, "当前草稿尚未生成可应用的工作流"),
    "draft_invalid": (422, "草稿存在错误，修复后才能应用"),
    "candidate_invalid": (422, "工作流校验失败，请检查节点、连线和输入输出"),
    "template_catalog_unavailable": (
        503,
        "设备动作模板暂不可用，请稍后重试",
    ),
    "internal_error": (500, "本地工作流服务出现错误，请重试或查看日志"),
}
_HASH_TOKEN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_NO_EXPECTED_HASH = object()


class WorkflowError(RuntimeError):
    """Stable frontend-facing Workflow failure."""

    def __init__(self, code: str):
        status, message = _ERRORS[code]
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class WorkflowConflict(WorkflowError):
    pass


class AuthoringCompiler(Protocol):
    compiler_version: str
    template_catalog_fingerprint: str

    def compile(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        python_source: str,
        source_uri: str,
        applied_graph: Dict[str, Any],
    ) -> CandidateCompilation: ...


def _sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _mtime_rfc3339(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


class WorkflowService:
    """Coordinates SQLite facts, package Draft files, and compiler state."""

    def __init__(
        self,
        store: WorkflowStore,
        *,
        compiler: Optional[AuthoringCompiler] = None,
    ):
        self.store = store
        self.compiler = compiler
        self._locks_guard = threading.Lock()
        self._authoring_locks: Dict[str, threading.RLock] = {}

    # Workflow and Graph -------------------------------------------------

    def create_workflow(
        self,
        *,
        name: str,
        tags: List[Any],
        description: Optional[str],
        meta_data: Dict[str, Any],
        workflow_uuid: Optional[str] = None,
    ) -> Dict[str, Any]:
        try:
            name = name.strip()
            if not name:
                raise ValueError("workflow name must not be blank")
            identity = validate_uuid(workflow_uuid or str(uuid4()))
            public_meta_data = dict(meta_data)
            public_meta_data.pop("unilab", None)
            return self.store.create_workflow(
                workflow_uuid=identity,
                name=name,
                tags=tags,
                description=self._optional_text(description),
                meta_data=public_meta_data,
            )
        except (ValueError, ValidationError):
            raise WorkflowError("invalid_input") from None
        except StoreConflict:
            raise WorkflowConflict("conflict") from None

    def get_workflow(self, workflow_uuid: str) -> Dict[str, Any]:
        try:
            return self.store.get_workflow(validate_uuid(workflow_uuid))
        except (ValueError, StoreNotFound):
            raise WorkflowError("not_found") from None

    def list_workflows(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        name: str = "",
    ) -> Dict[str, Any]:
        page, page_size = self._normalize_page(page, page_size)
        return self.store.list_workflows(page=page, page_size=page_size, name=name)

    def update_workflow(
        self,
        workflow_uuid: str,
        *,
        name: str,
        tags: List[Any],
        description: Optional[str],
        meta_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        current = self.get_workflow(workflow_uuid)
        identity = current["uuid"]
        with self._authoring_lock(identity):
            current = self.get_workflow(identity)
            name = name.strip()
            if not name:
                raise WorkflowError("invalid_input")
            public_meta_data = dict(meta_data)
            public_meta_data.pop("unilab", None)
            if "unilab" in current["meta_data"]:
                public_meta_data["unilab"] = current["meta_data"]["unilab"]
            return self.store.update_workflow(
                identity,
                name=name,
                tags=tags,
                description=self._optional_text(description),
                meta_data=public_meta_data,
            )

    def delete_workflow(self, workflow_uuid: str) -> None:
        identity = self.get_workflow(workflow_uuid)["uuid"]
        with self._authoring_lock(identity):
            self.get_workflow(identity)
            self.store.delete_workflow(identity)

    def get_graph(self, workflow_uuid: str) -> Dict[str, Any]:
        identity = self.get_workflow(workflow_uuid)["uuid"]
        return self.store.get_graph(identity)

    def save_graph(
        self,
        workflow_uuid: str,
        *,
        revision: int,
        nodes: List[WorkflowNodeWrite | Dict[str, Any]],
        edges: List[WorkflowEdgeWrite | Dict[str, Any]],
    ) -> Dict[str, Any]:
        identity = self.get_workflow(workflow_uuid)["uuid"]
        with self._authoring_lock(identity):
            self.get_workflow(identity)
            try:
                node_values = [
                    item
                    if isinstance(item, WorkflowNodeWrite)
                    else WorkflowNodeWrite.model_validate(item)
                    for item in nodes
                ]
                edge_values = [
                    item
                    if isinstance(item, WorkflowEdgeWrite)
                    else WorkflowEdgeWrite.model_validate(item)
                    for item in edges
                ]
                return self.store.save_graph(
                    identity,
                    revision=revision,
                    nodes=node_values,
                    edges=edge_values,
                    protect_reserved_metadata=True,
                )
            except ValidationError:
                raise WorkflowError("invalid_input") from None
            except StoreRevisionConflict:
                raise WorkflowConflict("conflict") from None
            except StoreNotFound:
                raise WorkflowError("not_found") from None
            except StoreConflict:
                raise WorkflowError("invalid_input") from None

    # WorkflowTask and WorkflowNodeJob ----------------------------------

    def create_workflow_task(
        self,
        *,
        workflow_uuid: str,
        run_mode: str,
        target_node_uuid: Optional[str],
        input_value: Dict[str, Any],
        description: Optional[str],
        meta_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        workflow_uuid = self.get_workflow(workflow_uuid)["uuid"]
        run_mode = run_mode.strip().lower() or "normal"
        if run_mode not in {"normal", "step", "single_node"}:
            raise WorkflowError("invalid_input")
        if target_node_uuid is not None:
            try:
                target_node_uuid = validate_uuid(target_node_uuid)
            except ValueError:
                raise WorkflowError("invalid_input") from None
        # P0-2 已冻结合同；生产 schema/compiler 属于 Phase 02。本阶段镜像
        # Backend baseline 的空 Task input，不提前持久化未实现的解释。
        if input_value:
            raise WorkflowError("invalid_input")
        description = self._optional_text(description)
        try:
            return self.store.create_task_with_jobs(
                workflow_uuid=workflow_uuid,
                task_uuid=str(uuid4()),
                run_mode=run_mode,
                target_node_uuid=target_node_uuid,
                input_value={},
                description=description,
                meta_data=meta_data,
                plan_builder=lambda graph: self._build_execution_plan(
                    graph,
                    run_mode=run_mode,
                    target_node_uuid=target_node_uuid,
                ),
            )
        except StoreConflict:
            raise WorkflowError("invalid_input") from None

    def get_workflow_task(self, task_uuid: str) -> Dict[str, Any]:
        try:
            identity = validate_uuid(task_uuid)
            return self.store.get_task(identity)
        except (ValueError, StoreNotFound):
            raise WorkflowError("not_found") from None

    def list_workflow_tasks(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        workflow_uuid: Optional[str] = None,
        status: str = "",
        cleanup_status: str = "",
    ) -> Dict[str, Any]:
        page, page_size = self._normalize_page(page, page_size)
        if workflow_uuid is not None:
            try:
                workflow_uuid = validate_uuid(workflow_uuid)
            except ValueError:
                raise WorkflowError("invalid_input") from None
        status = status.strip().lower()
        cleanup_status = cleanup_status.strip().lower()
        if status and status not in {
            "pending",
            "running",
            "canceling",
            "succeeded",
            "failed",
            "canceled",
            "timeout",
        }:
            raise WorkflowError("invalid_input")
        if cleanup_status and cleanup_status not in {
            "none",
            "pending",
            "canceling",
            "settled",
            "requires_attention",
        }:
            raise WorkflowError("invalid_input")
        return self.store.list_tasks(
            page=page,
            page_size=page_size,
            workflow_uuid=workflow_uuid,
            status=status,
            cleanup_status=cleanup_status,
        )

    def list_workflow_node_jobs(self, task_uuid: str) -> List[Dict[str, Any]]:
        identity = self.get_workflow_task(task_uuid)["uuid"]
        return self.store.list_jobs(identity)

    def get_workflow_node_job(self, job_uuid: str) -> Dict[str, Any]:
        try:
            identity = validate_uuid(job_uuid)
            return self.store.get_job(identity)
        except (ValueError, StoreNotFound):
            raise WorkflowError("not_found") from None

    def _build_execution_plan(
        self,
        graph: Dict[str, Any],
        *,
        run_mode: str,
        target_node_uuid: Optional[str],
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        templates = {
            template["uuid"]: template for template in graph.get("node_templates", [])
        }
        handles = {
            handle["uuid"]: handle for handle in graph.get("handle_templates", [])
        }
        graph_nodes = graph["nodes"]
        graph_edges = graph["edges"]
        if run_mode == "single_node" and target_node_uuid is not None:
            selected_node = next(
                (node for node in graph_nodes if node["uuid"] == target_node_uuid),
                None,
            )
            if selected_node is None or selected_node["disabled"]:
                raise StoreConflict("single_node target is not enabled")
            graph_nodes = [selected_node]
            graph_edges = []

        def stable_key(node_uuid: str) -> Tuple[str, str]:
            node = enabled[node_uuid]
            return str(node.get("create_time") or ""), node_uuid

        enabled: Dict[str, Dict[str, Any]] = {}
        node_kinds: Dict[str, str] = {}
        for node in graph_nodes:
            template = templates.get(node.get("workflow_node_template_uuid"))
            raw_kind = (
                template.get("node_type") if template is not None else node["type"]
            )
            kind = self._executor_kind(raw_kind)
            if node["disabled"] or kind == "group":
                continue
            enabled[node["uuid"]] = node
            node_kinds[node["uuid"]] = kind

        indegree = {node_uuid: 0 for node_uuid in enabled}
        outgoing: Dict[str, List[str]] = defaultdict(list)
        planned_edges: List[Dict[str, Any]] = []
        for edge in graph_edges:
            source = edge["source_node_uuid"]
            target = edge["target_node_uuid"]
            if source not in enabled or target not in enabled:
                continue
            source_handle = handles.get(edge["source_handle_uuid"])
            target_handle = handles.get(edge["target_handle_uuid"])
            if source_handle is None or target_handle is None:
                raise StoreConflict("workflow edge references a missing handle")
            outgoing[source].append(target)
            indegree[target] += 1
            planned_edge = {
                "uuid": edge["uuid"],
                "source_node_uuid": source,
                "target_node_uuid": target,
                "source_handle_uuid": edge["source_handle_uuid"],
                "target_handle_uuid": edge["target_handle_uuid"],
                "source_data_key": self._handle_data_key(source_handle),
                "target_data_key": self._handle_data_key(target_handle),
                "source_type": str(source_handle.get("type") or "").strip(),
                "target_type": str(target_handle.get("type") or "").strip(),
            }
            if self._dependency_only(source_handle):
                planned_edge["dependency_only"] = True
            planned_edges.append(planned_edge)

        available = sorted(
            (node_uuid for node_uuid, degree in indegree.items() if degree == 0),
            key=stable_key,
        )
        ordered: List[str] = []
        while available:
            node_uuid = available.pop(0)
            ordered.append(node_uuid)
            for target in outgoing[node_uuid]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    available.append(target)
                    available.sort(key=stable_key)
        if len(ordered) != len(enabled):
            raise StoreConflict("workflow graph contains a cycle")
        if run_mode == "single_node":
            if target_node_uuid is None:
                if not ordered:
                    raise StoreConflict("workflow has no enabled nodes")
                target_node_uuid = ordered[0]
            if target_node_uuid not in enabled:
                raise StoreConflict("single_node target is not enabled")
            ordered = [target_node_uuid]
            enabled = {target_node_uuid: enabled[target_node_uuid]}
            planned_edges = []

        planned_nodes: List[Dict[str, Any]] = []
        jobs: List[Dict[str, Any]] = []
        for index, node_uuid in enumerate(ordered):
            node = enabled[node_uuid]
            kind = node_kinds[node_uuid]
            if kind == "script":
                raise StoreConflict("script executor is not configured")
            policy = node.get("execution_policy") or {}
            template_uuid = node.get("workflow_node_template_uuid")
            template = templates.get(template_uuid)
            target_handles = sorted(
                (
                    handle
                    for handle in handles.values()
                    if template_uuid is not None
                    and handle.get("workflow_node_template_uuid") == template_uuid
                    and handle.get("io_type") == "target"
                ),
                key=lambda item: item["uuid"],
            )
            source_handle_uuids = sorted(
                handle["uuid"]
                for handle in handles.values()
                if template_uuid is not None
                and handle.get("workflow_node_template_uuid") == template_uuid
                and handle.get("io_type") == "source"
            )
            planned_node: Dict[str, Any] = {
                "uuid": node_uuid,
                "topological_index": index,
                "kind": kind,
                "param": node.get("param") or {},
                "execution_policy": policy,
                "inputs": [
                    {
                        "handle_uuid": handle["uuid"],
                        "data_key": self._final_target_data_key(
                            self._handle_data_key(handle)
                        ),
                        "type": str(handle.get("type") or "").strip(),
                        "required": bool(handle.get("required")),
                    }
                    for handle in target_handles
                ],
            }
            if node.get("material_uuid") is not None:
                planned_node["material_uuid"] = node["material_uuid"]
            if node.get("script") is not None:
                planned_node["script"] = node["script"]
            if source_handle_uuids:
                planned_node["source_handle_uuids"] = source_handle_uuids
            if template is not None and template.get("schema") is not None:
                planned_node["param_schema"] = template["schema"]
            planned_nodes.append(planned_node)
            jobs.append(
                {
                    "uuid": str(uuid4()),
                    "workflow_node_uuid": node_uuid,
                    "topological_index": index,
                    "executor_kind": kind,
                    "execution_policy": policy,
                    "execution_timeout_seconds": 0,
                    "param": node.get("param") or {},
                }
            )
        plan = {
            "run_mode": run_mode,
            "nodes": planned_nodes,
            "edges": planned_edges,
        }
        if target_node_uuid is not None:
            plan["target_node_uuid"] = target_node_uuid
        return plan, jobs

    @staticmethod
    def _handle_data_key(handle: Dict[str, Any]) -> str:
        return str(handle.get("data_key") or handle.get("handle_key") or "").strip()

    @staticmethod
    def _final_target_data_key(data_key: str) -> str:
        return data_key.split("@@@")[-1].strip()

    @staticmethod
    def _dependency_only(handle: Dict[str, Any]) -> bool:
        if str(handle.get("handle_key") or "").strip().lower() == "ready":
            return True
        data_source = str(handle.get("data_source") or "").strip()
        return bool(data_source) and data_source.lower() != "executor"

    @staticmethod
    def _executor_kind(node_type: str) -> str:
        normalized = node_type.strip().lower()
        aliases = {
            "ilab": "device_action",
            "device": "device_action",
            "action": "device_action",
            "resource_action": "device_action",
            "py_script": "script",
        }
        kind = aliases.get(normalized, normalized)
        if kind not in {
            "device_action",
            "compute",
            "condition",
            "script",
            "group",
            "tool_call",
            "manual_confirm",
        }:
            raise StoreConflict(f"unsupported workflow node type {node_type!r}")
        return kind

    # Authoring ----------------------------------------------------------

    def register_editable_source(
        self,
        *,
        workflow_uuid: str,
        package_id: str,
        package_root: str | Path,
        relative_path: str,
    ) -> Dict[str, Any]:
        workflow_uuid = self._get_authoring_workflow(workflow_uuid)["uuid"]
        with self._authoring_lock(workflow_uuid):
            self._get_authoring_workflow(workflow_uuid)
            raw_root = Path(os.path.abspath(package_root))
            if self._path_contains_symlink(raw_root):
                raise WorkflowError("invalid_input")
            try:
                root = raw_root.resolve(strict=True)
            except OSError:
                raise WorkflowError("invalid_input") from None
            if not root.is_dir() or not package_id:
                raise WorkflowError("invalid_input")
            relative = PurePosixPath(relative_path)
            if (
                relative.is_absolute()
                or len(relative.parts) != 2
                or any(part in {"", ".", ".."} for part in relative.parts)
                or relative.parts[0] != "workflows"
                or relative.suffix != ".py"
                or not relative.stem
            ):
                raise WorkflowError("invalid_input")
            target = root.joinpath(*relative.parts)
            self._assert_contained_regular_target(
                root,
                target,
                allow_missing=True,
            )
            source_uri = f"package://{package_id}/{relative.as_posix()}"
            try:
                return self.store.register_source(
                    workflow_uuid=workflow_uuid,
                    package_id=package_id,
                    package_root=str(root),
                    relative_path=relative.as_posix(),
                    source_uri=source_uri,
                )
            except StoreConflict:
                raise WorkflowConflict("invalid_input") from None

    def get_authoring(self, workflow_uuid: str) -> Dict[str, Any]:
        workflow_uuid = self._get_authoring_workflow(workflow_uuid)["uuid"]
        with self._authoring_lock(workflow_uuid):
            workflow = self._get_authoring_workflow(workflow_uuid)
            registration = self._registration(workflow_uuid)
            source = self._read_source(registration)
            graph = self.store.get_graph(workflow_uuid)
            record = self.store.get_authoring_record(workflow_uuid)
            return self._authoring_aggregate(
                workflow=workflow,
                graph=graph,
                registration=registration,
                source=source,
                record=record,
            )

    def save_draft(
        self,
        workflow_uuid: str,
        *,
        python_source: str,
        expected_draft_hash: Optional[str],
        expected_workflow_revision: int,
    ) -> Dict[str, Any]:
        self._validate_hash(expected_draft_hash, nullable=True)
        workflow_uuid = self._get_authoring_workflow(workflow_uuid)["uuid"]
        with self._authoring_lock(workflow_uuid):
            workflow = self._get_authoring_workflow(workflow_uuid)
            registration = self._registration(workflow_uuid)
            current = self._read_source(registration)
            current_hash = current["draft_hash"] if current is not None else None
            if current_hash != expected_draft_hash:
                raise WorkflowConflict("draft_hash_conflict")
            if workflow["revision"] != expected_workflow_revision:
                raise WorkflowConflict("workflow_revision_conflict")

            try:
                encoded = python_source.encode("utf-8")
            except UnicodeEncodeError:
                raise WorkflowError("invalid_input") from None
            try:
                self._atomic_write(
                    registration,
                    encoded,
                    expected_hash=current_hash,
                )
            except OSError:
                raise WorkflowError("internal_error") from None
            source = self._read_source(registration)
            assert source is not None
            if source["draft_hash"] != _sha256(encoded):
                raise WorkflowConflict("draft_hash_conflict")
            compilation = self._compile(
                workflow=workflow,
                graph=self.store.get_graph(workflow_uuid),
                registration=registration,
                python_source=source["python_source"],
            )
            candidate = self._issue_candidate(
                workflow_revision=workflow["revision"],
                draft_hash=source["draft_hash"],
                compilation=compilation,
            )
            event_data = {
                "workflow_uuid": workflow_uuid,
                "cause": "draft_saved",
                "workflow_revision": workflow["revision"],
                "draft_hash": source["draft_hash"],
                "candidate_hash": (
                    candidate["candidate_hash"] if candidate is not None else None
                ),
            }
            self.store.record_draft_compilation(
                workflow_uuid=workflow_uuid,
                draft_hash=source["draft_hash"],
                draft_update_time=source["update_time"],
                diagnostics=compilation.diagnostics,
                candidate_hash=(
                    candidate["candidate_hash"] if candidate is not None else None
                ),
                candidate=candidate,
                event_data=event_data,
            )
            return self.get_authoring(workflow_uuid)

    def reconcile_registered_source(
        self,
        workflow_uuid: str,
    ) -> Dict[str, Any]:
        workflow_uuid = self._get_authoring_workflow(workflow_uuid)["uuid"]
        with self._authoring_lock(workflow_uuid):
            workflow = self._get_authoring_workflow(workflow_uuid)
            registration = self._registration(workflow_uuid)
            source = self._read_source(registration)
            record = self.store.get_authoring_record(workflow_uuid)
            if record["writeback_status"] == "pending" and (
                source is None
                or source["draft_hash"] == record["writeback_expected_hash"]
            ):
                recovery_source = record.get("writeback_source")
                if recovery_source is not None:
                    try:
                        self._atomic_write(
                            registration,
                            recovery_source.encode("utf-8"),
                            expected_hash=(
                                source["draft_hash"] if source is not None else None
                            ),
                        )
                        source = self._read_source(registration)
                        assert source is not None
                        self.store.settle_writeback(
                            workflow_uuid=workflow_uuid,
                            observed_draft_hash=source["draft_hash"],
                            draft_update_time=source["update_time"],
                            event_data={
                                "workflow_uuid": workflow_uuid,
                                "cause": "recovered",
                                "workflow_revision": workflow["revision"],
                                "draft_hash": source["draft_hash"],
                                "candidate_hash": None,
                            },
                        )
                        return self.get_authoring(workflow_uuid)
                    except (OSError, UnicodeError, WorkflowError):
                        pass
            actual_hash = source["draft_hash"] if source is not None else None
            if actual_hash == record["observed_draft_hash"] and not (
                actual_hash is None and record.get("candidate") is not None
            ):
                return self.get_authoring(workflow_uuid)

            candidate: Optional[Dict[str, Any]] = None
            diagnostics: List[Dict[str, Any]] = []
            if source is not None:
                compilation = self._compile(
                    workflow=workflow,
                    graph=self.store.get_graph(workflow_uuid),
                    registration=registration,
                    python_source=source["python_source"],
                )
                diagnostics = compilation.diagnostics
                candidate = self._issue_candidate(
                    workflow_revision=workflow["revision"],
                    draft_hash=source["draft_hash"],
                    compilation=compilation,
                )
            cause = (
                "recovered"
                if source is not None
                and record["observed_draft_hash"] is None
                and record["update_time"] is not None
                else "external_draft_changed"
            )
            self.store.record_draft_compilation(
                workflow_uuid=workflow_uuid,
                draft_hash=actual_hash,
                draft_update_time=(
                    source["update_time"] if source is not None else None
                ),
                diagnostics=diagnostics,
                candidate_hash=(
                    candidate["candidate_hash"] if candidate is not None else None
                ),
                candidate=candidate,
                event_data={
                    "workflow_uuid": workflow_uuid,
                    "cause": cause,
                    "workflow_revision": workflow["revision"],
                    "draft_hash": actual_hash,
                    "candidate_hash": (
                        candidate["candidate_hash"] if candidate is not None else None
                    ),
                },
            )
            return self.get_authoring(workflow_uuid)

    def apply_authoring(
        self,
        workflow_uuid: str,
        *,
        expected_draft_hash: str,
        expected_workflow_revision: int,
        expected_candidate_hash: str,
    ) -> Dict[str, Any]:
        self._validate_hash(expected_draft_hash, nullable=False)
        self._validate_hash(expected_candidate_hash, nullable=False)
        workflow_uuid = self._get_authoring_workflow(workflow_uuid)["uuid"]
        with self._authoring_lock(workflow_uuid):
            workflow = self._get_authoring_workflow(workflow_uuid)
            registration = self._registration(workflow_uuid)
            source = self._read_source(registration)
            actual_hash = source["draft_hash"] if source is not None else None

            # D-079 fixes this exact conflict order.
            if actual_hash != expected_draft_hash:
                raise WorkflowConflict("draft_hash_conflict")
            if workflow["revision"] != expected_workflow_revision:
                raise WorkflowConflict("workflow_revision_conflict")

            record = self.store.get_authoring_record(workflow_uuid)
            candidate = record.get("candidate")
            current_catalog = self._catalog_fingerprint()
            if (
                candidate is not None
                and candidate["template_catalog_fingerprint"] != current_catalog
            ):
                raise WorkflowConflict("template_catalog_conflict")
            if candidate is None:
                if any(
                    str(item.get("severity", "")).lower() == "error"
                    for item in record["diagnostics"]
                ):
                    raise WorkflowError("draft_invalid")
                raise WorkflowConflict("candidate_not_ready")
            if candidate["candidate_hash"] != expected_candidate_hash:
                raise WorkflowConflict("candidate_hash_conflict")
            if source is None:
                raise WorkflowConflict("draft_hash_conflict")

            compilation = self._compile(
                workflow=workflow,
                graph=self.store.get_graph(workflow_uuid),
                registration=registration,
                python_source=source["python_source"],
            )
            if not compilation.valid:
                if any(
                    str(item.get("severity", "")).lower() == "error"
                    for item in compilation.diagnostics
                ):
                    raise WorkflowError("draft_invalid")
                raise WorkflowError("candidate_invalid")
            revalidated = self._issue_candidate(
                workflow_revision=workflow["revision"],
                draft_hash=source["draft_hash"],
                compilation=compilation,
            )
            if revalidated is None:
                raise WorkflowError("candidate_invalid")
            if (
                revalidated["template_catalog_fingerprint"]
                != candidate["template_catalog_fingerprint"]
            ):
                raise WorkflowConflict("template_catalog_conflict")
            if revalidated["candidate_hash"] != candidate["candidate_hash"]:
                raise WorkflowConflict("candidate_hash_conflict")

            normalized_source = candidate["normalized_python_source"]
            normalized_bytes = normalized_source.encode("utf-8")
            normalized_hash = _sha256(normalized_bytes)
            applied_source = {
                "python_source": normalized_source,
                "source_hash": normalized_hash,
                "source_map": candidate["source_map"],
                "compiler_version": candidate["compiler_version"],
                "template_catalog_fingerprint": candidate[
                    "template_catalog_fingerprint"
                ],
            }
            previous_revision = workflow["revision"]
            try:
                resulting_revision = self.store.apply_authoring_candidate(
                    workflow_uuid=workflow_uuid,
                    expected_revision=previous_revision,
                    candidate=candidate,
                    applied_source=applied_source,
                    event_data={
                        "workflow_uuid": workflow_uuid,
                        "cause": "applied",
                        "draft_hash": normalized_hash,
                        "candidate_hash": None,
                    },
                )
            except StoreRevisionConflict:
                raise WorkflowConflict("workflow_revision_conflict") from None
            except (StoreConflict, ValidationError):
                raise WorkflowError("candidate_invalid") from None

            warnings: List[Dict[str, str]] = []
            response_source = source

            def warn_writeback() -> None:
                if warnings:
                    return
                warnings.append(
                    {
                        "code": "draft_writeback_pending",
                        "message": (
                            "工作流已应用，但本地源码同步失败；"
                            "OS 已保留可恢复的源码记录。"
                        ),
                    }
                )

            def mark_pending_best_effort() -> None:
                for _attempt in range(2):
                    try:
                        self.store.mark_writeback_pending(workflow_uuid)
                        return
                    except Exception:  # noqa: BLE001 - 提交后只能尽力恢复
                        continue

            try:
                latest = self._read_source(registration)
                if latest is None or latest["draft_hash"] != actual_hash:
                    raise WorkflowError("draft_hash_conflict")
                self._atomic_write(
                    registration,
                    normalized_bytes,
                    expected_hash=actual_hash,
                )
                written = self._read_source(registration)
                assert written is not None
                response_source = written
            except Exception:  # noqa: BLE001 - 主事务已提交
                # 主事务已经提交。之后任何文件系统、数据库或聚合错误
                # 都只能降级为可恢复警告，不能把成功伪装成失败。
                warn_writeback()
                mark_pending_best_effort()
            else:
                settled = False
                for _attempt in range(2):
                    try:
                        self.store.settle_writeback(
                            workflow_uuid=workflow_uuid,
                            observed_draft_hash=written["draft_hash"],
                            draft_update_time=written["update_time"],
                        )
                        settled = True
                        break
                    except Exception:  # noqa: BLE001 - 主事务已提交
                        warn_writeback()
                if not settled:
                    mark_pending_best_effort()

            fallback_meta_data = dict(workflow["meta_data"])
            candidate_workflow = candidate["graph"].get("workflow") or {}
            candidate_meta_data = candidate_workflow.get("meta_data") or {}
            if (
                isinstance(candidate_meta_data, dict)
                and "unilab" in candidate_meta_data
            ):
                fallback_meta_data["unilab"] = candidate_meta_data["unilab"]
            fallback_workflow = {
                **workflow,
                "revision": resulting_revision,
                "meta_data": fallback_meta_data,
                "update_time": utc_now(),
            }
            fallback_applied_source = {
                **applied_source,
                "workflow_revision": resulting_revision,
                "update_time": utc_now(),
            }
            fallback_record = {
                "observed_draft_hash": (
                    response_source["draft_hash"]
                    if response_source is not None
                    else None
                ),
                "diagnostics": [],
                "candidate": None,
                "applied_source": fallback_applied_source,
            }
            try:
                authoring = self.get_authoring(workflow_uuid)
            except Exception:  # noqa: BLE001 - 主事务已提交
                try:
                    authoring = self.get_authoring(workflow_uuid)
                except Exception:  # noqa: BLE001 - 使用已知事实降级
                    try:
                        fallback_graph = self.store.get_graph(workflow_uuid)
                        fallback_workflow = fallback_graph["workflow"]
                        fallback_record = self.store.get_authoring_record(workflow_uuid)
                    except Exception:  # noqa: BLE001 - 使用提交时事实降级
                        fallback_graph = self._post_commit_candidate_graph(
                            candidate["graph"],
                            workflow=fallback_workflow,
                        )
                    authoring = self._authoring_aggregate(
                        workflow=fallback_workflow,
                        graph=fallback_graph,
                        registration=registration,
                        source=response_source,
                        record=fallback_record,
                    )

            return {
                "apply_result": {
                    "kind": candidate["changeset"]["kind"],
                    "previous_workflow_revision": previous_revision,
                    "workflow_revision": resulting_revision,
                    "applied_candidate_hash": candidate["candidate_hash"],
                    "applied_source_hash": normalized_hash,
                    "warnings": warnings,
                },
                "authoring": authoring,
            }

    def list_events(
        self,
        *,
        after_id: int = 0,
        limit: int = 200,
    ) -> Dict[str, Any]:
        if after_id < 0 or not 1 <= limit <= 1000:
            raise WorkflowError("invalid_input")
        return {
            "items": self.store.list_events(after_id=after_id, limit=limit),
            "after_id": after_id,
        }

    # Authoring internals ------------------------------------------------

    def _get_authoring_workflow(
        self,
        workflow_uuid: str,
    ) -> Dict[str, Any]:
        try:
            return self.store.get_workflow(validate_uuid(workflow_uuid))
        except (ValueError, StoreNotFound):
            raise WorkflowError("workflow_not_found") from None

    def _registration(self, workflow_uuid: str) -> Dict[str, Any]:
        try:
            return self.store.get_source_registration(workflow_uuid)
        except StoreNotFound:
            raise WorkflowError("workflow_not_found") from None

    def _read_source(
        self,
        registration: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        root, target = self._source_path(registration)
        self._assert_contained_regular_target(root, target, allow_missing=True)
        with self._source_parent_fd(
            registration,
            create=False,
        ) as source_parent:
            if source_parent is None:
                return None
            parent_fd, filename = source_parent
            self._recover_missing_target(
                parent_fd,
                filename,
            )
            try:
                descriptor = os.open(
                    filename,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
            except FileNotFoundError:
                return None
            except OSError:
                raise WorkflowError("invalid_input") from None
            try:
                stat_result = os.fstat(descriptor)
                if not stat.S_ISREG(stat_result.st_mode):
                    raise WorkflowError("invalid_input")
                with os.fdopen(descriptor, "rb") as stream:
                    descriptor = -1
                    try:
                        raw = stream.read()
                    except OSError:
                        raise WorkflowError("invalid_input") from None
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        try:
            source = raw.decode("utf-8")
        except UnicodeError:
            raise WorkflowError("invalid_input") from None
        return {
            "python_source": source,
            "draft_hash": _sha256(raw),
            "update_time": _mtime_rfc3339(stat_result.st_mtime),
        }

    def source_signature(
        self,
        registration: Dict[str, Any],
    ) -> Tuple[Any, ...]:
        """返回无需读取文件内容的稳定性签名，供 Draft 监视器去抖。"""

        root, target = self._source_path(registration)
        self._assert_contained_regular_target(root, target, allow_missing=True)
        with self._source_parent_fd(
            registration,
            create=False,
        ) as source_parent:
            if source_parent is None:
                return ("missing",)
            parent_fd, filename = source_parent
            try:
                stat_result = os.stat(
                    filename,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return ("missing",)
            except OSError:
                raise WorkflowError("invalid_input") from None
            if not stat.S_ISREG(stat_result.st_mode):
                raise WorkflowError("invalid_input")
        return (
            "file",
            stat_result.st_dev,
            stat_result.st_ino,
            stat_result.st_size,
            stat_result.st_mtime_ns,
            stat_result.st_ctime_ns,
        )

    def _atomic_write(
        self,
        registration: Dict[str, Any],
        content: bytes,
        *,
        expected_hash: Any = _NO_EXPECTED_HASH,
    ) -> None:
        root, target = self._source_path(registration)
        self._assert_contained_regular_target(root, target, allow_missing=True)
        # 先以目录 FD 安全地创建（如有需要）固定的 workflows 目录。
        # 该上下文关闭后再次打开，避免依赖校验时拿到的字符串路径。
        with self._source_parent_fd(registration, create=True):
            pass
        self._assert_contained_regular_target(root, target, allow_missing=True)
        with self._source_parent_fd(
            registration,
            create=False,
        ) as source_parent:
            if source_parent is None:
                raise WorkflowError("invalid_input")
            parent_fd, filename = source_parent
            temporary_name = f".{filename}.{uuid4().hex}.tmp"
            descriptor = -1
            try:
                descriptor = os.open(
                    temporary_name,
                    (
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | os.O_CLOEXEC
                        | os.O_NOFOLLOW
                    ),
                    0o600,
                    dir_fd=parent_fd,
                )
                with os.fdopen(descriptor, "wb") as stream:
                    descriptor = -1
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                if expected_hash is _NO_EXPECTED_HASH:
                    os.replace(
                        temporary_name,
                        filename,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                    )
                else:
                    self._compare_and_replace(
                        parent_fd=parent_fd,
                        target_name=filename,
                        temporary_name=temporary_name,
                        expected_hash=expected_hash,
                    )
                os.fsync(parent_fd)
            except WorkflowError:
                raise
            except OSError:
                raise WorkflowError("internal_error") from None
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                with suppress(FileNotFoundError):
                    os.unlink(temporary_name, dir_fd=parent_fd)

    @staticmethod
    def _compare_and_replace(
        *,
        parent_fd: int,
        target_name: str,
        temporary_name: str,
        expected_hash: Optional[str],
    ) -> None:
        """在内核独占 lease 下更新同一 inode，避免旧句柄成为孤儿。"""

        target_descriptor = -1
        temporary_descriptor = -1
        backup_descriptor = -1
        backup_name = f".{target_name}.{uuid4().hex}.cas"
        backup_created = False
        modified = False
        original = b""
        try:
            try:
                target_descriptor = os.open(
                    target_name,
                    os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
            except FileNotFoundError:
                if expected_hash is not None:
                    raise WorkflowConflict("draft_hash_conflict") from None
                try:
                    os.link(
                        temporary_name,
                        target_name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    raise WorkflowConflict("draft_hash_conflict") from None
                os.unlink(temporary_name, dir_fd=parent_fd)
                return

            if expected_hash is None:
                raise WorkflowConflict("draft_hash_conflict")
            try:
                fcntl.fcntl(
                    target_descriptor,
                    fcntl.F_SETLEASE,
                    fcntl.F_WRLCK,
                )
            except (AttributeError, OSError):
                # 无法证明没有预打开的读写句柄时必须失败关闭。
                raise WorkflowConflict("draft_hash_conflict") from None

            original = WorkflowService._read_regular_fd(target_descriptor)
            if _sha256(original) != expected_hash:
                raise WorkflowConflict("draft_hash_conflict")
            if not WorkflowService._target_matches_fd(
                parent_fd,
                target_name,
                target_descriptor,
            ):
                raise WorkflowConflict("draft_hash_conflict")

            temporary_descriptor = os.open(
                temporary_name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            replacement = WorkflowService._read_regular_fd(temporary_descriptor)

            backup_descriptor = os.open(
                backup_name,
                (os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW),
                0o600,
                dir_fd=parent_fd,
            )
            backup_created = True
            WorkflowService._write_regular_fd(
                backup_descriptor,
                original,
            )
            os.close(backup_descriptor)
            backup_descriptor = -1
            os.fsync(parent_fd)

            modified = True
            WorkflowService._write_regular_fd(
                target_descriptor,
                replacement,
            )
            if not WorkflowService._target_matches_fd(
                parent_fd,
                target_name,
                target_descriptor,
            ) or WorkflowService._hash_regular_fd(target_descriptor) != _sha256(
                replacement
            ):
                raise WorkflowConflict("draft_hash_conflict")

            os.unlink(temporary_name, dir_fd=parent_fd)
            os.unlink(backup_name, dir_fd=parent_fd)
            backup_created = False
            os.fsync(parent_fd)
        except Exception:
            restored = False
            if modified:
                try:
                    WorkflowService._restore_regular_fd(
                        target_descriptor,
                        original,
                    )
                    restored = WorkflowService._target_matches_fd(
                        parent_fd,
                        target_name,
                        target_descriptor,
                    )
                except OSError:
                    restored = False
            if backup_created and restored:
                with suppress(OSError):
                    os.unlink(backup_name, dir_fd=parent_fd)
                    backup_created = False
            elif (
                backup_created
                and modified
                and WorkflowService._target_matches_fd(
                    parent_fd,
                    target_name,
                    target_descriptor,
                )
            ):
                # 回滚失败时，不能把候选内容伪装成一次失败前的 Draft。
                # 删除当前 inode 的目录项并保留已 fsync 的 `.cas`；后续
                # 读取会从该唯一原稿副本恢复。
                with suppress(OSError):
                    os.unlink(target_name, dir_fd=parent_fd)
            if backup_created:
                with suppress(OSError):
                    os.fsync(parent_fd)
            raise
        finally:
            if backup_descriptor >= 0:
                os.close(backup_descriptor)
            if temporary_descriptor >= 0:
                os.close(temporary_descriptor)
            if target_descriptor >= 0:
                os.close(target_descriptor)

    @staticmethod
    def _target_matches_fd(
        parent_fd: int,
        target_name: str,
        descriptor: int,
    ) -> bool:
        try:
            target_stat = os.stat(
                target_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return False
        descriptor_stat = os.fstat(descriptor)
        return (
            stat.S_ISREG(target_stat.st_mode)
            and target_stat.st_dev == descriptor_stat.st_dev
            and target_stat.st_ino == descriptor_stat.st_ino
        )

    @staticmethod
    def _read_regular_fd(descriptor: int) -> bytes:
        stat_result = os.fstat(descriptor)
        if not stat.S_ISREG(stat_result.st_mode):
            raise WorkflowError("invalid_input")
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _write_regular_fd(descriptor: int, content: bytes) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise OSError("short write while replacing Workflow Draft")
            offset += written
        os.ftruncate(descriptor, len(content))
        os.fsync(descriptor)

    @staticmethod
    def _restore_regular_fd(descriptor: int, content: bytes) -> None:
        WorkflowService._write_regular_fd(descriptor, content)

    @staticmethod
    def _hash_regular_fd(descriptor: int) -> str:
        return _sha256(WorkflowService._read_regular_fd(descriptor))

    @staticmethod
    def _recover_missing_target(
        parent_fd: int,
        target_name: str,
    ) -> None:
        try:
            os.stat(
                target_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            return
        except FileNotFoundError:
            pass
        prefix = f".{target_name}."
        backups = []
        for name in os.listdir(parent_fd):
            if not name.startswith(prefix) or not name.endswith(".cas"):
                continue
            try:
                backup_stat = os.stat(
                    name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            if stat.S_ISREG(backup_stat.st_mode):
                backups.append((backup_stat.st_mtime_ns, name))
        if not backups:
            return
        backup_name = max(backups)[1]
        try:
            os.replace(
                backup_name,
                target_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            os.fsync(parent_fd)
        except OSError:
            raise WorkflowError("internal_error") from None

    @classmethod
    @contextmanager
    def _source_parent_fd(
        cls,
        registration: Dict[str, Any],
        *,
        create: bool,
    ) -> Iterator[Optional[Tuple[int, str]]]:
        relative = PurePosixPath(registration["relative_path"])
        if (
            relative.is_absolute()
            or len(relative.parts) != 2
            or relative.parts[0] != "workflows"
            or any(part in {"", ".", ".."} for part in relative.parts)
            or relative.suffix != ".py"
            or not relative.stem
        ):
            raise WorkflowError("invalid_input")

        root_fd = cls._open_directory_chain(Path(registration["package_root"]))
        parent_fd = -1
        try:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
            try:
                parent_fd = os.open(
                    relative.parts[0],
                    flags,
                    dir_fd=root_fd,
                )
            except FileNotFoundError:
                if not create:
                    yield None
                    return
                with suppress(FileExistsError):
                    os.mkdir(relative.parts[0], 0o755, dir_fd=root_fd)
                parent_fd = os.open(
                    relative.parts[0],
                    flags,
                    dir_fd=root_fd,
                )
            yield parent_fd, relative.parts[1]
        except WorkflowError:
            raise
        except OSError:
            raise WorkflowError("invalid_input") from None
        finally:
            if parent_fd >= 0:
                os.close(parent_fd)
            os.close(root_fd)

    @staticmethod
    def _open_directory_chain(path: Path) -> int:
        absolute = Path(os.path.abspath(path))
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            current_fd = os.open(absolute.anchor, flags)
            for part in absolute.parts[1:]:
                try:
                    next_fd = os.open(part, flags, dir_fd=current_fd)
                finally:
                    os.close(current_fd)
                current_fd = next_fd
            return current_fd
        except OSError:
            raise WorkflowError("invalid_input") from None

    @staticmethod
    def _source_path(
        registration: Dict[str, Any],
    ) -> Tuple[Path, Path]:
        stored_root = Path(registration["package_root"])
        if WorkflowService._path_contains_symlink(stored_root):
            raise WorkflowError("invalid_input")
        try:
            root = stored_root.resolve(strict=True)
        except OSError:
            raise WorkflowError("invalid_input") from None
        relative = PurePosixPath(registration["relative_path"])
        return root, root.joinpath(*relative.parts)

    @staticmethod
    def _path_contains_symlink(path: Path) -> bool:
        absolute = Path(os.path.abspath(path))
        current = Path(absolute.anchor)
        for part in absolute.parts[1:]:
            current = current / part
            if current.is_symlink():
                return True
        return False

    @staticmethod
    def _assert_contained_regular_target(
        root: Path,
        target: Path,
        *,
        allow_missing: bool,
    ) -> None:
        if target.is_symlink():
            raise WorkflowError("invalid_input")
        try:
            relative = target.relative_to(root)
            current = root
            for part in relative.parts[:-1]:
                current = current / part
                if current.is_symlink():
                    raise WorkflowError("invalid_input")
            resolved = target.resolve(strict=False)
            resolved.relative_to(root)
        except WorkflowError:
            raise
        except (OSError, ValueError):
            raise WorkflowError("invalid_input") from None
        if target.exists() and not target.is_file():
            raise WorkflowError("invalid_input")
        if not allow_missing and not target.exists():
            raise WorkflowError("invalid_input")

    def _compile(
        self,
        *,
        workflow: Dict[str, Any],
        graph: Dict[str, Any],
        registration: Dict[str, Any],
        python_source: str,
    ) -> CandidateCompilation:
        if self.compiler is None:
            raise WorkflowError("template_catalog_unavailable")
        try:
            result = self.compiler.compile(
                workflow_uuid=workflow["uuid"],
                workflow_revision=workflow["revision"],
                python_source=python_source,
                source_uri=registration["source_uri"],
                applied_graph=graph,
            )
            return CandidateCompilation.model_validate(result)
        except WorkflowError:
            raise
        except Exception:
            raise WorkflowError("internal_error") from None

    def _catalog_fingerprint(self) -> str:
        if self.compiler is None:
            raise WorkflowError("template_catalog_unavailable")
        try:
            value = self.compiler.template_catalog_fingerprint
        except Exception:
            raise WorkflowError("template_catalog_unavailable") from None
        if not isinstance(value, str) or not value:
            raise WorkflowError("template_catalog_unavailable")
        return value

    def _issue_candidate(
        self,
        *,
        workflow_revision: int,
        draft_hash: str,
        compilation: CandidateCompilation,
    ) -> Optional[Dict[str, Any]]:
        if not compilation.valid:
            return None
        assert compilation.graph is not None
        graph = self._backend_graph_projection(compilation.graph)
        bundle = {
            "base_workflow_revision": workflow_revision,
            "draft_hash": draft_hash,
            "graph": graph,
            "normalized_python_source": compilation.normalized_python_source,
            "source_map": compilation.source_map,
            "changeset": compilation.changeset,
            "compiler_version": compilation.compiler_version,
            "template_catalog_fingerprint": (compilation.template_catalog_fingerprint),
        }
        return {
            "candidate_hash": _sha256(_canonical_json(bundle)),
            **bundle,
            "update_time": utc_now(),
        }

    @staticmethod
    def _backend_graph_projection(
        graph: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Project one Candidate through Backend JSON omitempty semantics."""

        def omit_none(value: Any) -> Any:
            if not isinstance(value, dict):
                return value
            return {key: item for key, item in value.items() if item is not None}

        return {
            "workflow": omit_none(graph.get("workflow") or {}),
            "nodes": [omit_none(item) for item in (graph.get("nodes") or [])],
            "edges": [omit_none(item) for item in (graph.get("edges") or [])],
            "node_templates": [
                omit_none(item) for item in (graph.get("node_templates") or [])
            ],
            "handle_templates": [
                omit_none(item) for item in (graph.get("handle_templates") or [])
            ],
        }

    @classmethod
    def _post_commit_candidate_graph(
        cls,
        graph: Dict[str, Any],
        *,
        workflow: Dict[str, Any],
    ) -> Dict[str, Any]:
        projected = cls._backend_graph_projection(graph)
        projected["workflow"] = {
            **projected["workflow"],
            **workflow,
        }
        return projected

    def _authoring_aggregate(
        self,
        *,
        workflow: Dict[str, Any],
        graph: Dict[str, Any],
        registration: Dict[str, Any],
        source: Optional[Dict[str, Any]],
        record: Dict[str, Any],
    ) -> Dict[str, Any]:
        draft: Optional[Dict[str, Any]] = None
        diagnostics: List[Dict[str, Any]] = []
        if source is not None:
            if record["observed_draft_hash"] == source["draft_hash"]:
                diagnostics = record["diagnostics"]
            draft = {
                "source_uri": registration["source_uri"],
                **source,
                "diagnostics": diagnostics,
            }

        stored_candidate = record.get("candidate")
        candidate: Optional[Dict[str, Any]] = None
        candidate_stale = False
        if stored_candidate is not None and source is not None:
            catalog_matches = False
            try:
                catalog_matches = (
                    stored_candidate["template_catalog_fingerprint"]
                    == self._catalog_fingerprint()
                )
            except WorkflowError:
                catalog_matches = False
            candidate_current = (
                record["observed_draft_hash"] == source["draft_hash"]
                and stored_candidate["draft_hash"] == source["draft_hash"]
                and stored_candidate["base_workflow_revision"] == workflow["revision"]
                and catalog_matches
            )
            if candidate_current:
                candidate = stored_candidate
            else:
                candidate_stale = True

        applied_source = record.get("applied_source")
        if source is None:
            state = "draft_missing"
        elif candidate_stale:
            state = "candidate_stale"
        elif any(
            str(item.get("severity", "")).lower() == "error" for item in diagnostics
        ):
            state = "draft_invalid"
        elif candidate is not None:
            state = (
                "unapplied_source_only"
                if candidate["changeset"]["kind"] == "source_only"
                else "unapplied_graph"
            )
        elif (
            applied_source is not None
            and applied_source["workflow_revision"] == workflow["revision"]
            and applied_source["source_hash"] == source["draft_hash"]
        ):
            state = "applied"
        else:
            state = "applied_source_stale"

        return {
            "workflow_uuid": workflow["uuid"],
            "workflow_revision": workflow["revision"],
            "state": state,
            "applied_graph": graph,
            "draft": draft,
            "candidate": candidate,
            "applied_source": applied_source,
        }

    def _authoring_lock(self, workflow_uuid: str) -> threading.RLock:
        with self._locks_guard:
            return self._authoring_locks.setdefault(
                workflow_uuid,
                threading.RLock(),
            )

    @staticmethod
    def _normalize_page(page: int, page_size: int) -> Tuple[int, int]:
        if page < 1:
            page = 1
        if page_size < 1:
            page_size = 20
        if page_size > 100:
            page_size = 100
        return page, page_size

    @staticmethod
    def _optional_text(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @staticmethod
    def _validate_hash(value: Optional[str], *, nullable: bool) -> None:
        if value is None:
            if nullable:
                return
            raise WorkflowError("invalid_input")
        if _HASH_TOKEN.fullmatch(value) is None:
            raise WorkflowError("invalid_input")


__all__ = [
    "AuthoringCompiler",
    "WorkflowConflict",
    "WorkflowError",
    "WorkflowService",
]
