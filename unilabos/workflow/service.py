"""本地后端形态工作流权威（Backend-shaped Workflow Authority）的应用服务。"""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Callable
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple
from uuid import uuid4

from pydantic import ValidationError

from unilabos.workflow.authoring_candidate_hash import (
    AuthoringCandidateHashError,
    compute_authoring_candidate_hash,
)
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
from unilabos.workflow.execution_plan import ExecutionPlanBuilder
from unilabos.workflow.graph_validation import GraphValidationError
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
from unilabos.workflow.task_input import (
    PreparedTaskInput,
    TaskInputError,
    prepare_task_input,
)
from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridgeError

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
    "invalid_material_source": (400, "物料来源选择器不符合规范"),
    "material_flow_fan_out": (409, "同一个物料输出不能连接多个物理消费者"),
    "material_template_mismatch": (409, "物料资源模板与消费者约束不兼容"),
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


class WorkflowTaskSchedulerBridge(Protocol):
    """普通任务与设备单动作共享的工作流任务（WorkflowTask）调度端口。"""

    def submit(self, task: dict[str, Any]) -> dict[str, Any]:
        """提交已持久任务。

        参数：``task`` 是标准工作流任务（WorkflowTask）投影。返回：同步推进后的
        任务/作业聚合。异常：编译、准入或派发前投影失败时由实现抛稳定桥接错误。
        """

        ...

    def close(self) -> None:
        """幂等释放调度生命周期监听器；参数无，返回无。"""

        ...


def _sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


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
        compiler_rebuilder: Callable[[], AuthoringCompiler] | None = None,
        material_resolver: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
        task_scheduler_bridge: WorkflowTaskSchedulerBridge | None = None,
    ) -> None:
        """装配本地工作流应用服务。

        参数：``store`` 是唯一工作流写模型；``compiler`` 负责编译可信工作流源码；
        ``compiler_rebuilder`` 在成功应用后重建包含已发布工作流的完整目录代际；
        ``material_resolver`` 按物料 UUID 读取活动物料身份，供设备单动作运行
        （DeviceActionRun）关闭式校验；``task_scheduler_bridge`` 把普通工作流任务
        （WorkflowTask）与首次创建的设备单动作聚合交给同一本地调度器。返回无。
        异常：编译器重建器不可调用时抛出 ``TypeError``。
        """

        self._store = store
        self.compiler = compiler
        if compiler_rebuilder is not None and not callable(compiler_rebuilder):
            raise TypeError("compiler_rebuilder 必须是可调用对象")
        self._compiler_rebuilder = compiler_rebuilder
        self._device_action_runs = DeviceActionRunService(
            store,
            material_resolver=material_resolver,
        )
        # ``_task_scheduler_bridge`` 是普通任务与设备单动作共享的唯一监听器所有者；
        # 后端控制（Backend-controlled）配置保持空，避免第二个调度权威。
        self._task_scheduler_bridge = task_scheduler_bridge
        self._locks_guard = threading.Lock()
        self._authoring_locks: Dict[str, threading.RLock] = {}
        # ``_source_authorization_replacement_lock`` 串行化完整授权集合替换，使“当前
        # 集合 ∪ 新集合”的锁快照在取得所有创作锁前不会被另一替换命令改变。
        self._source_authorization_replacement_lock = threading.RLock()
        # ``_active_source_workflow_uuids`` 只表达本次进程启动配置授权的工作流
        # 源码（Workflow Source）；SQLite 注册行仅保留跨启动历史身份。
        self._active_sources_lock = threading.RLock()
        self._active_source_workflow_uuids: frozenset[str] = frozenset()

    # 工作流（Workflow）与图（Graph） -------------------------------------

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

        参数说明：``workflow_uuid`` 是工作流（Workflow）稳定身份，``revision``
        是乐观并发预期版本，``nodes`` 与 ``edges`` 是完整替换集合。返回：提交后
        的后端（Backend）形状工作流图投影。异常：输入 DTO 或图语义非法抛出
        ``WorkflowError``；修订冲突抛出 ``WorkflowConflict``；任何失败都由存储
        适配器（Store Adapter）回滚，公共服务入口不会留下部分节点或修订写入。
        旧形状只在存储适配器内兼容，公共入口始终使用同一个严格校验深模块。
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
            except StoreAuthoringConflict as error:
                raise WorkflowError(error.code) from None
            except StoreConflict:
                raise WorkflowError("invalid_input") from None

    # 工作流任务（WorkflowTask）与工作流节点作业（WorkflowNodeJob） --------

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
        """从已应用工作流图创建一次工作流任务（WorkflowTask）及其作业。

        参数：``workflow_uuid`` 是工作流定义身份；``run_mode`` 是普通、单步或
        单节点运行模式；``target_node_uuid`` 是单节点运行目标；``input_value``
        是任务输入；``description`` 与 ``meta_data`` 是用户说明和公开元数据。
        返回：同一事务创建的工作流任务及工作流节点作业（WorkflowNodeJob）投影。
        异常：身份、运行模式、输入或执行计划不合法时抛出稳定工作流错误；输入
        合同解析、默认值填充与计划绑定全部在同一创建事务的首次写入前完成。
        """

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
        description = self._optional_text(description)
        try:
            task = self._store.create_task_with_jobs(
                workflow_uuid=workflow_uuid,
                task_uuid=str(uuid4()),
                run_mode=run_mode,
                target_node_uuid=target_node_uuid,
                description=description,
                meta_data=meta_data,
                plan_builder=lambda graph: self._prepare_task_input(
                    graph,
                    input_value=input_value,
                    run_mode=run_mode,
                    target_node_uuid=target_node_uuid,
                ),
            )
            if self._task_scheduler_bridge is None:
                return task
            # ``aggregate`` 来自调度同步推进后的标准持久投影，不返回创建事务中的
            # 过期 ``pending`` 快照。
            aggregate = self._task_scheduler_bridge.submit(task)
            return aggregate["task"]
        except TaskSchedulerBridgeError:
            raise WorkflowError("internal_error") from None
        except TaskInputError:
            raise WorkflowError("invalid_input") from None
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
        公共任务调度桥失败映射为 ``internal_error``，且不创建第二套执行身份。
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
            if aggregate["created"] is True and self._task_scheduler_bridge is not None:
                scheduled = self._task_scheduler_bridge.submit(aggregate["task"])
                # ``scheduled_jobs`` 是公共桥返回的同一任务作业集合；设备单动作
                # 必须仍精确包含创建事务生成的唯一作业身份。
                scheduled_jobs = [
                    job
                    for job in scheduled["jobs"]
                    if job.get("uuid") == aggregate["job"]["uuid"]
                ]
                if len(scheduled_jobs) != 1:
                    raise TaskSchedulerBridgeError(
                        "设备单动作调度结果缺少唯一原始作业身份"
                    )
                # 公共桥可能同步推进首次派发，必须返回同一任务/作业身份的刷新状态，
                # 不能把创建事务中的 ``pending`` 快照误报给前端。
                aggregate = {
                    "task": scheduled["task"],
                    "job": scheduled_jobs[0],
                    "created": True,
                }
            return aggregate
        except DeviceActionRunInputError:
            raise WorkflowError("invalid_input") from None
        except DeviceActionRunUnavailable:
            raise WorkflowError("template_catalog_unavailable") from None
        except DeviceActionRunConflict:
            raise WorkflowConflict("conflict") from None
        except TaskSchedulerBridgeError:
            raise WorkflowError("internal_error") from None

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
        """按后端（Backend）查询合同分页读取工作流任务（WorkflowTask）。

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
        """委托深模块构造执行计划（ExecutionPlan）与首次作业集合。

        参数：``graph`` 是冻结应用图，``run_mode`` 是运行模式，
        ``target_node_uuid`` 是单节点目标。返回：唯一版本化计划和待持久化作业。
        异常：图、物料来源（MaterialSource）或目标非法时由构建器失败关闭。
        """

        return ExecutionPlanBuilder().build(
            graph,
            run_mode=run_mode,
            target_node_uuid=target_node_uuid,
        )

    def _prepare_task_input(
        self,
        graph: Dict[str, Any],
        *,
        input_value: Dict[str, Any],
        run_mode: str,
        target_node_uuid: Optional[str],
    ) -> PreparedTaskInput:
        """从同一应用图构造计划并冻结工作流任务（WorkflowTask）输入。

        参数：``graph`` 是创建事务读取的应用图；``input_value`` 是规范 JSON
        请求对象；``run_mode`` 和 ``target_node_uuid`` 决定活动计划范围。返回：
        已解析输入、快照、执行计划（ExecutionPlan）和首次作业。异常：计划或
        输入绑定不合法时保留构建器/``TaskInputError`` 以映射稳定业务错误。
        """

        plan, jobs = self._build_execution_plan(
            graph,
            run_mode=run_mode,
            target_node_uuid=target_node_uuid,
        )
        return prepare_task_input(
            graph=graph,
            raw_input=input_value,
            execution_plan=plan,
            jobs=jobs,
        )

    # 工作流创作（Authoring） ---------------------------------------------

    def replace_discovered_source_authorizations(
        self,
        plan: EditableSourceDiscoveryPlan,
    ) -> List[Dict[str, Any]]:
        """原子持久化发现计划并替换当前活动源码授权集合。

        参数：``plan`` 是从全部显式授权目录完成预校验后生成的不可变计划。
        返回：按计划顺序排列的持久来源记录；成功后活动授权恰好等于本计划。
        异常：软删除工作流、来源身份或目录安全冲突映射为稳定
        ``invalid_input``，且不提交任何部分定义、来源或创作事实。
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
                != (f"package://{registration.package_id}/{registration.relative_path}")
                for registration in plan.registrations
            )
        ):
            raise WorkflowError("invalid_input")
        # ``incoming_workflow_uuids`` 是计划将保留授权的完整新集合。
        incoming_workflow_uuids = frozenset(
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
        with self._source_authorization_replacement_lock:
            with self._active_sources_lock:
                current_workflow_uuids = self._active_source_workflow_uuids
            # ``locked_workflow_uuids`` 同时覆盖将撤销和将激活的身份；稳定排序避免
            # 多工作流保存、读取与授权替换形成锁顺序反转。
            locked_workflow_uuids = sorted(
                current_workflow_uuids | incoming_workflow_uuids
            )
            with ExitStack() as locks:
                for workflow_uuid in locked_workflow_uuids:
                    locks.enter_context(self._authoring_lock(workflow_uuid))
                try:
                    with pin_package_roots(plan.root_identities) as pinned_roots:
                        registered = self._store.install_discovered_sources(
                            registration_rows,
                            before_commit=pinned_roots.assert_current,
                        )
                except SourceWorkspaceError:
                    raise WorkflowError("invalid_input") from None
                except StoreConflict:
                    raise WorkflowConflict("invalid_input") from None
                # SQLite 注册事务与进程级文件访问授权不能共用一个物理事务，但必须
                # 在所有相关创作锁释放前一次发布，撤权返回后不得再有旧操作读写路径。
                with self._active_sources_lock:
                    self._active_source_workflow_uuids = incoming_workflow_uuids
            return registered

    def replace_active_editable_source_authorization(
        self,
        *,
        workflow_uuid: str,
        package_id: str,
        package_root: str | Path,
        relative_path: str,
    ) -> Dict[str, Any]:
        """用一项可编辑来源替换当前进程的完整活动源码授权集合。

        参数：工作流（Workflow）UUID 是已有定义身份；包身份、包目录和相对路径
        共同形成工作流源码（Workflow Source）的稳定来源身份。
        返回：持久化后的来源记录；此前活动的其他来源失去本进程文件访问授权，
        但其持久历史不会被删除。
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
        source_uri = f"package://{normalized_package_id}/{normalized_relative_path}"
        # 单项替换命令构造成与启动发现完全相同的不可变计划，避免绕过物理路径、
        # 来源 URI 和“既有身份不可重绑定”等批量授权不变量。
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
            root_identities=(((root, (root_metadata.st_dev, root_metadata.st_ino))),),
        )
        return self.replace_discovered_source_authorizations(plan)[0]

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
        """关闭共享本地调度桥和由服务独占的工作流存储。

        参数：无。返回：无；桥必须幂等注销监听器，随后关闭持久存储。异常：清理
        失败原样传播，调用方据此保留未完成资源所有权并可重试。
        """

        if self._task_scheduler_bridge is not None:
            self._task_scheduler_bridge.close()
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
            current_signature = self.source_signature(workflow_uuid)
            if current_signature != observed_signature:
                return False
            self.reconcile_registered_source(workflow_uuid)
            # ``latest_signature`` 证明整个状态推进期间规范源码没有再次变化。
            latest_signature = self.source_signature(workflow_uuid)
            if latest_signature != observed_signature:
                return False
            record = self._store.get_authoring_record(workflow_uuid)
            return record["writeback_status"] != "pending"

    def apply_authoring(
        self,
        workflow_uuid: str,
        *,
        candidate_hash: str,
    ) -> Dict[str, Any]:
        """按服务端候选哈希线性化应用可信工作流创作结果。

        参数：``workflow_uuid`` 是工作流（Workflow）稳定身份；``candidate_hash``
        是服务端持久并签发的候选哈希（Candidate Hash），客户端不得重述草稿、
        工作流修订或候选包。返回：应用结果与最新创作聚合。异常：候选、源码
        权威（Source Authority）、工作流修订（Workflow Revision）或目录指纹已
        变化时抛出稳定 ``WorkflowConflict``；候选无效时抛出 ``WorkflowError``。
        """

        self._validate_hash(candidate_hash, nullable=False)
        workflow_uuid = self._get_authoring_workflow(workflow_uuid)["uuid"]
        with self._authoring_lock(workflow_uuid):
            workflow = self._get_authoring_workflow(workflow_uuid)
            registration = self._registration(workflow_uuid)
            record = self._store.get_authoring_record(workflow_uuid)
            candidate = record.get("candidate")
            if candidate is None:
                if any(
                    str(item.get("severity", "")).lower() == "error"
                    for item in record["diagnostics"]
                ):
                    raise WorkflowError("draft_invalid")
                raise WorkflowConflict("candidate_not_ready")
            if candidate.get("candidate_hash") != candidate_hash:
                raise WorkflowConflict("candidate_hash_conflict")
            try:
                # 这些前置事实只从持久候选推导，客户端无法混搭不同世代。
                expected_draft_hash = candidate["draft_hash"]
                expected_workflow_revision = candidate["base_workflow_revision"]
                expected_catalog_fingerprint = candidate["template_catalog_fingerprint"]
            except (KeyError, TypeError):
                raise WorkflowError("candidate_invalid") from None
            self._validate_hash(expected_draft_hash, nullable=False)
            if (
                type(expected_workflow_revision) is not int
                or expected_workflow_revision < 1
            ):
                raise WorkflowError("candidate_invalid")
            self._validate_hash(expected_catalog_fingerprint, nullable=False)

            source = self._read_source(registration)
            if source is None:
                raise WorkflowConflict("draft_hash_conflict")
            # D-079 的源码、修订、目录冲突顺序继续保持稳定。
            actual_hash = source["draft_hash"]
            if actual_hash != expected_draft_hash:
                raise WorkflowConflict("draft_hash_conflict")
            if workflow["revision"] != expected_workflow_revision:
                raise WorkflowConflict("workflow_revision_conflict")
            if self._catalog_fingerprint() != expected_catalog_fingerprint:
                raise WorkflowConflict("template_catalog_conflict")

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
                != expected_catalog_fingerprint
            ):
                raise WorkflowConflict("template_catalog_conflict")
            if revalidated["candidate_hash"] != candidate_hash:
                raise WorkflowConflict("candidate_hash_conflict")

            def validate_authoring_authorities(
                linearized_draft_hash: str,
                linearized_catalog_fingerprint: str,
            ) -> None:
                """在写事务内复核源码与目录两项创作权威。

                参数：``linearized_draft_hash`` 是存储从持久候选推导的草稿哈希。
                ``linearized_catalog_fingerprint`` 是同一候选的目录指纹
                （Catalog Fingerprint）。返回：无；源码或目录世代变化时抛出稳定
                冲突，使同一 SQLite 事务回滚。该回调不接受客户端事实。
                """

                latest_source = self._read_source(registration)
                if (
                    latest_source is None
                    or latest_source["draft_hash"] != linearized_draft_hash
                ):
                    raise WorkflowConflict("draft_hash_conflict")
                if self._catalog_fingerprint() != linearized_catalog_fingerprint:
                    raise WorkflowConflict("template_catalog_conflict")

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
            # 在进入唯一写事务前最后复核目录权威（Catalog Authority）。
            if self._catalog_fingerprint() != expected_catalog_fingerprint:
                raise WorkflowConflict("template_catalog_conflict")
            previous_revision = expected_workflow_revision
            try:
                (
                    resulting_revision,
                    writeback_generation,
                ) = self._store.apply_authoring_candidate(
                    workflow_uuid=workflow_uuid,
                    candidate_hash=candidate_hash,
                    authoring_authority_validator=validate_authoring_authorities,
                )
            except StoreAuthoringConflict as error:
                raise WorkflowConflict(error.code) from None
            except StoreRevisionConflict:
                raise WorkflowConflict("workflow_revision_conflict") from None
            except (StoreConflict, ValidationError):
                raise WorkflowError("candidate_invalid") from None

            warnings: List[Dict[str, str]] = []
            if self._compiler_rebuilder is not None:
                try:
                    rebuilt_compiler = self._compiler_rebuilder()
                except Exception:  # noqa: BLE001 - 主事务已提交，只能关闭目录
                    # 应用图已经提交，目录刷新失败时撤销编译入口，禁止继续用陈旧
                    # 指纹签发父候选；下次进程启动会从持久图重建完整代际。
                    self.compiler = None
                    warnings.append(
                        {
                            "code": "template_catalog_rebuild_pending",
                            "message": (
                                "工作流已应用，但模板目录重建失败；"
                                "创作编译已关闭，重启后将自动恢复。"
                            ),
                        }
                    )
                else:
                    self.compiler = rebuilt_compiler
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

    # 工作流创作（Authoring）内部实现 -------------------------------------

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
        workflow_uuid: str,
    ) -> Tuple[Any, ...]:
        """按当前授权的工作流身份返回轻量源码签名。

        参数：``workflow_uuid`` 是工作流源码（Workflow Source）绑定的稳定身份；
        本方法不接受调用者缓存的注册路径。
        返回：缺失标记或普通文件的身份、大小和时间签名。
        异常：已撤权身份稳定映射为 ``workflow_not_found``；不安全路径或非普通
        文件映射为 ``invalid_input``。安全：每次读取都在工作流创作锁内重新取得
        当前注册，撤权返回后旧注册信息不能继续触碰文件系统。
        """

        with self._authoring_lock(workflow_uuid):
            registration = self._registration(workflow_uuid)
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
        """校验编译结果并签发一个可信候选版本（Candidate）。

        参数：``workflow_revision`` 与 ``draft_hash`` 固定候选基线；``compilation``
        是编译结果；``applied_graph`` 是当前应用图；``draft_python_source`` 用于
        诊断和源码映射校验。返回：包含规范八字段、候选哈希（Candidate Hash）
        和更新时间的候选字典；编译结果不能证明时返回 ``None``。异常：目录不可
        用等非候选错误原样传播，其他候选结构或编码错误转为稳定诊断。
        """

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
            # ``candidate_hash`` 是共享八字段规则对本次签发正文的唯一稳定摘要。
            candidate_hash = compute_authoring_candidate_hash(bundle)
        except AuthoringCandidateHashError:
            self._set_candidate_invalid_diagnostic(compilation)
            return None
        return {
            "candidate_hash": candidate_hash,
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
        """按后端（Backend）JSON omitempty 语义投影候选版本（Candidate）。

        参数：``graph`` 是编译器产出的完整候选图。返回：删除可选 ``None`` 字段、
        保留后端读取容器形状的新字典；输入图不被修改。
        """

        def omit_none(value: Any) -> Any:
            """删除单个实体中的 ``None`` 字段；非字典值保持原样返回。"""

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
        """把编译器写实体补全为冻结的后端（Backend）读取形状。

        参数：``graph`` 是编译器写模型，``applied_graph`` 是当前已应用图。返回：
        补齐稳定身份、时间与读取字段的候选图；非法图抛出稳定工作流错误。
        """

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
        """检查候选版本（Candidate）前先校验权威（Authority）持有的工作流图。"""

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
        """在完整工作流图上强制执行冻结的后端（Backend）JSON 类型。

        参数：``graph`` 是待验证的完整工作流（Workflow）图。返回：无；任一字段
        类型偏离冻结合同即抛出 ``ValueError``。
        """

        def exact(entity: Dict[str, Any], fields: set[str], expected: type) -> None:
            """要求 ``entity`` 指定字段严格等于 ``expected`` 类型。"""

            if any(type(entity[field]) is not expected for field in fields):
                raise ValueError

        def optional(
            entity: Dict[str, Any],
            fields: set[str],
            expected: type,
        ) -> None:
            """要求存在的可选字段严格等于 ``expected`` 类型。"""

            if any(
                field in entity and type(entity[field]) is not expected
                for field in fields
            ):
                raise ValueError

        def uuids(entity: Dict[str, Any], fields: set[str]) -> None:
            """要求指定字段均为合法 UUID 字符串；非法值抛出 ``ValueError``。"""

            exact(entity, fields, str)
            for field in fields:
                validate_uuid(entity[field])

        def optional_uuids(entity: Dict[str, Any], fields: set[str]) -> None:
            """验证存在的可选 UUID 字段；缺失字段保持合法。"""

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
