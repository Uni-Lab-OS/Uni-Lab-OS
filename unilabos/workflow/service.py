"""Application service for the local Backend-shaped Workflow authority."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from collections import defaultdict
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
    ) -> CandidateCompilation:
        ...


def _sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _mtime_rfc3339(path: Path) -> str:
    timestamp = path.stat().st_mtime
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
            identity = validate_uuid(workflow_uuid or str(uuid4()))
            return self.store.create_workflow(
                workflow_uuid=identity,
                name=name,
                tags=tags,
                description=description,
                meta_data=meta_data,
            )
        except (ValueError, ValidationError):
            raise WorkflowError("invalid_input") from None
        except StoreConflict:
            raise WorkflowConflict("invalid_input") from None

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
        self._validate_page(page, page_size)
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
        with self._authoring_lock(workflow_uuid):
            self.get_workflow(workflow_uuid)
            return self.store.update_workflow(
                workflow_uuid,
                name=name,
                tags=tags,
                description=description,
                meta_data=meta_data,
            )

    def delete_workflow(self, workflow_uuid: str) -> None:
        with self._authoring_lock(workflow_uuid):
            self.get_workflow(workflow_uuid)
            self.store.delete_workflow(workflow_uuid)

    def get_graph(self, workflow_uuid: str) -> Dict[str, Any]:
        self.get_workflow(workflow_uuid)
        return self.store.get_graph(workflow_uuid)

    def save_graph(
        self,
        workflow_uuid: str,
        *,
        revision: int,
        nodes: List[WorkflowNodeWrite | Dict[str, Any]],
        edges: List[WorkflowEdgeWrite | Dict[str, Any]],
    ) -> Dict[str, Any]:
        with self._authoring_lock(workflow_uuid):
            self.get_workflow(workflow_uuid)
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
                    workflow_uuid,
                    revision=revision,
                    nodes=node_values,
                    edges=edge_values,
                )
            except ValidationError:
                raise WorkflowError("invalid_input") from None
            except StoreRevisionConflict:
                raise WorkflowConflict("workflow_revision_conflict") from None
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
        self.get_workflow(workflow_uuid)
        if run_mode not in {"normal", "step", "single_node"}:
            raise WorkflowError("invalid_input")
        if target_node_uuid is not None:
            try:
                target_node_uuid = validate_uuid(target_node_uuid)
            except ValueError:
                raise WorkflowError("invalid_input") from None
        if run_mode != "single_node" and target_node_uuid is not None:
            raise WorkflowError("invalid_input")
        # P0-2 has not frozen the root input vocabulary yet. Mirror the
        # Backend baseline's current empty Task input instead of persisting an
        # invented local representation.
        if input_value:
            raise WorkflowError("invalid_input")
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
            validate_uuid(task_uuid)
            return self.store.get_task(task_uuid)
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
        self._validate_page(page, page_size)
        if workflow_uuid is not None:
            try:
                workflow_uuid = validate_uuid(workflow_uuid)
            except ValueError:
                raise WorkflowError("invalid_input") from None
        return self.store.list_tasks(
            page=page,
            page_size=page_size,
            workflow_uuid=workflow_uuid,
            status=status,
            cleanup_status=cleanup_status,
        )

    def list_workflow_node_jobs(self, task_uuid: str) -> List[Dict[str, Any]]:
        self.get_workflow_task(task_uuid)
        return self.store.list_jobs(task_uuid)

    def get_workflow_node_job(self, job_uuid: str) -> Dict[str, Any]:
        try:
            validate_uuid(job_uuid)
            return self.store.get_job(job_uuid)
        except (ValueError, StoreNotFound):
            raise WorkflowError("not_found") from None

    def _build_execution_plan(
        self,
        graph: Dict[str, Any],
        *,
        run_mode: str,
        target_node_uuid: Optional[str],
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        enabled = {
            node["uuid"]: node
            for node in graph["nodes"]
            if not node["disabled"] and node["type"] != "group"
        }
        if run_mode == "single_node":
            selected = target_node_uuid
            if selected is None:
                selected = min(enabled) if enabled else None
            if selected not in enabled:
                raise StoreConflict("single_node target is not enabled")
            enabled = {selected: enabled[selected]}

        indegree = {node_uuid: 0 for node_uuid in enabled}
        outgoing: Dict[str, List[str]] = defaultdict(list)
        planned_edges: List[Dict[str, Any]] = []
        for edge in graph["edges"]:
            source = edge["source_node_uuid"]
            target = edge["target_node_uuid"]
            if source not in enabled or target not in enabled:
                continue
            outgoing[source].append(target)
            indegree[target] += 1
            planned_edges.append(
                {
                    "uuid": edge["uuid"],
                    "source_node_uuid": source,
                    "target_node_uuid": target,
                    "source_handle_uuid": edge["source_handle_uuid"],
                    "target_handle_uuid": edge["target_handle_uuid"],
                }
            )

        available = sorted(
            node_uuid for node_uuid, degree in indegree.items() if degree == 0
        )
        ordered: List[str] = []
        while available:
            node_uuid = available.pop(0)
            ordered.append(node_uuid)
            for target in sorted(outgoing[node_uuid]):
                indegree[target] -= 1
                if indegree[target] == 0:
                    available.append(target)
                    available.sort()
        if len(ordered) != len(enabled):
            raise StoreConflict("workflow graph contains a cycle")

        planned_nodes: List[Dict[str, Any]] = []
        jobs: List[Dict[str, Any]] = []
        for index, node_uuid in enumerate(ordered):
            node = enabled[node_uuid]
            kind = self._executor_kind(node["type"])
            policy = node.get("execution_policy") or {}
            planned_nodes.append(
                {
                    "uuid": node_uuid,
                    "topological_index": index,
                    "kind": kind,
                    "material_uuid": node.get("material_uuid"),
                    "param": node.get("param") or {},
                    "execution_policy": policy,
                    "inputs": [],
                }
            )
            jobs.append(
                {
                    "uuid": str(uuid4()),
                    "workflow_node_uuid": node_uuid,
                    "material_uuid": node.get("material_uuid"),
                    "topological_index": index,
                    "executor_kind": kind,
                    "execution_policy": policy,
                    "execution_timeout_seconds": policy.get(
                        "execution_timeout_seconds",
                        policy.get("timeout_seconds", 0),
                    ),
                    "param": node.get("param") or {},
                }
            )
        plan = {
            "run_mode": run_mode,
            "nodes": planned_nodes,
            "edges": planned_edges,
        }
        if run_mode == "single_node" and ordered:
            plan["target_node_uuid"] = ordered[0]
        return plan, jobs

    @staticmethod
    def _executor_kind(node_type: str) -> str:
        aliases = {
            "ILab": "device_action",
            "device": "device_action",
            "action": "device_action",
        }
        kind = aliases.get(node_type, node_type)
        if kind not in {
            "device_action",
            "compute",
            "condition",
            "script",
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
        with self._authoring_lock(workflow_uuid):
            self._get_authoring_workflow(workflow_uuid)
            root = Path(package_root).resolve(strict=True)
            if not root.is_dir() or not package_id:
                raise WorkflowError("invalid_input")
            relative = PurePosixPath(relative_path)
            if (
                relative.is_absolute()
                or not relative.parts
                or any(part in {"", ".", ".."} for part in relative.parts)
            ):
                raise WorkflowError("invalid_input")
            target = root.joinpath(*relative.parts)
            self._assert_contained_regular_target(
                root,
                target,
                allow_missing=True,
            )
            source_uri = f"package://{package_id}/{relative.as_posix()}"
            return self.store.register_source(
                workflow_uuid=workflow_uuid,
                package_id=package_id,
                package_root=str(root),
                relative_path=relative.as_posix(),
                source_uri=source_uri,
            )

    def get_authoring(self, workflow_uuid: str) -> Dict[str, Any]:
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
                self._atomic_write(registration, encoded)
            except OSError:
                raise WorkflowError("internal_error") from None
            source = self._read_source(registration)
            assert source is not None
            compilation = self._compile(
                workflow=workflow,
                graph=self.store.get_graph(workflow_uuid),
                registration=registration,
                python_source=python_source,
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
            if (
                actual_hash == record["observed_draft_hash"]
                and not (
                    actual_hash is None
                    and record.get("candidate") is not None
                )
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
                        candidate["candidate_hash"]
                        if candidate is not None
                        else None
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
                        "draft_hash": actual_hash,
                        "candidate_hash": None,
                    },
                )
            except StoreRevisionConflict:
                raise WorkflowConflict("workflow_revision_conflict") from None
            except (StoreConflict, ValidationError):
                raise WorkflowError("candidate_invalid") from None

            warnings: List[Dict[str, str]] = []
            try:
                self._atomic_write(registration, normalized_bytes)
                written = self._read_source(registration)
                assert written is not None
                self.store.settle_writeback(
                    workflow_uuid=workflow_uuid,
                    observed_draft_hash=written["draft_hash"],
                    draft_update_time=written["update_time"],
                )
            except (OSError, UnicodeError, WorkflowError):
                self.store.mark_writeback_pending(workflow_uuid)
                warnings.append(
                    {
                        "code": "draft_writeback_pending",
                        "message": (
                            "工作流已应用，但本地源码同步失败；"
                            "OS 已保留可恢复的源码记录。"
                        ),
                    }
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
                "authoring": self.get_authoring(workflow_uuid),
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
        if not target.exists():
            return None
        try:
            raw = target.read_bytes()
            source = raw.decode("utf-8")
        except (OSError, UnicodeError):
            raise WorkflowError("invalid_input") from None
        return {
            "python_source": source,
            "draft_hash": _sha256(raw),
            "update_time": _mtime_rfc3339(target),
        }

    def _atomic_write(
        self,
        registration: Dict[str, Any],
        content: bytes,
    ) -> None:
        root, target = self._source_path(registration)
        self._assert_contained_regular_target(root, target, allow_missing=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._assert_contained_regular_target(root, target, allow_missing=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
        )
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, target)
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    @staticmethod
    def _source_path(
        registration: Dict[str, Any],
    ) -> Tuple[Path, Path]:
        root = Path(registration["package_root"]).resolve(strict=True)
        relative = PurePosixPath(registration["relative_path"])
        return root, root.joinpath(*relative.parts)

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
            resolved = target.resolve(strict=False)
            resolved.relative_to(root)
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
        bundle = {
            "base_workflow_revision": workflow_revision,
            "draft_hash": draft_hash,
            "graph": compilation.graph,
            "normalized_python_source": compilation.normalized_python_source,
            "source_map": compilation.source_map,
            "changeset": compilation.changeset,
            "compiler_version": compilation.compiler_version,
            "template_catalog_fingerprint": (
                compilation.template_catalog_fingerprint
            ),
        }
        return {
            "candidate_hash": _sha256(_canonical_json(bundle)),
            **bundle,
            "update_time": utc_now(),
        }

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
                and stored_candidate["base_workflow_revision"]
                == workflow["revision"]
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
            str(item.get("severity", "")).lower() == "error"
            for item in diagnostics
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
    def _validate_page(page: int, page_size: int) -> None:
        if page < 1 or page_size < 1 or page_size > 200:
            raise WorkflowError("invalid_input")

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
