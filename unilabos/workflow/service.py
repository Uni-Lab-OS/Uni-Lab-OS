"""本地 Backend-shaped Workflow Authority 的应用服务。"""

from __future__ import annotations

import hashlib
import re
import threading
from collections import defaultdict
from collections.abc import Callable
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple
from uuid import uuid4

from pydantic import ValidationError

from unilabos.workflow.candidate_validation import (
    CandidateBundleError,
    validate_candidate_bundle,
)
from unilabos.workflow.device_action_run import (
    DeviceActionRunConflict,
    DeviceActionRunInputError,
    DeviceActionRunService,
    DeviceActionRunUnavailable,
)
from unilabos.workflow.graph_validation import GraphValidationError
from unilabos.workflow.json_codec import encode_json
from unilabos.workflow.models import (
    CandidateChangeset,
    CandidateCompilation,
    CandidateDiagnostic,
    CandidateSourceMapEntry,
    WorkflowEdgeWrite,
    WorkflowNodeWrite,
    normalize_json_array,
    normalize_json_object,
    validate_uuid,
)
from unilabos.workflow.source_coordinates import source_ranges_fit
from unilabos.workflow.source_discovery import (
    EditableSourceDiscoveryPlan,
    EditableSourceRegistration,
)
from unilabos.workflow.source_workspace import (
    NO_EXPECTED_HASH as _NO_EXPECTED_HASH,
)
from unilabos.workflow.source_workspace import (
    SourceWorkspaceConflict,
    SourceWorkspaceError,
    pin_package_roots,
    read_registered_source,
    registered_source_signature,
    validate_source_registration,
    write_registered_source,
)
from unilabos.workflow.store import (
    StoreAuthoringConflict,
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
_WORKFLOW_READ_FIELDS = {
    "uuid",
    "create_time",
    "update_time",
    "meta_data",
    "name",
    "tags",
    "revision",
    "description",
}
_NODE_TEMPLATE_READ_FIELDS = {
    "uuid",
    "create_time",
    "update_time",
    "meta_data",
    "resource_template_uuid",
    "name",
    "display_name",
    "goal",
    "goal_default",
    "feedback",
    "result",
    "type",
    "node_type",
    "description",
    "class",
    "schema",
    "icon",
    "header",
    "footer",
}
_HANDLE_TEMPLATE_READ_FIELDS = {
    "uuid",
    "create_time",
    "update_time",
    "meta_data",
    "workflow_node_template_uuid",
    "handle_key",
    "io_type",
    "display_name",
    "type",
    "required",
    "description",
    "data_source",
    "data_key",
}
_WORKFLOW_REQUIRED_READ_FIELDS = _WORKFLOW_READ_FIELDS - {"description"}
_NODE_REQUIRED_READ_FIELDS = {
    "uuid",
    "create_time",
    "update_time",
    "meta_data",
    "workflow_uuid",
    "name",
    "status",
    "type",
    "pose",
    "param",
    "execution_policy",
    "disabled",
    "minimized",
}
_EDGE_REQUIRED_READ_FIELDS = {
    "uuid",
    "create_time",
    "update_time",
    "meta_data",
    "source_node_uuid",
    "target_node_uuid",
    "source_handle_uuid",
    "target_handle_uuid",
}
_NODE_TEMPLATE_REQUIRED_READ_FIELDS = {
    "uuid",
    "create_time",
    "update_time",
    "meta_data",
    "resource_template_uuid",
    "name",
    "display_name",
    "goal",
    "goal_default",
    "feedback",
    "result",
    "type",
    "node_type",
}
_HANDLE_TEMPLATE_REQUIRED_READ_FIELDS = {
    "uuid",
    "create_time",
    "update_time",
    "meta_data",
    "workflow_node_template_uuid",
    "handle_key",
    "io_type",
    "display_name",
    "type",
    "required",
}


class WorkflowError(RuntimeError):
    """面向前端的稳定 Workflow 错误。"""

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


class DeviceActionRunBridge(Protocol):
    """设备单动作运行（DeviceActionRun）的本地执行端口。"""

    def submit(self, aggregate: Dict[str, Any]) -> None:
        """提交首次创建的 Task/Job 聚合；参数是标准持久投影，返回无。"""

        ...

    def close(self) -> None:
        """释放执行端口持有的生命周期监听；返回无且必须幂等。"""

        ...


def _sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _canonical_json(value: Any) -> bytes:
    return encode_json(value, sort_keys=True)


def _mtime_rfc3339(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


class WorkflowService:
    """协调 SQLite 事实、package Draft 文件与编译器状态。"""

    def __init__(
        self,
        store: WorkflowStore,
        *,
        compiler: Optional[AuthoringCompiler] = None,
        material_resolver: Optional[
            Callable[[str], Optional[Dict[str, Any]]]
        ] = None,
        device_action_run_bridge: Optional[DeviceActionRunBridge] = None,
    ):
        """装配本地工作流应用服务。

        参数：``store`` 是唯一工作流写模型；``compiler`` 负责编译可信工作流源码；
        ``material_resolver`` 按物料 UUID 读取活动物料身份，供设备单动作运行
        （DeviceActionRun）关闭式校验；``device_action_run_bridge`` 把首次创建的
        标准 Task/Job 提交到本地执行内核。返回无。
        """

        self._store = store
        self.compiler = compiler
        self._device_action_runs = DeviceActionRunService(
            store,
            material_resolver=material_resolver,
        )
        # ``device_action_run_bridge`` 是可选本地执行端口；Backend-controlled
        # 模式不装配它，避免 OS 形成第二个生产调度权威（Scheduler Authority）。
        self._device_action_run_bridge = device_action_run_bridge
        self._locks_guard = threading.Lock()
        self._authoring_locks: Dict[str, threading.RLock] = {}
        # ``_active_source_workflow_uuids`` 只表达本次进程启动配置授权的工作流
        # 源码（Workflow Source）；SQLite 注册行仅保留跨启动历史身份。
        self._active_sources_lock = threading.RLock()
        self._active_source_workflow_uuids: frozenset[str] = frozenset()

    # Workflow 与 Graph --------------------------------------------------

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
            tags = normalize_json_array(tags)
            meta_data = normalize_json_object(meta_data)
            public_meta_data = dict(meta_data)
            public_meta_data.pop("unilab", None)
            return self._store.create_workflow(
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
            identity = validate_uuid(workflow_uuid)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        try:
            return self._store.get_workflow(identity)
        except StoreNotFound:
            raise WorkflowError("not_found") from None

    def list_workflows(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        name: str = "",
    ) -> Dict[str, Any]:
        page, page_size = self._normalize_page(page, page_size)
        return self._store.list_workflows(page=page, page_size=page_size, name=name)

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
            try:
                name = name.strip()
                if not name:
                    raise ValueError("workflow name must not be blank")
                tags = normalize_json_array(tags)
                public_meta_data = dict(normalize_json_object(meta_data))
            except (AttributeError, TypeError, ValueError):
                raise WorkflowError("invalid_input") from None
            public_meta_data.pop("unilab", None)
            if "unilab" in current["meta_data"]:
                public_meta_data["unilab"] = current["meta_data"]["unilab"]
            return self._store.update_workflow(
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
            self._store.delete_workflow(identity)

    def get_graph(self, workflow_uuid: str) -> Dict[str, Any]:
        identity = self.get_workflow(workflow_uuid)["uuid"]
        return self._validated_applied_backend_graph(
            self._store.get_graph(identity),
        )

    def save_graph(
        self,
        workflow_uuid: str,
        *,
        revision: int,
        nodes: List[WorkflowNodeWrite | Dict[str, Any]],
        edges: List[WorkflowEdgeWrite | Dict[str, Any]],
    ) -> Dict[str, Any]:
        """以严格工作流输入/输出（Workflow I/O）合同保存完整图。

        参数说明：`workflow_uuid` 是工作流稳定身份，`revision` 是预期版本，
        `nodes/edges` 是完整替换集合。旧形状只在存储适配器（Store Adapter）
        内兼容，公共服务入口始终使用同一个严格校验深模块。
        """

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
                return self._store.save_graph(
                    identity,
                    revision=revision,
                    nodes=node_values,
                    edges=edge_values,
                    protect_reserved_metadata=True,
                    validate_workflow_io_contract=True,
                )
            except ValidationError:
                raise WorkflowError("invalid_input") from None
            except StoreRevisionConflict:
                raise WorkflowConflict("conflict") from None
            except StoreNotFound:
                raise WorkflowError("not_found") from None
            except StoreConflict:
                raise WorkflowError("invalid_input") from None

    # WorkflowTask 与 WorkflowNodeJob -----------------------------------

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
        run_mode = "normal" if run_mode == "" else run_mode
        if run_mode not in {"normal", "step", "single_node"}:
            raise WorkflowError("invalid_input")
        if target_node_uuid is not None:
            try:
                target_node_uuid = validate_uuid(target_node_uuid)
            except ValueError:
                raise WorkflowError("invalid_input") from None
        try:
            input_value = normalize_json_object(input_value)
            meta_data = normalize_json_object(meta_data)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        # P0-2 已冻结合同；生产 schema/compiler 属于 Phase 02。本阶段镜像
        # Backend baseline 的空 Task input，不提前持久化未实现的解释。
        if input_value:
            raise WorkflowError("invalid_input")
        description = self._optional_text(description)
        try:
            return self._store.create_task_with_jobs(
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

    def create_device_action_run(
        self,
        *,
        material_uuid: str,
        workflow_node_template_uuid: str,
        param: Optional[Dict[str, Any]],
        execution_policy: Optional[Dict[str, Any]],
        idempotency_key: str,
        description: Optional[str],
        meta_data: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """创建或幂等复用设备单动作运行（DeviceActionRun）。

        参数与 Backend ``POST /device-action-runs`` DTO 同名；返回标准工作流任务
        （WorkflowTask）、唯一工作流节点作业（WorkflowNodeJob）和 ``created``。
        输入/引用错误、依赖未装配和幂等冲突分别映射为稳定 HTTP 业务错误。
        """

        try:
            # ``aggregate`` 是已原子持久化的标准工作流任务（WorkflowTask）和
            # 工作流节点作业（WorkflowNodeJob）；只有首次创建才允许物理派发。
            aggregate = self._device_action_runs.create(
                material_uuid=material_uuid,
                workflow_node_template_uuid=workflow_node_template_uuid,
                param=param,
                execution_policy=execution_policy,
                idempotency_key=idempotency_key,
                description=description,
                meta_data=meta_data,
            )
            if (
                aggregate["created"] is True
                and self._device_action_run_bridge is not None
            ):
                self._device_action_run_bridge.submit(aggregate)
                # 旧调度器可能同步完成首次派发，必须返回刷新后的权威状态，不能把
                # 创建事务中的 ``pending`` 快照误报给前端。
                aggregate = {
                    "task": self._store.get_task(aggregate["task"]["uuid"]),
                    "job": self._store.get_job(aggregate["job"]["uuid"]),
                    "created": True,
                }
            return aggregate
        except DeviceActionRunInputError:
            raise WorkflowError("invalid_input") from None
        except DeviceActionRunUnavailable:
            raise WorkflowError("template_catalog_unavailable") from None
        except DeviceActionRunConflict:
            raise WorkflowConflict("conflict") from None

    def get_workflow_task(self, task_uuid: str) -> Dict[str, Any]:
        try:
            identity = validate_uuid(task_uuid)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        try:
            return self._store.get_task(identity)
        except StoreNotFound:
            raise WorkflowError("not_found") from None

    def list_workflow_tasks(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        workflow_uuid: Optional[str] = None,
        execution_kind: str = "",
        status: str = "",
        cleanup_status: str = "",
    ) -> Dict[str, Any]:
        """按 Backend 查询合同分页读取工作流任务（WorkflowTask）。

        参数：分页字段限定结果窗口；``workflow_uuid`` 限定工作流定义；
        ``execution_kind`` 区分工作流与直接设备动作来源；状态字段限定业务和清理
        生命周期。返回分页投影，非法枚举或 UUID 映射为稳定输入错误。
        """

        page, page_size = self._normalize_page(page, page_size)
        if workflow_uuid is not None:
            try:
                workflow_uuid = validate_uuid(workflow_uuid)
            except ValueError:
                raise WorkflowError("invalid_input") from None
        status = status.strip().lower()
        execution_kind = execution_kind.strip().lower()
        cleanup_status = cleanup_status.strip().lower()
        if execution_kind and execution_kind not in {
            "workflow",
            "ad_hoc_device_action",
        }:
            raise WorkflowError("invalid_input")
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
        return self._store.list_tasks(
            page=page,
            page_size=page_size,
            workflow_uuid=workflow_uuid,
            execution_kind=execution_kind,
            status=status,
            cleanup_status=cleanup_status,
        )

    def list_workflow_node_jobs(self, task_uuid: str) -> List[Dict[str, Any]]:
        identity = self.get_workflow_task(task_uuid)["uuid"]
        return self._store.list_jobs(identity)

    def get_workflow_node_job(self, job_uuid: str) -> Dict[str, Any]:
        try:
            identity = validate_uuid(job_uuid)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        try:
            return self._store.get_job(identity)
        except StoreNotFound:
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

    def register_discovered_sources(
        self,
        plan: EditableSourceDiscoveryPlan,
    ) -> List[Dict[str, Any]]:
        """原子注册一个完整工作流源码（Workflow Source）发现计划。

        参数：``plan`` 是从全部显式授权目录完成预校验后生成的不可变计划。
        返回：按计划顺序排列的持久来源记录。
        异常：缺失工作流映射为 ``workflow_not_found``；来源身份或目录安全冲突
        分别映射为稳定 ``invalid_input`` 错误，且不提交任何部分注册。
        """

        if not isinstance(plan, EditableSourceDiscoveryPlan):
            raise WorkflowError("invalid_input")
        # ``root_paths`` 是计划声称已固定的全部包目录；每项注册必须且只能引用它们。
        root_paths = tuple(
            package_root for package_root, _identity in plan.root_identities
        )
        registered_roots = {
            registration.package_root for registration in plan.registrations
        }
        if (
            len(root_paths) != len(set(root_paths))
            or any(not package_root.is_absolute() for package_root in root_paths)
            or registered_roots != set(root_paths)
            or any(
                registration.source_uri
                != (
                    f"package://{registration.package_id}/"
                    f"{registration.relative_path}"
                )
                for registration in plan.registrations
            )
        ):
            raise WorkflowError("invalid_input")
        # ``workflow_uuids`` 以稳定顺序取得所有创作锁，避免与草稿操作交叉提交。
        workflow_uuids = sorted(
            {registration.workflow_uuid for registration in plan.registrations}
        )
        # ``registration_rows`` 是交给 SQLite 写模型的完整、不可变批次。
        registration_rows = tuple(
            {
                "workflow_uuid": registration.workflow_uuid,
                "package_id": registration.package_id,
                "package_root": str(registration.package_root),
                "relative_path": registration.relative_path,
                "source_uri": registration.source_uri,
            }
            for registration in plan.registrations
        )
        with ExitStack() as locks:
            for workflow_uuid in workflow_uuids:
                locks.enter_context(self._authoring_lock(workflow_uuid))
            try:
                with pin_package_roots(plan.root_identities) as pinned_roots:
                    registered = self._store.register_sources(
                        registration_rows,
                        before_commit=pinned_roots.assert_current,
                    )
            except StoreNotFound:
                raise WorkflowError("workflow_not_found") from None
            except SourceWorkspaceError:
                raise WorkflowError("invalid_input") from None
            except StoreConflict:
                raise WorkflowConflict("invalid_input") from None
        # 只有完整 SQLite 注册事务成功后才一次替换当前授权集合；失败计划不得
        # 残留部分活动来源，也不得沿用上一次计划中已经撤销的来源。
        active_workflow_uuids = frozenset(workflow_uuids)
        with self._active_sources_lock:
            self._active_source_workflow_uuids = active_workflow_uuids
        return registered

    def register_editable_source(
        self,
        *,
        workflow_uuid: str,
        package_id: str,
        package_root: str | Path,
        relative_path: str,
    ) -> Dict[str, Any]:
        """把工作流（Workflow）绑定到一个受限的本地 Python 源码路径。

        参数：工作流 UUID 是已有定义身份；包身份、包目录和相对路径共同形成
        工作流源码（Workflow Source）的稳定来源身份。
        返回：持久化后的来源注册记录。
        异常：身份不存在、路径不安全或唯一性冲突时返回稳定工作流错误。
        """

        workflow_uuid = self._get_authoring_workflow(workflow_uuid)["uuid"]
        try:
            root, normalized_relative_path = validate_source_registration(
                package_root=package_root,
                relative_path=relative_path,
            )
            root_metadata = root.lstat()
        except (OSError, SourceWorkspaceError):
            raise WorkflowError("invalid_input") from None
        if not isinstance(package_id, str) or not package_id.strip():
            raise WorkflowError("invalid_input")
        normalized_package_id = package_id.strip()
        source_uri = (
            f"package://{normalized_package_id}/{normalized_relative_path}"
        )
        # 单项兼容入口构造成与启动发现完全相同的不可变计划，避免绕过物理路径、
        # 来源 URI 和“既有身份不可重绑定”等批量注册不变量。
        plan = EditableSourceDiscoveryPlan(
            registrations=(
                EditableSourceRegistration(
                    workflow_uuid=workflow_uuid,
                    package_id=normalized_package_id,
                    package_root=root,
                    relative_path=normalized_relative_path,
                    source_uri=source_uri,
                ),
            ),
            root_identities=(
                ((root, (root_metadata.st_dev, root_metadata.st_ino))),
            ),
        )
        return self.register_discovered_sources(plan)[0]

    def list_registered_sources(self) -> List[Dict[str, Any]]:
        """返回本次进程配置仍授权的工作流源码（Workflow Source）。

        参数：无。返回：按稳定工作流 UUID 排序的活动注册；持久历史注册不会因
        数据库中仍存在就自动获得当前路径访问权。
        """

        with self._active_sources_lock:
            active_workflow_uuids = self._active_source_workflow_uuids
        return [
            registration
            for registration in self._store.list_source_registrations()
            if registration["workflow_uuid"] in active_workflow_uuids
        ]

    def recover_registered_sources(self) -> None:
        """启动时逐一恢复当前授权源码并隔离单项瞬态故障。

        参数：无。返回：无；只读取本轮活动注册，历史路径不会被探测。
        """

        for registration in self.list_registered_sources():
            try:
                self.reconcile_registered_source(registration["workflow_uuid"])
            except (OSError, RuntimeError):
                continue

    def close(self) -> None:
        """关闭本地执行桥和由该 Service 独占的工作流持久存储。"""

        if self._device_action_run_bridge is not None:
            self._device_action_run_bridge.close()
        self._store.close()

    def get_authoring(self, workflow_uuid: str) -> Dict[str, Any]:
        workflow_uuid = self._get_authoring_workflow(workflow_uuid)["uuid"]
        with self._authoring_lock(workflow_uuid):
            workflow = self._get_authoring_workflow(workflow_uuid)
            registration = self._registration(workflow_uuid)
            source = self._read_source(registration)
            graph = self.get_graph(workflow_uuid)
            record = self._store.get_authoring_record(workflow_uuid)
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
            applied_graph = self.get_graph(workflow_uuid)
            compilation = self._compile(
                workflow=workflow,
                graph=applied_graph,
                registration=registration,
                python_source=source["python_source"],
            )
            candidate = self._issue_candidate(
                workflow_revision=workflow["revision"],
                draft_hash=source["draft_hash"],
                compilation=compilation,
                applied_graph=applied_graph,
                draft_python_source=source["python_source"],
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
            self._store.record_draft_compilation(
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
            record = self._store.get_authoring_record(workflow_uuid)
            applied_source = record.get("applied_source")
            writeback_marker_valid = (
                record.get("writeback_source") is not None
                and record.get("writeback_expected_hash") is not None
                and record.get("writeback_generation") is not None
            )
            if (
                record["writeback_status"] == "pending"
                and writeback_marker_valid
                and source is not None
                and applied_source is not None
                and source["draft_hash"] == applied_source["source_hash"]
            ):
                self._store.settle_writeback(
                    workflow_uuid=workflow_uuid,
                    expected_writeback_source=record["writeback_source"],
                    expected_writeback_hash=record["writeback_expected_hash"],
                    expected_writeback_generation=record["writeback_generation"],
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
            if (
                record["writeback_status"] == "pending"
                and writeback_marker_valid
                and (
                    source is None
                    or source["draft_hash"] == record["writeback_expected_hash"]
                )
            ):
                recovery_source = record.get("writeback_source")
                if recovery_source is not None:
                    try:
                        recovery_bytes = recovery_source.encode("utf-8")
                        recovery_hash = _sha256(recovery_bytes)
                        self._atomic_write(
                            registration,
                            recovery_bytes,
                            expected_hash=(
                                source["draft_hash"] if source is not None else None
                            ),
                        )
                        source = self._read_source(registration)
                        if source is not None and source["draft_hash"] == recovery_hash:
                            self._store.settle_writeback(
                                workflow_uuid=workflow_uuid,
                                expected_writeback_source=record["writeback_source"],
                                expected_writeback_hash=record[
                                    "writeback_expected_hash"
                                ],
                                expected_writeback_generation=record[
                                    "writeback_generation"
                                ],
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
                        return self.get_authoring(workflow_uuid)
            actual_hash = source["draft_hash"] if source is not None else None
            invalid_writeback_marker = (
                record["writeback_status"] == "pending" and not writeback_marker_valid
            )
            if (
                actual_hash == record["observed_draft_hash"]
                and not invalid_writeback_marker
                and not (actual_hash is None and record.get("candidate") is not None)
            ):
                return self.get_authoring(workflow_uuid)

            candidate: Optional[Dict[str, Any]] = None
            diagnostics: List[Dict[str, Any]] = []
            if source is not None:
                applied_graph = self.get_graph(workflow_uuid)
                compilation = self._compile(
                    workflow=workflow,
                    graph=applied_graph,
                    registration=registration,
                    python_source=source["python_source"],
                )
                diagnostics = compilation.diagnostics
                candidate = self._issue_candidate(
                    workflow_revision=workflow["revision"],
                    draft_hash=source["draft_hash"],
                    compilation=compilation,
                    applied_graph=applied_graph,
                    draft_python_source=source["python_source"],
                )
            cause = (
                "recovered"
                if source is not None
                and record["observed_draft_hash"] is None
                and record["update_time"] is not None
                else "external_draft_changed"
            )
            self._store.record_draft_compilation(
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

    def submit_source_change(
        self,
        workflow_uuid: str,
        *,
        observed_signature: Tuple[Any, ...],
    ) -> bool:
        """提交一个稳定观测到的工作流源码（Workflow Source）变化命令。

        参数：``workflow_uuid`` 是已注册来源绑定的稳定工作流身份；
        ``observed_signature`` 是源码监视器（Source Monitor）去抖后的文件世代。
        返回：只有相同文件世代完成哈希去重、候选推进及待写回恢复时才为
        ``True``；文件并发变化或持久恢复仍待处理时返回 ``False``。读取、编译或
        持久化异常原样映射为稳定工作流错误，调用者不得把异常视为已确认。
        """

        workflow_uuid = self._get_authoring_workflow(workflow_uuid)["uuid"]
        with self._authoring_lock(workflow_uuid):
            registration = self._registration(workflow_uuid)
            # ``current_signature`` 是服务在取得创作锁后复核的文件世代，防止监视
            # 线程用过期观测授权编译更新中的文件。
            current_signature = self.source_signature(registration)
            if current_signature != observed_signature:
                return False
            self.reconcile_registered_source(workflow_uuid)
            # ``latest_signature`` 证明整个状态推进期间规范源码没有再次变化。
            latest_signature = self.source_signature(registration)
            if latest_signature != observed_signature:
                return False
            record = self._store.get_authoring_record(workflow_uuid)
            return record["writeback_status"] != "pending"

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

            # D-079 固定了这里的冲突顺序。
            if actual_hash != expected_draft_hash:
                raise WorkflowConflict("draft_hash_conflict")
            if workflow["revision"] != expected_workflow_revision:
                raise WorkflowConflict("workflow_revision_conflict")

            record = self._store.get_authoring_record(workflow_uuid)
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

            applied_graph = self.get_graph(workflow_uuid)
            compilation = self._compile(
                workflow=workflow,
                graph=applied_graph,
                registration=registration,
                python_source=source["python_source"],
            )
            if not self._normalize_candidate_diagnostics(
                compilation,
                python_source=source["python_source"],
            ):
                raise WorkflowError("candidate_invalid")
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
                applied_graph=applied_graph,
                draft_python_source=source["python_source"],
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

            def validate_authorities() -> None:
                latest_source = self._read_source(registration)
                if (
                    latest_source is None
                    or latest_source["draft_hash"] != expected_draft_hash
                ):
                    raise WorkflowConflict("draft_hash_conflict")
                if (
                    self._catalog_fingerprint()
                    != candidate["template_catalog_fingerprint"]
                ):
                    raise WorkflowConflict("template_catalog_conflict")

            validate_authorities()

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
                (
                    resulting_revision,
                    writeback_generation,
                ) = self._store.apply_authoring_candidate(
                    workflow_uuid=workflow_uuid,
                    expected_revision=previous_revision,
                    expected_draft_hash=expected_draft_hash,
                    expected_candidate_hash=expected_candidate_hash,
                    expected_catalog_fingerprint=candidate[
                        "template_catalog_fingerprint"
                    ],
                    candidate=candidate,
                    applied_source=applied_source,
                    event_data={
                        "workflow_uuid": workflow_uuid,
                        "cause": "applied",
                        "draft_hash": normalized_hash,
                        "candidate_hash": None,
                    },
                )
            except StoreAuthoringConflict as error:
                raise WorkflowConflict(error.code) from None
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
                        marker_owned = self._store.mark_writeback_pending(
                            workflow_uuid=workflow_uuid,
                            expected_writeback_source=normalized_source,
                            expected_writeback_hash=actual_hash,
                            expected_writeback_generation=writeback_generation,
                        )
                        if not marker_owned:
                            # 新 Apply/Draft 已接管 marker，旧 generation 不再重试。
                            return
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
                if written["draft_hash"] != normalized_hash:
                    raise WorkflowConflict("draft_hash_conflict")
            except Exception:  # noqa: BLE001 - 主事务已提交
                # 主事务已经提交。之后任何文件系统、数据库或聚合错误
                # 都只能降级为可恢复警告，不能把成功伪装成失败。
                warn_writeback()
                mark_pending_best_effort()
            else:
                settled = False
                for _attempt in range(2):
                    try:
                        marker_owned = self._store.settle_writeback(
                            workflow_uuid=workflow_uuid,
                            expected_writeback_source=normalized_source,
                            expected_writeback_hash=actual_hash,
                            expected_writeback_generation=writeback_generation,
                            observed_draft_hash=written["draft_hash"],
                            draft_update_time=written["update_time"],
                        )
                        if not marker_owned:
                            # 新 generation 已接管；陈旧 settle 无需恢复。
                            settled = True
                            break
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
                        fallback_graph = self.get_graph(workflow_uuid)
                        fallback_workflow = fallback_graph["workflow"]
                        fallback_record = self._store.get_authoring_record(
                            workflow_uuid
                        )
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
            "items": self._store.list_events(after_id=after_id, limit=limit),
            "after_id": after_id,
        }

    # Authoring 内部实现 -------------------------------------------------

    def _get_authoring_workflow(
        self,
        workflow_uuid: str,
    ) -> Dict[str, Any]:
        try:
            identity = validate_uuid(workflow_uuid)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        try:
            return self._store.get_workflow(identity)
        except StoreNotFound:
            raise WorkflowError("workflow_not_found") from None

    def _registration(self, workflow_uuid: str) -> Dict[str, Any]:
        """读取当前进程仍授权的规范源码注册。

        参数：``workflow_uuid`` 是已校验的工作流稳定身份。返回：当前活动来源
        注册。异常：未在本次启动 allowlist 中授权或历史行缺失时统一抛出
        ``workflow_not_found``，且在拒绝前不触碰持久路径。
        """

        with self._active_sources_lock:
            if workflow_uuid not in self._active_source_workflow_uuids:
                raise WorkflowError("workflow_not_found")
        try:
            return self._store.get_source_registration(workflow_uuid)
        except StoreNotFound:
            raise WorkflowError("workflow_not_found") from None

    def _read_source(
        self,
        registration: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """通过源码工作区（SourceWorkspace）读取一项已注册草稿。

        参数：``registration`` 是持久化来源身份。
        返回：缺失时为 ``None``，否则返回源码、草稿哈希和修改时间字典。
        异常：不安全或超限文件映射为 ``invalid_input``。
        """

        try:
            source = read_registered_source(registration)
        except SourceWorkspaceError:
            raise WorkflowError("invalid_input") from None
        if source is None:
            return None
        return {
            "python_source": source.python_source,
            "draft_hash": source.draft_hash,
            "update_time": source.update_time,
        }

    def source_signature(
        self,
        registration: Dict[str, Any],
    ) -> Tuple[Any, ...]:
        """返回无需读取内容的稳定签名，供源码监视器（Source Monitor）去抖。

        参数：``registration`` 是持久化来源身份。
        返回：缺失标记或普通文件的身份、大小和时间签名。
        异常：不安全路径或非普通文件映射为 ``invalid_input``。
        """

        try:
            return registered_source_signature(registration)
        except SourceWorkspaceError:
            raise WorkflowError("invalid_input") from None

    def _atomic_write(
        self,
        registration: Dict[str, Any],
        content: bytes,
        *,
        expected_hash: Any = _NO_EXPECTED_HASH,
    ) -> None:
        """通过源码工作区（SourceWorkspace）执行原子 CAS 草稿写入。

        参数：``registration`` 是来源身份；``content`` 是 UTF-8 源码字节；
        ``expected_hash`` 是可选原稿哈希条件。
        返回：无；成功时规范源码完整替换。
        异常：CAS 变化映射为 ``draft_hash_conflict``，其他失败映射为稳定错误。
        """

        try:
            write_registered_source(
                registration,
                content,
                expected_hash=expected_hash,
            )
        except SourceWorkspaceConflict:
            raise WorkflowConflict("draft_hash_conflict") from None
        except SourceWorkspaceError as error:
            error_code = (
                "invalid_input" if error.code == "invalid_input" else "internal_error"
            )
            raise WorkflowError(error_code) from None

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
        if not isinstance(value, str) or _HASH_TOKEN.fullmatch(value) is None:
            raise WorkflowError("template_catalog_unavailable")
        return value

    def _issue_candidate(
        self,
        *,
        workflow_revision: int,
        draft_hash: str,
        compilation: CandidateCompilation,
        applied_graph: Dict[str, Any],
        draft_python_source: str,
    ) -> Optional[Dict[str, Any]]:
        applied_graph = self._validated_applied_backend_graph(applied_graph)
        if not self._normalize_candidate_diagnostics(
            compilation,
            python_source=draft_python_source,
        ):
            return None
        if not compilation.valid:
            return None
        assert compilation.graph is not None
        try:
            graph = self._backend_candidate_graph(
                compilation.graph,
                applied_graph=applied_graph,
            )
            if not isinstance(compilation.source_map, list):
                raise ValueError
            source_map = [
                CandidateSourceMapEntry.model_validate(item).model_dump()
                for item in compilation.source_map
            ]
            if not source_ranges_fit(
                compilation.normalized_python_source,
                source_map,
            ):
                raise ValueError
            changeset = CandidateChangeset.model_validate(
                compilation.changeset,
            ).model_dump()
            validate_candidate_bundle(
                graph=graph,
                base_graph=applied_graph,
                workflow_uuid=graph["workflow"]["uuid"],
                revision=workflow_revision,
                source_map=source_map,
                changeset=changeset,
            )
            compiler_version = compilation.compiler_version
            if not compiler_version.strip():
                raise ValueError
            template_catalog_fingerprint = compilation.template_catalog_fingerprint
            if _HASH_TOKEN.fullmatch(template_catalog_fingerprint) is None:
                raise ValueError
        except (
            GraphValidationError,
            CandidateBundleError,
            KeyError,
            TypeError,
            ValidationError,
            ValueError,
            WorkflowError,
        ) as error:
            if isinstance(error, WorkflowError) and error.code != "candidate_invalid":
                raise
            self._set_candidate_invalid_diagnostic(compilation)
            return None
        bundle = {
            "base_workflow_revision": workflow_revision,
            "draft_hash": draft_hash,
            "graph": graph,
            "normalized_python_source": compilation.normalized_python_source,
            "source_map": source_map,
            "changeset": changeset,
            "compiler_version": compiler_version,
            "template_catalog_fingerprint": template_catalog_fingerprint,
        }
        try:
            canonical_bundle = _canonical_json(bundle)
        except (TypeError, UnicodeError, ValueError):
            self._set_candidate_invalid_diagnostic(compilation)
            return None
        return {
            "candidate_hash": _sha256(canonical_bundle),
            **bundle,
            "update_time": utc_now(),
        }

    @staticmethod
    def _set_candidate_invalid_diagnostic(
        compilation: CandidateCompilation,
    ) -> None:
        compilation.diagnostics = [
            {
                "severity": "error",
                "code": "candidate_invalid",
                "message": _ERRORS["candidate_invalid"][1],
            }
        ]

    @classmethod
    def _normalize_candidate_diagnostics(
        cls,
        compilation: CandidateCompilation,
        *,
        python_source: str,
    ) -> bool:
        try:
            if not isinstance(compilation.diagnostics, list):
                raise ValueError
            compilation.diagnostics = [
                CandidateDiagnostic.model_validate(item).model_dump(
                    exclude_none=True,
                )
                for item in compilation.diagnostics
            ]
            source_ranges = [
                item["source_range"]
                for item in compilation.diagnostics
                if item.get("source_range") is not None
            ]
            if not source_ranges_fit(python_source, source_ranges):
                raise ValueError
        except (TypeError, ValidationError, ValueError):
            cls._set_candidate_invalid_diagnostic(compilation)
            return False
        return True

    @staticmethod
    def _backend_graph_projection(
        graph: Dict[str, Any],
    ) -> Dict[str, Any]:
        """按 Backend JSON omitempty 语义投影 Candidate。"""

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
    def _backend_candidate_graph(
        cls,
        graph: Dict[str, Any],
        *,
        applied_graph: Dict[str, Any],
    ) -> Dict[str, Any]:
        """把编译器写实体补全为冻结的 Backend 读取形状。"""

        applied = cls._validated_applied_backend_graph(applied_graph)
        cls._require_candidate_graph_containers(graph)
        projected = cls._backend_graph_projection(graph)
        applied_workflow = applied["workflow"]
        workflow_uuid = applied_workflow["uuid"]
        timestamp = applied_workflow["update_time"]
        applied_nodes = {item["uuid"]: item for item in applied["nodes"]}
        applied_edges = {item["uuid"]: item for item in applied["edges"]}
        applied_node_templates = {
            item["uuid"]: item for item in applied["node_templates"]
        }
        applied_handle_templates = {
            item["uuid"]: item for item in applied["handle_templates"]
        }

        nodes = []
        for item in projected["nodes"]:
            value = WorkflowNodeWrite.model_validate(item).model_dump(
                exclude_none=True,
            )
            persisted = applied_nodes.get(value["uuid"], {})
            nodes.append(
                {
                    "uuid": value["uuid"],
                    "create_time": persisted.get("create_time", timestamp),
                    "update_time": persisted.get("update_time", timestamp),
                    "meta_data": value.get("meta_data", {}),
                    "workflow_uuid": workflow_uuid,
                    **value,
                }
            )
        cls._require_backend_read_fields(
            nodes,
            _NODE_REQUIRED_READ_FIELDS,
        )

        edges = []
        for item in projected["edges"]:
            value = WorkflowEdgeWrite.model_validate(item).model_dump(
                exclude_none=True,
            )
            persisted = applied_edges.get(value["uuid"], {})
            edges.append(
                {
                    "uuid": value["uuid"],
                    "create_time": persisted.get("create_time", timestamp),
                    "update_time": persisted.get("update_time", timestamp),
                    "meta_data": value.get("meta_data", {}),
                    **value,
                }
            )
        cls._require_backend_read_fields(
            edges,
            _EDGE_REQUIRED_READ_FIELDS,
        )

        projected["workflow"] = {
            key: value
            for key, value in {
                **applied_workflow,
                **projected["workflow"],
                "uuid": workflow_uuid,
                "create_time": applied_workflow["create_time"],
                "update_time": timestamp,
            }.items()
            if key in _WORKFLOW_READ_FIELDS
        }
        projected["nodes"] = nodes
        projected["edges"] = edges
        projected["node_templates"] = cls._hydrate_backend_catalog_entities(
            projected["node_templates"],
            persisted=applied_node_templates,
            timestamp=timestamp,
            uuid_fields={"uuid", "resource_template_uuid"},
            allowed_fields=_NODE_TEMPLATE_READ_FIELDS,
            required_fields=_NODE_TEMPLATE_REQUIRED_READ_FIELDS,
        )
        projected["handle_templates"] = cls._hydrate_backend_catalog_entities(
            projected["handle_templates"],
            persisted=applied_handle_templates,
            timestamp=timestamp,
            uuid_fields={"uuid", "workflow_node_template_uuid"},
            allowed_fields=_HANDLE_TEMPLATE_READ_FIELDS,
            required_fields=_HANDLE_TEMPLATE_REQUIRED_READ_FIELDS,
        )
        cls._require_backend_read_fields(
            [projected["workflow"]],
            _WORKFLOW_REQUIRED_READ_FIELDS,
        )
        try:
            cls._require_backend_entity_types(projected)
        except (AttributeError, KeyError, TypeError, ValueError):
            raise WorkflowError("candidate_invalid") from None
        if projected["workflow"]["revision"] != applied_workflow["revision"]:
            raise WorkflowError("candidate_invalid")
        return projected

    @classmethod
    def _validated_applied_backend_graph(
        cls,
        graph: Dict[str, Any],
    ) -> Dict[str, Any]:
        """检查 Candidate 前先校验 Authority 持有的工作流图。"""

        try:
            applied = cls._backend_graph_projection(graph)
            cls._require_backend_read_fields(
                [applied["workflow"]],
                _WORKFLOW_REQUIRED_READ_FIELDS,
                error_code="internal_error",
            )
            cls._require_backend_read_fields(
                applied["nodes"],
                _NODE_REQUIRED_READ_FIELDS,
                error_code="internal_error",
            )
            cls._require_backend_read_fields(
                applied["edges"],
                _EDGE_REQUIRED_READ_FIELDS,
                error_code="internal_error",
            )
            cls._require_backend_read_fields(
                applied["node_templates"],
                _NODE_TEMPLATE_REQUIRED_READ_FIELDS,
                error_code="internal_error",
            )
            cls._require_backend_read_fields(
                applied["handle_templates"],
                _HANDLE_TEMPLATE_REQUIRED_READ_FIELDS,
                error_code="internal_error",
            )
            cls._require_backend_entity_types(applied)
            return applied
        except WorkflowError:
            raise
        except (AttributeError, KeyError, TypeError, ValueError):
            raise WorkflowError("internal_error") from None

    @staticmethod
    def _require_backend_entity_types(graph: Dict[str, Any]) -> None:
        """在完整工作流图上强制执行冻结的 Backend JSON 类型。"""

        def exact(entity: Dict[str, Any], fields: set[str], expected: type) -> None:
            if any(type(entity[field]) is not expected for field in fields):
                raise ValueError

        def optional(
            entity: Dict[str, Any],
            fields: set[str],
            expected: type,
        ) -> None:
            if any(
                field in entity and type(entity[field]) is not expected
                for field in fields
            ):
                raise ValueError

        def uuids(entity: Dict[str, Any], fields: set[str]) -> None:
            exact(entity, fields, str)
            for field in fields:
                validate_uuid(entity[field])

        def optional_uuids(entity: Dict[str, Any], fields: set[str]) -> None:
            for field in fields:
                if field in entity:
                    uuids(entity, {field})

        workflow = graph["workflow"]
        uuids(workflow, {"uuid"})
        exact(workflow, {"create_time", "update_time", "name"}, str)
        exact(workflow, {"meta_data"}, dict)
        exact(workflow, {"tags"}, list)
        normalize_json_object(workflow["meta_data"])
        normalize_json_array(workflow["tags"])
        optional(workflow, {"description"}, str)
        revision = workflow["revision"]
        if type(revision) is not int or not 1 <= revision <= (1 << 63) - 1:
            raise ValueError

        for node in graph["nodes"]:
            uuids(node, {"uuid", "workflow_uuid"})
            optional_uuids(
                node,
                {
                    "workflow_node_template_uuid",
                    "parent_uuid",
                    "material_uuid",
                },
            )
            exact(
                node,
                {"create_time", "update_time", "name", "status", "type"},
                str,
            )
            exact(
                node,
                {"meta_data", "pose", "param", "execution_policy"},
                dict,
            )
            for field in ("meta_data", "pose", "param", "execution_policy"):
                normalize_json_object(node[field])
            exact(node, {"disabled", "minimized"}, bool)
            optional(
                node,
                {
                    "description",
                    "icon",
                    "footer",
                    "action_name",
                    "action_type",
                    "script",
                },
                str,
            )

        for edge in graph["edges"]:
            uuids(
                edge,
                {
                    "uuid",
                    "source_node_uuid",
                    "target_node_uuid",
                    "source_handle_uuid",
                    "target_handle_uuid",
                },
            )
            exact(edge, {"create_time", "update_time"}, str)
            exact(edge, {"meta_data"}, dict)
            normalize_json_object(edge["meta_data"])
            optional(edge, {"description"}, str)

        for template in graph["node_templates"]:
            uuids(template, {"uuid", "resource_template_uuid"})
            exact(
                template,
                {
                    "create_time",
                    "update_time",
                    "name",
                    "display_name",
                    "type",
                    "node_type",
                },
                str,
            )
            exact(
                template,
                {
                    "meta_data",
                    "goal",
                    "goal_default",
                    "feedback",
                    "result",
                },
                dict,
            )
            for field in (
                "meta_data",
                "goal",
                "goal_default",
                "feedback",
                "result",
            ):
                normalize_json_object(template[field])
            optional(
                template,
                {
                    "description",
                    "class",
                    "schema",
                    "icon",
                    "header",
                    "footer",
                },
                str,
            )

        for handle in graph["handle_templates"]:
            uuids(handle, {"uuid", "workflow_node_template_uuid"})
            exact(
                handle,
                {
                    "create_time",
                    "update_time",
                    "handle_key",
                    "io_type",
                    "display_name",
                    "type",
                },
                str,
            )
            exact(handle, {"meta_data"}, dict)
            normalize_json_object(handle["meta_data"])
            exact(handle, {"required"}, bool)
            optional(
                handle,
                {"description", "data_source", "data_key"},
                str,
            )

    @staticmethod
    def _require_candidate_graph_containers(graph: Dict[str, Any]) -> None:
        workflow = graph.get("workflow")
        if workflow is not None and not isinstance(workflow, dict):
            raise WorkflowError("candidate_invalid")
        for field in (
            "nodes",
            "edges",
            "node_templates",
            "handle_templates",
        ):
            entities = graph.get(field)
            if entities is None:
                continue
            if not isinstance(entities, list) or any(
                not isinstance(item, dict) for item in entities
            ):
                raise WorkflowError("candidate_invalid")

    @staticmethod
    def _hydrate_backend_catalog_entities(
        entities: List[Dict[str, Any]],
        *,
        persisted: Dict[str, Dict[str, Any]],
        timestamp: str,
        uuid_fields: set[str],
        allowed_fields: set[str],
        required_fields: set[str],
    ) -> List[Dict[str, Any]]:
        hydrated = []
        for item in entities:
            value = {
                key: child
                for key, child in item.items()
                if key in allowed_fields and child is not None
            }
            for field in uuid_fields:
                try:
                    value[field] = validate_uuid(value[field])
                except (KeyError, ValueError):
                    raise WorkflowError("candidate_invalid") from None
            previous = persisted.get(value["uuid"], {})
            hydrated.append(
                {
                    "uuid": value["uuid"],
                    "create_time": previous.get("create_time", timestamp),
                    "update_time": previous.get("update_time", timestamp),
                    "meta_data": value.get("meta_data", {}),
                    **value,
                }
            )
        WorkflowService._require_backend_read_fields(
            hydrated,
            required_fields,
        )
        return hydrated

    @staticmethod
    def _require_backend_read_fields(
        entities: List[Dict[str, Any]],
        required_fields: set[str],
        *,
        error_code: str = "candidate_invalid",
    ) -> None:
        if any(
            not isinstance(item, dict) or not required_fields.issubset(item)
            for item in entities
        ):
            raise WorkflowError(error_code)

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
