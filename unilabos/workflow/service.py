"""本地 Backend-shaped Workflow Authority 的应用服务。"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import signal
import stat
import struct
import sys
import threading
from collections import defaultdict
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable
from uuid import uuid4

try:
    import fcntl
except ModuleNotFoundError:  # pragma: no cover - exercised by Windows CI
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ModuleNotFoundError:  # pragma: no cover - exercised by POSIX CI
    msvcrt = None  # type: ignore[assignment]

from pydantic import ValidationError

from unilabos.app.scheduler.inventory import (
    TaskMaterialAdmissionResult,
    TaskMaterialReleaseResult,
)
from unilabos.workflow.candidate_validation import validate_candidate_bundle
from unilabos.workflow.darwin_draft_cas import (
    DarwinDraftCasConflict,
    DarwinDraftCasInternalError,
    DarwinDraftCasInvalidTarget,
    supports_darwin_draft_cas,
    write_darwin_draft_cas,
)
from unilabos.workflow.json_codec import encode_json
from unilabos.workflow.material_source import (
    MaterialSourceAuthorityError,
    MaterialSourceStaticAuthority,
    validate_material_source_authority,
)
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
    ResourceSlotResolver,
    TaskInputError,
    UnconfiguredResourceSlotResolver,
    preflight_task_input,
)
from unilabos.workflow.windows_draft_cas import (
    WindowsDraftCasConflict,
    WindowsDraftCasInternalError,
    WindowsDraftCasInvalidTarget,
    write_windows_draft_cas,
)

_LOGGER = logging.getLogger(__name__)
AUTHORING_SOURCE_BYTE_LIMIT = 8 * 1024 * 1024
_CANDIDATE_HASH_FIELDS = (
    "base_workflow_revision",
    "draft_hash",
    "graph",
    "normalized_python_source",
    "source_map",
    "changeset",
    "compiler_version",
    "template_catalog_fingerprint",
)
_GRAPH_AUDIT_FIELDS = frozenset({"create_time", "update_time"})
_GRAPH_ENTITY_COLLECTIONS = (
    "nodes",
    "edges",
    "node_templates",
    "handle_templates",
)
_ERRORS = {
    "invalid_input": (400, "提交内容格式不正确"),
    "not_found": (404, "请求的资源不存在"),
    "conflict": (409, "资源已发生冲突，请刷新后重试"),
    "invalid_transition": (409, "当前工作流任务状态不接受该命令"),
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
    "candidate_not_materialized": (
        409,
        "请先接受并保存规范化源码，再应用工作流",
    ),
    "template_catalog_conflict": (
        409,
        "设备动作模板已更新，请重新编译并检查工作流",
    ),
    "idempotency_conflict": (409, "幂等键已用于不同的设备动作请求"),
    "device_action_mismatch": (409, "设备当前 Action 与模板合同不一致"),
    "unsupported_contract": (422, "该 Action 合同超出单动作运行范围"),
    "admission_unavailable": (503, "设备动作调度暂不可用"),
    "candidate_not_ready": (409, "当前草稿尚未生成可应用的工作流"),
    "draft_invalid": (422, "草稿存在错误，修复后才能应用"),
    "candidate_invalid": (422, "工作流校验失败，请检查节点、连线和输入输出"),
    "invalid_material_source": (400, "物料来源配置不符合合同"),
    "template_catalog_mismatch": (409, "物料来源框架模板与目录不一致"),
    "material_source_conflict": (409, "物料来源与仓库或库位事实冲突"),
    "material_flow_fan_out": (409, "同一个物料输出不能同时连接多个下游节点"),
    "material_authority_unavailable": (503, "物料权威暂不可用"),
    "reconciliation_required": (503, "物理执行事实需要先完成对账"),
    "template_catalog_unavailable": (
        503,
        "设备动作模板暂不可用，请稍后重试",
    ),
    "internal_error": (500, "本地工作流服务出现错误，请重试或查看日志"),
}
_HASH_TOKEN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_NO_EXPECTED_HASH = object()
_F_SETOWN_EX = getattr(fcntl, "F_SETOWN_EX", 15)
_F_OWNER_TID = 0
_LEASE_BREAK_SIGNAL = getattr(signal, "SIGIO", None)
_DIRECTORY_FD_PATHS_SUPPORTED = os.open in getattr(
    os, "supports_dir_fd", ()
) and os.stat in getattr(os, "supports_dir_fd", ())
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


def _supports_directory_fd_paths() -> bool:
    return _DIRECTORY_FD_PATHS_SUPPORTED


def _supports_linux_file_lease_cas() -> bool:
    """判断当前进程是否具备 Linux Draft 强 CAS 所需的全部原语。

    本函数没有参数；返回值为 ``True`` 时，调用方可以使用目录 FD、Linux 文件
    lease 和同步信号消费保护工作流源码（Workflow Source）的比较并替换窗口。
    任何平台或原语缺失都返回 ``False``，调用方必须选择其他 Adapter 或失败关闭，
    不能仅凭 ``fcntl`` 模块或目录 FD 存在就进入 Linux 实现。
    """

    if not sys.platform.startswith("linux"):
        return False
    if not _supports_directory_fd_paths() or fcntl is None:
        return False
    return all(
        hasattr(owner, name)
        for owner, name in (
            (fcntl, "F_SETSIG"),
            (fcntl, "F_SETLEASE"),
            (fcntl, "F_WRLCK"),
            (fcntl, "F_UNLCK"),
            (signal, "SIGIO"),
            (signal, "SIG_BLOCK"),
            (signal, "SIG_SETMASK"),
            (signal, "pthread_sigmask"),
            (signal, "sigtimedwait"),
        )
    )


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
_NODE_READ_FIELDS = set(WorkflowNodeWrite.model_fields) | {
    "create_time",
    "update_time",
    "workflow_uuid",
}
_EDGE_READ_FIELDS = set(WorkflowEdgeWrite.model_fields) | {
    "create_time",
    "update_time",
}
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


@runtime_checkable
class CatalogSnapshotProvider(Protocol):
    """可变 Catalog 编译器提供的可选稳定快照能力。"""

    def catalog_snapshot(self) -> AbstractContextManager[str]: ...


class CatalogPublisher(Protocol):
    """Apply 提交后、Catalog guard 释放前执行 complete replace 的 capability。"""

    def publish(self) -> object: ...

    def invalidate(self) -> None: ...

    @property
    def authority_id(self) -> str: ...


def _sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _canonical_json(value: Any) -> bytes:
    return encode_json(value, sort_keys=True)


def _candidate_semantic_hash(candidate: Dict[str, Any]) -> str:
    """对完整 Candidate 语义求哈希，排除 Backend 只读审计时间。"""

    graph = candidate["graph"]

    def semantic_entity(entity: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: value
            for key, value in entity.items()
            if key not in _GRAPH_AUDIT_FIELDS
        }

    semantic_graph = {
        "workflow": semantic_entity(graph["workflow"]),
        **{
            field: [semantic_entity(item) for item in graph[field]]
            for field in _GRAPH_ENTITY_COLLECTIONS
        },
    }
    payload = {field: candidate[field] for field in _CANDIDATE_HASH_FIELDS}
    payload["graph"] = semantic_graph
    return _sha256(_canonical_json(payload))


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
        resource_resolver: Optional[ResourceSlotResolver] = None,
        material_source_authority: MaterialSourceStaticAuthority | None = None,
        material_reservations: object | None = None,
        catalog_publisher: CatalogPublisher | None = None,
    ):
        # Compatibility-only constructor input. Task creation deliberately does
        # not invoke Inventory; EdgeScheduler owns the post-commit saga.
        del material_reservations
        self._store = store
        self.compiler = compiler
        self._resource_resolver = (
            resource_resolver
            if resource_resolver is not None
            else UnconfiguredResourceSlotResolver()
        )
        self._material_source_authority = material_source_authority
        self._catalog_publisher = catalog_publisher
        self._locks_guard = threading.Lock()
        self._authoring_locks: Dict[str, threading.RLock] = {}

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
            if self._store.is_device_action_system_workflow(identity):
                raise WorkflowError("not_found")
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
        """更新工作流展示事实并刷新受发布目录影响的工作流创作草稿。

        参数：
            workflow_uuid: 待更新的工作流（Workflow）UUID。
            name: 去除首尾空白后必须非空的工作流名称。
            tags: Backend 形态的 JSON 标签数组。
            description: 可空工作流说明。
            meta_data: 不得覆盖 ``unilab`` 保留区的展示元数据。

        返回：
            更新后的 Backend 形态工作流。

        异常：
            WorkflowError: 输入、目录发布或持久化失败。

        不变量：
            已发布工作流目录先在 Catalog guard 内完整替换；依赖 Draft 只在相关
            工作流锁和 Catalog guard 全部释放后重新编译。
        """

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
            with self._catalog_mutation() as catalog_authority_id:
                updated = self._store.update_workflow(
                    identity,
                    name=name,
                    tags=tags,
                    description=self._optional_text(description),
                    meta_data=public_meta_data,
                    catalog_authority_id=catalog_authority_id,
                )
                self._publish_catalog_after_mutation()
        self._refresh_registered_sources_after_catalog_mutation(identity)
        return updated

    def delete_workflow(self, workflow_uuid: str) -> None:
        """软删除工作流并在发布完成后重编译其余已注册工作流创作草稿。

        参数：
            workflow_uuid: 待删除的工作流（Workflow）UUID。

        返回：
            无。

        异常：
            WorkflowError: 工作流不存在或目录发布失败。

        不变量：
            删除和 Catalog 失效属于同一持久事务；依赖 Draft 刷新不持有被删除
            工作流的创作锁，也不把派生刷新失败改写为删除失败。
        """

        identity = self.get_workflow(workflow_uuid)["uuid"]
        with self._authoring_lock(identity):
            self.get_workflow(identity)
            with self._catalog_mutation() as catalog_authority_id:
                self._store.delete_workflow(
                    identity,
                    catalog_authority_id=catalog_authority_id,
                )
                self._publish_catalog_after_mutation()
        self._refresh_registered_sources_after_catalog_mutation(identity)

    def get_graph(self, workflow_uuid: str) -> Dict[str, Any]:
        identity = self.get_workflow(workflow_uuid)["uuid"]
        return self._validated_applied_backend_graph(
            self._store.get_graph(identity),
        )

    def _validate_material_source(
        self,
        graph: Dict[str, Any],
    ) -> None:
        """在 Workflow 写事务外执行 MaterialSource 静态只读检查。"""

        try:
            validate_material_source_authority(
                graph,
                self._material_source_authority,
            )
        except MaterialSourceAuthorityError as error:
            raise StoreAuthoringConflict(error.code) from None

    def save_graph(
        self,
        workflow_uuid: str,
        *,
        revision: int,
        nodes: List[WorkflowNodeWrite | Dict[str, Any]],
        edges: List[WorkflowEdgeWrite | Dict[str, Any]],
    ) -> Dict[str, Any]:
        """保存完整工作流图并刷新依赖已发布合同的工作流创作草稿。

        参数：
            workflow_uuid: 图所属工作流（Workflow）UUID。
            revision: 调用方观察到的工作流修订号。
            nodes: 完整替换图中的工作流节点（WorkflowNode）。
            edges: 完整替换图中的工作流边（WorkflowEdge）。

        返回：
            保存后的 Backend 形态工作流图。

        异常：
            WorkflowConflict: 修订号冲突。
            WorkflowError: 图、物料来源（MaterialSource）或目录发布无效。

        不变量：
            图事务和目录发布完成前不重编译其他 Draft；刷新不得嵌套取得另一个
            工作流创作锁与 Catalog guard。
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
                self._validate_material_source(
                    {"nodes": [item.model_dump() for item in node_values]}
                )
                with self._catalog_mutation() as catalog_authority_id:
                    saved = self._store.save_graph(
                        identity,
                        revision=revision,
                        nodes=node_values,
                        edges=edge_values,
                        protect_reserved_metadata=True,
                        validate_workflow_io_contract=True,
                        catalog_authority_id=catalog_authority_id,
                    )
                    self._publish_catalog_after_mutation()
            except ValidationError:
                raise WorkflowError("invalid_input") from None
            except MaterialSourceAuthorityError as error:
                raise WorkflowError(error.code) from None
            except StoreAuthoringConflict as error:
                raise WorkflowError(error.code) from None
            except StoreRevisionConflict:
                raise WorkflowConflict("conflict") from None
            except StoreNotFound:
                raise WorkflowError("not_found") from None
            except StoreConflict:
                raise WorkflowError("invalid_input") from None
        self._refresh_registered_sources_after_catalog_mutation(identity)
        return saved

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
        description = self._optional_text(description)
        try:
            return self._store.create_task_with_jobs(
                workflow_uuid=workflow_uuid,
                task_uuid=str(uuid4()),
                run_mode=run_mode,
                target_node_uuid=target_node_uuid,
                description=description,
                meta_data=meta_data,
                plan_builder=lambda graph: self._prepare_task_input(
                    graph,
                    run_mode=run_mode,
                    target_node_uuid=target_node_uuid,
                    input_value=input_value,
                ),
            )
        except TaskInputError as error:
            raise WorkflowError(error.code) from None
        except StoreConflict:
            raise WorkflowError("invalid_input") from None

    def get_workflow_task(self, task_uuid: str) -> Dict[str, Any]:
        try:
            identity = validate_uuid(task_uuid)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        try:
            if self._store.is_device_action_task(identity):
                raise WorkflowError("not_found")
            return self._store.get_task(identity)
        except StoreNotFound:
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
            "admission_blocked",
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
            status=status,
            cleanup_status=cleanup_status,
        )

    def list_workflow_node_jobs(self, task_uuid: str) -> List[Dict[str, Any]]:
        identity = self.get_workflow_task(task_uuid)["uuid"]
        return self._store.list_jobs(identity)

    def project_material_admission(
        self,
        result: TaskMaterialAdmissionResult,
    ) -> bool:
        """Project one closed Inventory admission result exactly once."""

        if not isinstance(result, TaskMaterialAdmissionResult):
            raise WorkflowError("invalid_input")
        task_uuid = self.get_workflow_task(result.workflow_task_uuid)["uuid"]
        payload = {
            "schema_version": result.schema_version,
            "command_uuid": result.command_uuid,
            "workflow_task_uuid": result.workflow_task_uuid,
            "status": result.status,
            "reservation_uuid": result.reservation_uuid,
            "bindings": [
                {
                    "material_source_node_uuid": binding.material_source_node_uuid,
                    "resource_slot": dict(binding.resource_slot),
                    "site_uuid": binding.site_uuid,
                }
                for binding in result.bindings
            ],
            "diagnostics": [dict(item) for item in result.diagnostics],
            "outbox_sequence": result.outbox_sequence,
        }
        try:
            return self._store.project_task_material_admission(
                task_uuid=task_uuid,
                command_uuid=result.command_uuid,
                status=result.status,
                reservation_uuid=result.reservation_uuid,
                outbox_sequence=result.outbox_sequence,
                result=payload,
                bindings=payload["bindings"],
            )
        except StoreNotFound:
            raise WorkflowError("not_found") from None
        except StoreConflict:
            raise WorkflowError("conflict") from None

    def get_material_admission(
        self,
        task_uuid: str,
    ) -> dict[str, Any] | None:
        """Read the durable five-field Material admission projection."""

        identity = self.get_workflow_task(task_uuid)["uuid"]
        return self._store.get_task_material_admission(identity)

    def project_material_release(
        self,
        result: TaskMaterialReleaseResult,
    ) -> bool:
        """Project one closed terminal Material release exactly once."""

        if not isinstance(result, TaskMaterialReleaseResult):
            raise WorkflowError("invalid_input")
        task_uuid = self.get_workflow_task(result.workflow_task_uuid)["uuid"]
        payload = {
            "schema_version": result.schema_version,
            "command_uuid": result.command_uuid,
            "workflow_task_uuid": result.workflow_task_uuid,
            "status": result.status,
            "reservation_uuid": result.reservation_uuid,
            "outbox_sequence": result.outbox_sequence,
        }
        try:
            return self._store.project_task_material_release(
                task_uuid=task_uuid,
                command_uuid=result.command_uuid,
                status=result.status,
                reservation_uuid=result.reservation_uuid,
                outbox_sequence=result.outbox_sequence,
                result=payload,
            )
        except StoreNotFound:
            raise WorkflowError("not_found") from None
        except StoreConflict:
            raise WorkflowError("conflict") from None

    def get_material_release(
        self,
        task_uuid: str,
    ) -> dict[str, Any] | None:
        """Read the durable five-field terminal Material release projection."""

        identity = self.get_workflow_task(task_uuid)["uuid"]
        return self._store.get_task_material_release(identity)

    def create_workflow_task_command(
        self,
        task_uuid: str,
        *,
        command_type: str,
        target_node_uuid: Optional[str],
        idempotency_key: str,
        description: Optional[str],
        meta_data: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        try:
            task_identity = validate_uuid(task_uuid)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        try:
            task = self._store.get_task(task_identity)
        except StoreNotFound:
            raise WorkflowError("not_found") from None
        if task["status"] in {"succeeded", "failed", "canceled", "timeout"}:
            raise WorkflowError("invalid_transition")
        if command_type not in {"step", "pause", "resume", "cancel"}:
            raise WorkflowError("invalid_input")
        if command_type == "step" and task["run_mode"] != "step":
            raise WorkflowError("invalid_transition")
        if command_type != "step" and target_node_uuid is not None:
            raise WorkflowError("invalid_input")
        if target_node_uuid is not None:
            try:
                target_node_uuid = validate_uuid(target_node_uuid)
            except ValueError:
                raise WorkflowError("invalid_input") from None
        try:
            idempotency_key = idempotency_key.strip()
        except AttributeError:
            raise WorkflowError("invalid_input") from None
        if not idempotency_key or len(idempotency_key.encode("utf-8")) > 255:
            raise WorkflowError("invalid_input")
        try:
            normalized_meta_data = normalize_json_object(meta_data)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        command, created = self._store.create_task_command(
            command_uuid=str(uuid4()),
            task_uuid=task_identity,
            command_type=command_type,
            target_node_uuid=target_node_uuid,
            idempotency_key=idempotency_key,
            description=self._optional_text(description),
            meta_data=normalized_meta_data,
        )
        if not created and (
            command["type"] != command_type
            or command.get("target_node_uuid") != target_node_uuid
        ):
            raise WorkflowConflict("conflict")
        return command

    def get_workflow_node_job(self, job_uuid: str) -> Dict[str, Any]:
        try:
            identity = validate_uuid(job_uuid)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        try:
            if self._store.is_device_action_job(identity):
                raise WorkflowError("not_found")
            return self._store.get_job(identity)
        except StoreNotFound:
            raise WorkflowError("not_found") from None

    def list_workflow_task_runtime_events(
        self,
        task_uuid: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> Dict[str, Any]:
        try:
            identity = validate_uuid(task_uuid)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        if (
            isinstance(after_sequence, bool)
            or not isinstance(after_sequence, int)
            or after_sequence < 0
            or after_sequence > (1 << 63) - 1
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 500
        ):
            raise WorkflowError("invalid_input")
        try:
            return self._store.list_task_runtime_events(
                identity,
                after_sequence=after_sequence,
                limit=limit,
            )
        except StoreNotFound:
            raise WorkflowError("not_found") from None

    def list_workflow_node_job_feedback(
        self,
        job_uuid: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> Dict[str, Any]:
        try:
            identity = validate_uuid(job_uuid)
        except ValueError:
            raise WorkflowError("invalid_input") from None
        if (
            isinstance(after_sequence, bool)
            or not isinstance(after_sequence, int)
            or after_sequence < 0
            or after_sequence > (1 << 63) - 1
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 500
        ):
            raise WorkflowError("invalid_input")
        try:
            return self._store.list_job_feedback(
                identity,
                after_sequence=after_sequence,
                limit=limit,
            )
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
        if not ordered:
            raise StoreConflict("workflow has no enabled nodes")
        if run_mode == "single_node":
            if target_node_uuid is None:
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

    def _prepare_task_input(
        self,
        graph: Dict[str, Any],
        *,
        run_mode: str,
        target_node_uuid: Optional[str],
        input_value: Dict[str, Any],
    ) -> PreparedTaskInput:
        plan, jobs = self._build_execution_plan(
            graph,
            run_mode=run_mode,
            target_node_uuid=target_node_uuid,
        )
        return preflight_task_input(
            graph=graph,
            raw_input=input_value,
            execution_plan=plan,
            jobs=jobs,
            resource_resolver=self._resource_resolver,
        )

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
        return data_source.lower() == "dependency"

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
            "material_source",
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
                return self._store.register_source(
                    workflow_uuid=workflow_uuid,
                    package_id=package_id,
                    package_root=str(root),
                    relative_path=relative.as_posix(),
                    source_uri=source_uri,
                )
            except StoreConflict:
                raise WorkflowConflict("invalid_input") from None

    @contextmanager
    def editable_source_registration_batch(self) -> Iterator[None]:
        """让多个既有单 source Interface 共享一个 Store transaction。"""

        with self._store.source_registration_batch():
            yield

    def list_registered_sources(self) -> List[Dict[str, Any]]:
        """返回 Draft 监视与启动恢复所需的已注册源码。"""

        return self._store.list_source_registrations()

    def recover_registered_sources(self) -> None:
        """启动时恢复全部已注册工作流源码及其 Catalog 相关派生状态。

        本方法没有参数和返回值。每个注册项独立恢复；损坏路径、编译器故障或
        单个工作流创作错误不会阻止其余 Draft。源码 hash 未变只证明文件未变，
        不能证明子工作流或模板 Catalog 未变，因此有诊断、过期候选或不完整派生
        状态时仍会重新编译。
        """

        for registration in self.list_registered_sources():
            try:
                self.reconcile_registered_source(registration["workflow_uuid"])
            except (OSError, RuntimeError, WorkflowError):
                continue

    def close(self) -> None:
        """关闭由该 Service 独占的 Workflow 持久存储。"""

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
        """以双 CAS 保存并编译一个工作流创作草稿（Authoring Draft）。

        ``workflow_uuid`` 标识现有工作流（Workflow），``python_source`` 是完整待保存
        源码，``expected_draft_hash`` 是调用方开始编辑时观察到的工作流源码
        （Workflow Source）字节 hash，``expected_workflow_revision`` 是同时观察到的
        工作流修订（Workflow Revision）。成功返回自洽的工作流创作聚合；相同字节
        不替换权威文件，但仍重新编译并刷新派生候选状态。hash 或修订冲突抛出
        ``WorkflowConflict``，非法输入或基础设施故障抛出 ``WorkflowError``；失败不得
        改写可编辑包（Editable Package）中的权威源码。
        """

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
            if len(encoded) > AUTHORING_SOURCE_BYTE_LIMIT:
                raise WorkflowError("invalid_input")
            encoded_hash = _sha256(encoded)
            if encoded_hash != current_hash:
                try:
                    self._atomic_write(
                        registration,
                        encoded,
                        expected_hash=current_hash,
                    )
                except OSError:
                    raise WorkflowError("internal_error") from None
            source = self._read_source(registration)
            if source is None or source["draft_hash"] != encoded_hash:
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
        """把一个已注册工作流源码的文件变化协调为最新创作聚合。

        参数：
            workflow_uuid: 已注册源码所属工作流（Workflow）UUID。

        返回：
            自洽的工作流创作聚合。

        异常：
            WorkflowError: 工作流、注册路径或编译结果无效。

        不变量：
            源码、修订、候选指纹和 Applied Source 均可证明仍为当前状态时按 hash
            去重；诊断或过期候选不能仅凭源码 hash 未变跳过重编译。
        """

        return self._reconcile_registered_source(
            workflow_uuid,
            refresh_catalog_dependent_state=True,
        )

    def _reconcile_registered_source(
        self,
        workflow_uuid: str,
        *,
        refresh_catalog_dependent_state: bool,
    ) -> Dict[str, Any]:
        """协调一个 Draft，并可刷新由模板 Catalog 决定的派生编译状态。

        参数：
            workflow_uuid: 已注册源码所属工作流（Workflow）UUID。
            refresh_catalog_dependent_state: 为真时，即使源码 hash 未变，也检查候选、
                诊断和已应用源码是否仍足以证明当前创作状态。

        返回：
            自洽的工作流创作聚合。

        异常：
            WorkflowError: 工作流、注册路径、Catalog 或编译结果无效。

        不变量：
            读取、编译和记录更新均在同一工作流创作锁下完成；本方法不发布 Catalog。
        """

        workflow_uuid = self._get_authoring_workflow(workflow_uuid)["uuid"]
        with self._authoring_lock(workflow_uuid):
            workflow = self._get_authoring_workflow(workflow_uuid)
            registration = self._registration(workflow_uuid)
            source = self._read_source(registration)
            record = self._store.get_authoring_record(workflow_uuid)
            actual_hash = source["draft_hash"] if source is not None else None
            source_state_unchanged = actual_hash == record[
                "observed_draft_hash"
            ] and not (actual_hash is None and record.get("candidate") is not None)
            if source_state_unchanged and not (
                refresh_catalog_dependent_state
                and self._authoring_record_requires_catalog_refresh(
                    workflow=workflow,
                    source=source,
                    record=record,
                )
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
                candidate = self._issue_candidate(
                    workflow_revision=workflow["revision"],
                    draft_hash=source["draft_hash"],
                    compilation=compilation,
                    applied_graph=applied_graph,
                    draft_python_source=source["python_source"],
                )
                diagnostics = compilation.diagnostics
            cause = (
                "draft_compiled"
                if source_state_unchanged
                else (
                    "recovered"
                    if source is not None
                    and record["observed_draft_hash"] is None
                    and record["update_time"] is not None
                    else "external_draft_changed"
                )
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

    def _authoring_record_requires_catalog_refresh(
        self,
        *,
        workflow: Dict[str, Any],
        source: Dict[str, Any] | None,
        record: Dict[str, Any],
    ) -> bool:
        """判断同 hash 创作记录是否仍需依据当前模板 Catalog 重新编译。

        参数：
            workflow: 当前 Backend 形态工作流（Workflow）。
            source: 当前已注册工作流源码；文件缺失时为空。
            record: SQLite 中保存的派生工作流创作记录。

        返回：
            现有记录不能证明对应当前 Catalog、修订和源码时返回 ``True``。

        不变量：
            Applied Source、诊断和候选均属于可重建派生状态；只要其绑定的 Catalog
            指纹不再是当前值，即使源码 hash 未变也必须重新编译。
        """

        if source is None:
            return record.get("candidate") is not None

        candidate = record.get("candidate")
        if isinstance(candidate, dict):
            try:
                return (
                    candidate["template_catalog_fingerprint"]
                    != self._catalog_fingerprint()
                    or candidate["base_workflow_revision"] != workflow["revision"]
                    or candidate["draft_hash"] != source["draft_hash"]
                )
            except (KeyError, TypeError, WorkflowError):
                return True

        if record.get("diagnostics"):
            return True

        applied_source = record.get("applied_source")
        try:
            return not (
                isinstance(applied_source, dict)
                and applied_source.get("workflow_revision") == workflow["revision"]
                and applied_source.get("source_hash") == source["draft_hash"]
                and applied_source.get("template_catalog_fingerprint")
                == self._catalog_fingerprint()
            )
        except WorkflowError:
            return True

    def apply_authoring(
        self,
        workflow_uuid: str,
        *,
        candidate_hash: str,
    ) -> Dict[str, Any]:
        """原子应用一个服务端候选并刷新依赖其发布合同的工作流创作草稿。

        参数：
            workflow_uuid: 待应用候选所属工作流（Workflow）UUID。
            candidate_hash: 服务端签发且已物化到 Draft 的不透明候选 hash。

        返回：
            Apply 结果和提交后的自洽工作流创作聚合。

        异常：
            WorkflowConflict: Draft、修订、候选或 Catalog 快照发生冲突。
            WorkflowError: Draft、候选、物料来源或目录发布无效。

        不变量：
            Catalog guard 覆盖候选重校验、SQLite Apply 和完整目录发布；依赖 Draft
            只在该 guard 与当前工作流创作锁释放后刷新，且刷新失败不得把已提交
            Apply 伪装成失败。
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
            if candidate["candidate_hash"] != candidate_hash:
                raise WorkflowConflict("candidate_hash_conflict")

            source = self._read_source(registration)
            if source is None or source["draft_hash"] != candidate["draft_hash"]:
                raise WorkflowConflict("draft_hash_conflict")
            if workflow["revision"] != candidate["base_workflow_revision"]:
                raise WorkflowConflict("workflow_revision_conflict")
            current_catalog = self._catalog_fingerprint()
            if candidate["template_catalog_fingerprint"] != current_catalog:
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
                != candidate["template_catalog_fingerprint"]
            ):
                raise WorkflowConflict("template_catalog_conflict")
            try:
                candidate_changed = _candidate_semantic_hash(
                    revalidated
                ) != _candidate_semantic_hash(candidate)
            except (KeyError, TypeError, UnicodeError, ValueError):
                raise WorkflowError("candidate_invalid") from None
            if candidate_changed:
                raise WorkflowConflict("candidate_hash_conflict")

            normalized_source = candidate["normalized_python_source"]
            normalized_bytes = normalized_source.encode("utf-8")
            normalized_hash = _sha256(normalized_bytes)
            if (
                source["python_source"] != normalized_source
                or source["draft_hash"] != normalized_hash
            ):
                raise WorkflowConflict("candidate_not_materialized")

            # 编译可能阻塞；先在事务外快速拒绝已经发生的外部变化。
            latest_source = self._read_source(registration)
            if (
                latest_source is None
                or latest_source["draft_hash"] != candidate["draft_hash"]
            ):
                raise WorkflowConflict("draft_hash_conflict")
            if latest_source["python_source"] != normalized_source:
                raise WorkflowConflict("candidate_not_materialized")
            if self._catalog_fingerprint() != candidate["template_catalog_fingerprint"]:
                raise WorkflowConflict("template_catalog_conflict")

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

            def validate_draft_linearization() -> None:
                """在 SQLite 写事务内确定 Apply 与外部 Draft 编辑的先后顺序。"""

                linearized_source = self._read_source(registration)
                if (
                    linearized_source is None
                    or linearized_source["draft_hash"] != candidate["draft_hash"]
                ):
                    raise WorkflowConflict("draft_hash_conflict")
                if linearized_source["python_source"] != normalized_source:
                    raise WorkflowConflict("candidate_not_materialized")

            try:
                self._validate_material_source(candidate["graph"])
                # 可变 Catalog 的 guard 必须先于 Store 事务获取并保持到事务结束。
                with self._catalog_snapshot() as catalog_fingerprint:
                    if catalog_fingerprint != candidate["template_catalog_fingerprint"]:
                        raise WorkflowConflict("template_catalog_conflict")
                    resulting_revision = self._store.apply_authoring_candidate(
                        workflow_uuid=workflow_uuid,
                        candidate_hash=candidate_hash,
                        validate_draft_state=validate_draft_linearization,
                        catalog_authority_id=self._catalog_authority_id(),
                    )
                    self._publish_catalog_after_mutation()
            except StoreAuthoringConflict as error:
                raise WorkflowConflict(error.code) from None
            except StoreRevisionConflict:
                raise WorkflowConflict("workflow_revision_conflict") from None
            except (StoreConflict, ValidationError):
                raise WorkflowError("candidate_invalid") from None

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
                "name": candidate_workflow.get("name", workflow["name"]),
                "description": candidate_workflow.get(
                    "description",
                    workflow.get("description"),
                ),
                "update_time": utc_now(),
            }
            fallback_applied_source = {
                **applied_source,
                "workflow_revision": resulting_revision,
                "update_time": utc_now(),
            }
            fallback_record = {
                "observed_draft_hash": source["draft_hash"],
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
                        source=source,
                        record=fallback_record,
                    )

            result = {
                "apply_result": {
                    "kind": candidate["changeset"]["kind"],
                    "previous_workflow_revision": previous_revision,
                    "workflow_revision": resulting_revision,
                    "applied_candidate_hash": candidate["candidate_hash"],
                    "applied_source_hash": normalized_hash,
                    "warnings": [],
                },
                "authoring": authoring,
            }
        self._refresh_registered_sources_after_catalog_mutation(workflow_uuid)
        return result

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
            if self._store.is_device_action_system_workflow(identity):
                raise WorkflowError("not_found")
            return self._store.get_workflow(identity)
        except StoreNotFound:
            raise WorkflowError("workflow_not_found") from None

    def _registration(self, workflow_uuid: str) -> Dict[str, Any]:
        try:
            return self._store.get_source_registration(workflow_uuid)
        except StoreNotFound:
            raise WorkflowError("workflow_not_found") from None

    @staticmethod
    def _read_source_by_path(
        root: Path,
        target: Path,
    ) -> Optional[Dict[str, Any]]:
        """在不支持相对目录 FD 的平台按原始字节读取工作流源码 Draft。

        ``root`` 是已授权的可编辑包根目录，``target`` 是其中已注册的工作流源码
        （Workflow Source）路径。目标不存在时返回 ``None``；成功时返回源码、mtime
        与基于原始 UTF-8 字节的 hash。路径越界、符号链接、非普通文件
        或读取期间身份变化会抛出 ``WorkflowError``。Windows 必须显式请求二进制
        模式，确保 Authoring GET 与 Draft PUT CAS 观察完全相同的 CRLF 字节。
        """

        try:
            root_before = root.lstat()
        except (OSError, TypeError, ValueError):
            raise WorkflowError("invalid_input") from None
        if not stat.S_ISDIR(
            root_before.st_mode
        ) or WorkflowService._path_contains_symlink(root):
            raise WorkflowError("invalid_input")
        try:
            parent_before = target.parent.lstat()
            target_before = target.lstat()
        except FileNotFoundError:
            return None
        except (OSError, TypeError, ValueError):
            raise WorkflowError("invalid_input") from None
        if (
            not stat.S_ISDIR(parent_before.st_mode)
            or WorkflowService._path_contains_symlink(target.parent)
            or target.is_symlink()
            or not stat.S_ISREG(target_before.st_mode)
        ):
            raise WorkflowError("invalid_input")
        descriptor = -1
        try:
            descriptor = os.open(
                target,
                os.O_RDONLY
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                target_before.st_dev,
                target_before.st_ino,
            ):
                raise WorkflowError("invalid_input")
            raw = WorkflowService._read_regular_fd(
                descriptor,
                byte_limit=AUTHORING_SOURCE_BYTE_LIMIT,
            )
            root_after = root.lstat()
            parent_after = target.parent.lstat()
            target_after = target.lstat()
            if (
                WorkflowService._path_contains_symlink(target.parent)
                or target.is_symlink()
                or (root_after.st_dev, root_after.st_ino)
                != (root_before.st_dev, root_before.st_ino)
                or (parent_after.st_dev, parent_after.st_ino)
                != (parent_before.st_dev, parent_before.st_ino)
                or (target_after.st_dev, target_after.st_ino)
                != (opened.st_dev, opened.st_ino)
            ):
                raise WorkflowError("invalid_input")
        except WorkflowError:
            raise
        except (OSError, OverflowError, TypeError, ValueError):
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
            "update_time": _mtime_rfc3339(opened.st_mtime),
        }

    def _read_source(
        self,
        registration: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        root, target = self._source_path(registration)
        self._assert_contained_regular_target(root, target, allow_missing=True)
        if not _supports_directory_fd_paths():
            return self._read_source_by_path(root, target)
        with self._source_parent_fd(
            registration,
            create=False,
        ) as source_parent:
            if source_parent is None:
                return None
            parent_fd, filename = source_parent
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
                try:
                    raw = self._read_regular_fd(
                        descriptor,
                        byte_limit=AUTHORING_SOURCE_BYTE_LIMIT,
                    )
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
        if not _supports_directory_fd_paths():
            try:
                root_before = root.lstat()
            except (OSError, TypeError, ValueError):
                raise WorkflowError("invalid_input") from None
            if not stat.S_ISDIR(root_before.st_mode) or self._path_contains_symlink(
                root
            ):
                raise WorkflowError("invalid_input")
            try:
                parent_before = target.parent.lstat()
                stat_result = target.lstat()
            except FileNotFoundError:
                return ("missing",)
            except (OSError, TypeError, ValueError):
                raise WorkflowError("invalid_input") from None
            if (
                not stat.S_ISDIR(parent_before.st_mode)
                or self._path_contains_symlink(target.parent)
                or target.is_symlink()
                or not stat.S_ISREG(stat_result.st_mode)
            ):
                raise WorkflowError("invalid_input")
            try:
                root_after = root.lstat()
                parent_after = target.parent.lstat()
                target_after = target.lstat()
            except (OSError, TypeError, ValueError):
                raise WorkflowError("invalid_input") from None
            if (
                (root_after.st_dev, root_after.st_ino)
                != (
                    root_before.st_dev,
                    root_before.st_ino,
                )
                or (parent_after.st_dev, parent_after.st_ino)
                != (
                    parent_before.st_dev,
                    parent_before.st_ino,
                )
                or (target_after.st_dev, target_after.st_ino)
                != (
                    stat_result.st_dev,
                    stat_result.st_ino,
                )
                or self._path_contains_symlink(target.parent)
            ):
                raise WorkflowError("invalid_input")
            return (
                "file",
                stat_result.st_dev,
                stat_result.st_ino,
                stat_result.st_size,
                stat_result.st_mtime_ns,
                stat_result.st_ctime_ns,
            )
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
        """按平台把 ``content`` 以 Draft hash CAS 写入已注册工作流源码。

        `registration` 固定 editable package 根和相对路径，`content` 是有界源码，
        `expected_hash` 是调用方观察到的旧 Draft hash。成功没有返回值；函数保证
        Linux 与 Windows 只在目录链、文件身份及旧 hash 可证明稳定时发布；其他
        平台的受保护写入在选择 Adapter 前失败关闭。路径变化映射为输入错误或冲突，
        基础设施故障映射为内部错误。
        """

        if len(content) > AUTHORING_SOURCE_BYTE_LIMIT:
            raise WorkflowError("invalid_input")
        if (
            expected_hash is not _NO_EXPECTED_HASH
            and not _supports_linux_file_lease_cas()
        ):
            if msvcrt is not None:
                root, target = self._source_path(registration)
                self._assert_contained_regular_target(root, target, allow_missing=True)
                try:
                    write_windows_draft_cas(
                        root=root,
                        target=target,
                        content=content,
                        expected_hash=expected_hash,
                        byte_limit=AUTHORING_SOURCE_BYTE_LIMIT,
                        locking=msvcrt,
                    )
                except WindowsDraftCasConflict:
                    raise WorkflowConflict("draft_hash_conflict") from None
                except WindowsDraftCasInvalidTarget:
                    raise WorkflowError("invalid_input") from None
                except WindowsDraftCasInternalError:
                    raise WorkflowError("internal_error") from None
                return
            if expected_hash is not None and supports_darwin_draft_cas():
                root, target = self._source_path(registration)
                self._assert_contained_regular_target(root, target, allow_missing=False)
                with self._source_parent_fd(
                    registration,
                    create=False,
                ) as source_parent:
                    if source_parent is None:
                        raise WorkflowConflict("draft_hash_conflict")
                    parent_fd, filename = source_parent
                    try:
                        write_darwin_draft_cas(
                            parent_fd=parent_fd,
                            target_name=filename,
                            content=content,
                            expected_hash=expected_hash,
                            byte_limit=AUTHORING_SOURCE_BYTE_LIMIT,
                        )
                    except DarwinDraftCasConflict:
                        raise WorkflowConflict("draft_hash_conflict") from None
                    except DarwinDraftCasInvalidTarget:
                        raise WorkflowError("invalid_input") from None
                    except DarwinDraftCasInternalError:
                        raise WorkflowError("internal_error") from None
                return
            if expected_hash is not None:
                raise WorkflowConflict("draft_hash_conflict")
            # POSIX 目录 FD 可以用 exclusive link 原子证明 Draft 不存在；这条
            # missing-target CAS 不依赖 Linux file lease，并在 Darwin 保持可用。
        if not _supports_directory_fd_paths() or fcntl is None:
            raise WorkflowConflict("draft_hash_conflict")
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
        """在 Linux 可安全中断 lease 下执行 fsync 后的原子 CAS replace。

        ``parent_fd`` 固定已验证源码父目录，``target_name`` 是权威工作流源码文件名，
        ``temporary_name`` 是同目录且已 fsync 的候选文件，``expected_hash`` 是调用方
        观察到的旧源码 hash。成功没有返回值；目标身份、内容或 lease 证明变化时抛出
        ``WorkflowConflict``，并保留任何无法安全回滚的恢复 artifact。
        """

        target_descriptor = -1
        temporary_descriptor = -1
        backup_name = f".{target_name}.{uuid4().hex}.cas"
        backup_created = False
        replacement_attempted = False
        lease_held = False
        previous_signal_mask: Optional[set[signal.Signals]] = None
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
            if not _supports_linux_file_lease_cas():
                # 非 Linux 或缺少完整 lease/signal 原语时，不能进入部分初始化后再
                # 清理；否则 Darwin 会用第二个 AttributeError 覆盖受控冲突。
                raise WorkflowConflict("draft_hash_conflict")
            try:
                previous_signal_mask = signal.pthread_sigmask(
                    signal.SIG_BLOCK,
                    {_LEASE_BREAK_SIGNAL},
                )
                fcntl.fcntl(
                    target_descriptor,
                    _F_SETOWN_EX,
                    struct.pack(
                        "ii",
                        _F_OWNER_TID,
                        threading.get_native_id(),
                    ),
                )
                fcntl.fcntl(
                    target_descriptor,
                    fcntl.F_SETSIG,
                    _LEASE_BREAK_SIGNAL,
                )
                fcntl.fcntl(
                    target_descriptor,
                    fcntl.F_SETLEASE,
                    fcntl.F_WRLCK,
                )
                lease_held = True
            except (AttributeError, OSError, ValueError):
                # 无法证明没有预打开的读写句柄时必须失败关闭。
                raise WorkflowConflict("draft_hash_conflict") from None

            original = WorkflowService._read_regular_fd(
                target_descriptor,
                byte_limit=AUTHORING_SOURCE_BYTE_LIMIT,
            )
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
            replacement_hash = WorkflowService._hash_regular_fd(
                temporary_descriptor,
                byte_limit=AUTHORING_SOURCE_BYTE_LIMIT,
            )

            os.link(
                target_name,
                backup_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
            backup_created = True
            os.fsync(parent_fd)
            if WorkflowService._drain_lease_break_signal():
                raise WorkflowConflict("draft_hash_conflict")

            replacement_attempted = True
            os.replace(
                temporary_name,
                target_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            os.fsync(parent_fd)
            if WorkflowService._drain_lease_break_signal():
                raise WorkflowConflict("draft_hash_conflict")
            if (
                not WorkflowService._target_matches_fd(
                    parent_fd,
                    target_name,
                    temporary_descriptor,
                )
                or WorkflowService._hash_regular_fd(
                    temporary_descriptor,
                    byte_limit=AUTHORING_SOURCE_BYTE_LIMIT,
                )
                != replacement_hash
            ):
                raise WorkflowConflict("draft_hash_conflict")

            fcntl.fcntl(
                target_descriptor,
                fcntl.F_SETLEASE,
                fcntl.F_UNLCK,
            )
            lease_held = False
            if WorkflowService._drain_lease_break_signal():
                raise WorkflowConflict("draft_hash_conflict")
            if (
                not WorkflowService._target_matches_fd(
                    parent_fd,
                    target_name,
                    temporary_descriptor,
                )
                or WorkflowService._hash_regular_fd(
                    temporary_descriptor,
                    byte_limit=AUTHORING_SOURCE_BYTE_LIMIT,
                )
                != replacement_hash
            ):
                raise WorkflowConflict("draft_hash_conflict")

            with suppress(OSError):
                os.unlink(backup_name, dir_fd=parent_fd)
                backup_created = False
                os.fsync(parent_fd)
        except Exception as error:
            # os.replace() 一旦被调用，异常路径便无法证明 canonical
            # 仍是本进程发布的 inode；外部 authority 可能已经原地写入
            # 或再次原子替换。此时绝不能用历史 `.cas` 覆盖或删除它。
            # 保留 fsync 过的原稿 artifact，只允许显式人工/Git 恢复。
            if backup_created and not replacement_attempted:
                with suppress(OSError):
                    os.unlink(backup_name, dir_fd=parent_fd)
                    backup_created = False
                    os.fsync(parent_fd)
            if isinstance(error, WorkflowError) and error.code == "invalid_input":
                raise WorkflowConflict("draft_hash_conflict") from None
            raise
        finally:
            if lease_held and target_descriptor >= 0:
                with suppress(OSError):
                    fcntl.fcntl(
                        target_descriptor,
                        fcntl.F_SETLEASE,
                        fcntl.F_UNLCK,
                    )
            if previous_signal_mask is not None:
                with suppress(OSError, ValueError):
                    WorkflowService._drain_lease_break_signal()
                signal.pthread_sigmask(
                    signal.SIG_SETMASK,
                    previous_signal_mask,
                )
            if temporary_descriptor >= 0:
                os.close(temporary_descriptor)
            if target_descriptor >= 0:
                os.close(target_descriptor)

    @staticmethod
    def _drain_lease_break_signal() -> bool:
        """同步消费发给当前线程的 lease 通知。"""

        observed = False
        while True:
            try:
                notification = signal.sigtimedwait(
                    {_LEASE_BREAK_SIGNAL},
                    0,
                )
            except InterruptedError:
                continue
            if notification is None:
                return observed
            observed = True

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
    def _read_regular_fd(
        descriptor: int,
        *,
        byte_limit: int,
    ) -> bytes:
        stat_result = os.fstat(descriptor)
        if not stat.S_ISREG(stat_result.st_mode) or (
            byte_limit < 0 or stat_result.st_size > byte_limit
        ):
            raise WorkflowError("invalid_input")
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks = bytearray()
        while True:
            remaining = byte_limit + 1 - len(chunks)
            if remaining <= 0:
                raise WorkflowError("invalid_input")
            read_size = min(1024 * 1024, remaining)
            chunk = os.read(descriptor, read_size)
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks) > byte_limit:
                raise WorkflowError("invalid_input")
        return bytes(chunks)

    @staticmethod
    def _write_regular_fd(
        descriptor: int,
        content: bytes,
        *,
        byte_limit: int,
    ) -> None:
        if byte_limit < 0 or len(content) > byte_limit:
            raise WorkflowError("invalid_input")
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
    def _restore_regular_fd(
        descriptor: int,
        content: bytes,
        *,
        byte_limit: int,
    ) -> None:
        WorkflowService._write_regular_fd(
            descriptor,
            content,
            byte_limit=byte_limit,
        )

    @staticmethod
    def _hash_regular_fd(descriptor: int, *, byte_limit: int) -> str:
        return _sha256(
            WorkflowService._read_regular_fd(
                descriptor,
                byte_limit=byte_limit,
            )
        )

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
        return self._validate_catalog_fingerprint(value)

    def _catalog_authority_id(self) -> str | None:
        if self._catalog_publisher is None:
            return None
        authority_id = self._catalog_publisher.authority_id
        if not isinstance(authority_id, str) or not authority_id:
            raise WorkflowError("template_catalog_unavailable")
        return authority_id

    @contextmanager
    def _catalog_mutation(self) -> Iterator[str | None]:
        """让 eligibility mutation 与 complete publication 共享 Catalog guard。"""

        if self._catalog_publisher is None:
            yield None
            return
        with self._catalog_snapshot():
            yield self._catalog_authority_id()

    def _refresh_registered_sources_after_catalog_mutation(
        self,
        mutated_workflow_uuid: str,
    ) -> None:
        """在 Catalog 发布锁释放后刷新其他已注册工作流的派生编译状态。

        参数：
            mutated_workflow_uuid: 刚完成持久变更与目录发布的工作流（Workflow）UUID；
                该工作流自己的 Apply 记录不在本轮重新编译。

        返回：
            无。

        不变量：
            调用方不得持有 Catalog guard 或被修改工作流的创作锁。每个依赖 Draft
            独立刷新；注册源枚举或单个派生刷新失败只记录诊断日志，不改变已经提交
            的工作流事实或 Catalog 可用性。
        """

        if self._catalog_publisher is None:
            return
        try:
            registrations = self.list_registered_sources()
        except Exception:  # noqa: BLE001 - 已提交 mutation 的派生刷新必须隔离
            _LOGGER.exception(
                "Catalog 发布后刷新工作流创作草稿时枚举失败: mutated_workflow_uuid=%s",
                mutated_workflow_uuid,
            )
            return
        for registration in registrations:
            workflow_uuid = "<unknown>"
            try:
                workflow_uuid = registration["workflow_uuid"]
                if workflow_uuid == mutated_workflow_uuid:
                    continue
                self._reconcile_registered_source(
                    workflow_uuid,
                    refresh_catalog_dependent_state=True,
                )
            except Exception:  # noqa: BLE001 - 已提交 mutation 的派生刷新必须隔离
                _LOGGER.exception(
                    "Catalog 发布后刷新工作流创作草稿失败: workflow_uuid=%s",
                    workflow_uuid,
                )

    def _publish_catalog_after_mutation(self) -> None:
        if self._catalog_publisher is None:
            return
        try:
            self._catalog_publisher.publish()
        except Exception:  # noqa: BLE001 - adapter boundary
            _LOGGER.exception("Catalog publication 失败")
            try:
                self._catalog_publisher.invalidate()
            except Exception:  # noqa: BLE001 - marker 已在 Store transaction 内删除
                _LOGGER.exception(
                    "Catalog publication 失败后的冗余 unavailable cleanup 失败"
                )
            raise WorkflowError("template_catalog_unavailable") from None

    @staticmethod
    def _validate_catalog_fingerprint(value: Any) -> str:
        if not isinstance(value, str) or _HASH_TOKEN.fullmatch(value) is None:
            raise WorkflowError("template_catalog_unavailable")
        return value

    @contextmanager
    def _catalog_snapshot(self) -> Iterator[str]:
        """取得 Catalog→Store 顺序所需的稳定内部快照。"""

        if self.compiler is None:
            raise WorkflowError("template_catalog_unavailable")
        if not isinstance(self.compiler, CatalogSnapshotProvider):
            # 兼容既有不可变、无状态编译器 Adapter。
            yield self._catalog_fingerprint()
            return

        try:
            snapshot = self.compiler.catalog_snapshot()
            value = snapshot.__enter__()
        except Exception:
            raise WorkflowError("template_catalog_unavailable") from None

        try:
            fingerprint = self._validate_catalog_fingerprint(value)
        except BaseException as error:
            self._release_catalog_snapshot(snapshot, error)
            raise

        try:
            yield fingerprint
        except BaseException as error:
            self._release_catalog_snapshot(snapshot, error)
            raise
        else:
            self._release_catalog_snapshot(snapshot, None)

    @staticmethod
    def _release_catalog_snapshot(
        snapshot: AbstractContextManager[str],
        error: BaseException | None,
    ) -> None:
        """释放 snapshot，但不遮蔽业务异常或已经提交的成功结果。"""

        error_type = type(error) if error is not None else None
        traceback = error.__traceback__ if error is not None else None
        try:
            # cleanup-only guard 不得通过返回 True 吞掉 Apply 异常。
            snapshot.__exit__(error_type, error, traceback)
        except Exception:
            _LOGGER.exception("Catalog snapshot guard 释放失败")

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
            graph = validate_candidate_bundle(
                graph=graph,
                base_graph=applied_graph,
                workflow_uuid=applied_graph["workflow"]["uuid"],
                revision=workflow_revision,
                source_map=source_map,
                changeset=changeset,
                require_unchanged_graph=False,
            )
            compiler_version = compilation.compiler_version
            if not compiler_version.strip():
                raise ValueError
            template_catalog_fingerprint = compilation.template_catalog_fingerprint
            if _HASH_TOKEN.fullmatch(template_catalog_fingerprint) is None:
                raise ValueError
        except (
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
            candidate_hash = _candidate_semantic_hash(bundle)
        except (KeyError, TypeError, UnicodeError, ValueError):
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
            source_ranges = []
            for item in compilation.diagnostics:
                if item.get("source_range") is not None:
                    source_ranges.append(item["source_range"])
                source_ranges.extend(item.get("occurrence_ranges") or [])
                for alternative in item.get("repair_alternatives") or []:
                    source_ranges.append(alternative["retained_range"])
                    source_ranges.extend(
                        replacement["source_range"]
                        for replacement in alternative["replacements"]
                    )
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
        raw_workflow = graph["workflow"]
        if not isinstance(raw_workflow, dict) or not set(raw_workflow).issubset(
            _WORKFLOW_READ_FIELDS
        ):
            raise WorkflowError("candidate_invalid")
        for field, allowed_fields in (
            ("nodes", _NODE_READ_FIELDS),
            ("edges", _EDGE_READ_FIELDS),
            ("node_templates", _NODE_TEMPLATE_READ_FIELDS),
            ("handle_templates", _HANDLE_TEMPLATE_READ_FIELDS),
        ):
            if any(
                not set(entity).issubset(allowed_fields)
                for entity in (graph.get(field) or [])
            ):
                raise WorkflowError("candidate_invalid")
        applied_workflow = applied["workflow"]
        if "uuid" in raw_workflow and raw_workflow["uuid"] != applied_workflow["uuid"]:
            raise WorkflowError("candidate_invalid")
        if (
            "revision" in raw_workflow
            and raw_workflow["revision"] != applied_workflow["revision"]
        ):
            raise WorkflowError("candidate_invalid")
        projected = cls._backend_graph_projection(graph)
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
                    "icon",
                    "header",
                    "footer",
                },
                str,
            )
            if "schema" in template:
                schema = template["schema"]
                if type(schema) not in {str, dict}:
                    raise ValueError
                if type(schema) is dict:
                    normalize_json_object(schema)

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
