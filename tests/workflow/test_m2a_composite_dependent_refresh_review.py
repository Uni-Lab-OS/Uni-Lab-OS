"""M2A 组合工作流依赖刷新 reviewer findings 的 tests-only RED。"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from unilabos.package_manager.consumers import (
    PackageCatalogPublishedWorkflowResolver,
)
from unilabos.registry.catalog_consumer import (
    workflow_template_imports_from_registry_snapshot,
)
from unilabos.workflow import service as workflow_service_module
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.catalog import TemplateCatalog
from unilabos.workflow.composite import (
    CompositeAuthoring,
    PublishedWorkflowCatalogPublisher,
)
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore

from .test_c1_catalog_publication_lifecycle import (
    AUTHORITY,
    PARENT_SOURCE,
    PARENT_WORKFLOW_UUID,
    SOURCE,
    WORKFLOW_TEMPLATE_NAME,
    _catalog_identities,
    _package_catalog,
    _prepare_child_apply,
    _register_child_source,
    _registry_snapshot,
    _StaticResourceTemplateIdentityIndex,
)
from .test_c1_published_workflow_contract import (
    HOST_RESOURCE_TEMPLATE_UUID,
    WORKFLOW_UUID,
)

CHILD_V2_SOURCE = f'''from unilabos.workflow.authoring import workflow_definition


@workflow_definition(
    workflow_uuid="{WORKFLOW_UUID}",
    displayname="Lifecycle child",
)
def prepare_sample(*, required_value: float):
    pass
'''


def _service_without_source_monitor(working_dir: Path) -> WorkflowService:
    """装配不启动源码监视线程的真实工作流服务。

    参数：
        working_dir: 隔离 SQLite 工作流权威与测试源码的工作目录。

    返回：
        使用真实目录发布器、组合编译器和 ``WorkflowService`` 的服务实例。

    异常：
        目录投影或持久存储初始化失败时透传对应异常。

    不变量：
        不创建后台源码监视线程，外部源码编辑只由测试控制的刷新路径观察。
    """

    store = WorkflowStore(working_dir / "workflow.db")
    identity_index = _StaticResourceTemplateIdentityIndex(include_host=True)
    resolver = PackageCatalogPublishedWorkflowResolver((_package_catalog(),))
    catalog = TemplateCatalog(store)
    publisher = PublishedWorkflowCatalogPublisher(
        catalog=catalog,
        authority=AUTHORITY,
        store=store,
        sources=resolver.sources,
        base_templates=workflow_template_imports_from_registry_snapshot(
            _registry_snapshot(include_host=True),
            authority_id=AUTHORITY.authority_id,
            resource_template_identity_resolver=identity_index.resolve_symbol,
        ),
        host_node_resource_template_uuid=HOST_RESOURCE_TEMPLATE_UUID,
    )
    publisher.publish()
    service = WorkflowService(
        store,
        compiler=WorkflowAuthoringEngine(
            catalog=catalog,
            authority=AUTHORITY,
            resource_template_identity_index=identity_index,
            composite_authoring=CompositeAuthoring(
                store=store,
                catalog=catalog,
                authority=AUTHORITY,
                resolver=resolver,
            ),
        ),
        catalog_publisher=publisher,
    )
    service.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="Lifecycle child",
        tags=[],
        description=None,
        meta_data={},
    )
    return service


def _register_parent_source(service: WorkflowService, working_dir: Path) -> Path:
    """创建父工作流并注册其唯一可编辑源码。

    参数：
        service: 持有工作流权威的公共应用服务。
        working_dir: 包含 ``c1_published_lab`` 测试包的工作目录。

    返回：
        父工作流源码的规范文件路径。

    异常：
        父工作流或源码注册冲突时透传服务异常。
    """

    service.create_workflow(
        workflow_uuid=PARENT_WORKFLOW_UUID,
        name="Lifecycle parent",
        tags=[],
        description=None,
        meta_data={},
    )
    package_root = working_dir / "c1_published_lab"
    service.register_editable_source(
        workflow_uuid=PARENT_WORKFLOW_UUID,
        package_id="c1_published_lab",
        package_root=package_root,
        relative_path="workflows/parent.py",
    )
    return package_root / "workflows" / "parent.py"


def _save_candidate(
    service: WorkflowService,
    workflow_uuid: str,
    python_source: str,
) -> dict[str, Any]:
    """保存源码并确保服务签发的规范候选已物化为当前 Draft。

    参数：
        service: 执行双 CAS Draft 保存的工作流应用服务。
        workflow_uuid: 待编译工作流（Workflow）的稳定 UUID。
        python_source: 完整待保存 Python 工作流源码。

    返回：
        Draft 字节与候选规范源码完全一致的工作流创作聚合。

    异常：
        Draft 无法产生有效候选或 CAS 失败时抛出断言或服务异常。

    不变量：
        不应用候选；仅通过公共 Draft 保存接口物化服务给出的规范源码。
    """

    current = service.get_authoring(workflow_uuid)
    current_draft = current["draft"]
    workflow_revision = current["workflow_revision"]
    aggregate = service.save_draft(
        workflow_uuid,
        python_source=python_source,
        expected_draft_hash=(
            current_draft["draft_hash"] if current_draft is not None else None
        ),
        expected_workflow_revision=workflow_revision,
    )
    candidate = aggregate["candidate"]
    assert candidate is not None, aggregate["draft"]["diagnostics"]
    if aggregate["draft"]["python_source"] != candidate["normalized_python_source"]:
        aggregate = service.save_draft(
            workflow_uuid,
            python_source=candidate["normalized_python_source"],
            expected_draft_hash=aggregate["draft"]["draft_hash"],
            expected_workflow_revision=workflow_revision,
        )
        candidate = aggregate["candidate"]
        assert candidate is not None, aggregate["draft"]["diagnostics"]
    assert aggregate["draft"]["python_source"] == candidate["normalized_python_source"]
    return aggregate


def _apply_source(
    service: WorkflowService,
    workflow_uuid: str,
    python_source: str,
) -> dict[str, Any]:
    """通过公共 Draft 与 Apply 接口应用一份有效工作流源码。

    参数：
        service: 负责工作流创作事务的公共应用服务。
        workflow_uuid: 待应用工作流（Workflow）的稳定 UUID。
        python_source: 完整工作流源码。

    返回：
        ``WorkflowService.apply_authoring`` 返回的提交结果。

    异常：
        编译、CAS、目录发布或 Apply 失败时透传对应异常。
    """

    aggregate = _save_candidate(service, workflow_uuid, python_source)
    return service.apply_authoring(
        workflow_uuid,
        candidate_hash=aggregate["candidate"]["candidate_hash"],
    )


def test_external_edit_before_parent_refresh_keeps_external_event_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """证明父锁取得前的外部编辑必须产生 ``external_draft_changed`` 事件。

    参数：
        tmp_path: 隔离工作流数据库与可编辑包源码的临时目录。
        monkeypatch: 在公共注册源码枚举 seam 安装确定性同步点的 pytest fixture。

    返回：
        无；断言父工作流刷新事件保留真实外部编辑原因。

    不变量：
        子工作流 Apply 已提交并发布目录后才修改父源码；测试不依赖时间休眠，
        且 Catalog 刷新不得把文件变化强制标记为 ``draft_compiled``。
    """

    working_dir = tmp_path / "authority"
    service = _service_without_source_monitor(working_dir)
    try:
        _register_child_source(service, working_dir)
        parent_path = _register_parent_source(service, working_dir)
        parent_workflow = service.get_workflow(PARENT_WORKFLOW_UUID)
        parent_before_apply = service.save_draft(
            PARENT_WORKFLOW_UUID,
            python_source=PARENT_SOURCE,
            expected_draft_hash=None,
            expected_workflow_revision=parent_workflow["revision"],
        )
        assert parent_before_apply["candidate"] is None
        child_candidate_hash = _prepare_child_apply(service, working_dir)
        event_cursor = service.list_events(after_id=0)["items"][-1]["id"]

        publication_reached = Event()
        release_enumeration = Event()
        original_list_registered_sources = service.list_registered_sources

        def list_registered_sources_after_publication() -> list[dict[str, Any]]:
            """在目录发布完成、父刷新取锁前暴露确定性外部编辑窗口。

            返回：
                释放同步点后返回公共服务原始注册源码快照。

            异常：
                TimeoutError: 测试主线程未在期限内完成外部源码编辑。
            """

            registrations = original_list_registered_sources()
            publication_reached.set()
            if not release_enumeration.wait(timeout=3):
                raise TimeoutError("父工作流刷新同步点未释放")
            return registrations

        monkeypatch.setattr(
            service,
            "list_registered_sources",
            list_registered_sources_after_publication,
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                service.apply_authoring,
                WORKFLOW_UUID,
                candidate_hash=child_candidate_hash,
            )
            assert publication_reached.wait(timeout=1)
            parent_path.write_text(
                PARENT_SOURCE + "\n# 外部编辑发生在父工作流刷新取锁前。\n",
                encoding="utf-8",
            )
            release_enumeration.set()
            future.result(timeout=3)

        parent_events = [
            event
            for event in service.list_events(after_id=event_cursor)["items"]
            if event["data"]["workflow_uuid"] == PARENT_WORKFLOW_UUID
        ]
        assert [event["data"]["cause"] for event in parent_events] == [
            "external_draft_changed"
        ]
    finally:
        service.close()


def test_breaking_child_contract_recompiles_already_applied_parent(
    tmp_path: Path,
) -> None:
    """证明子工作流破坏性合同变化会使未改源码的已应用父工作流失效。

    参数：
        tmp_path: 隔离工作流数据库与可编辑包源码的临时目录。

    返回：
        无；断言父 Draft 自动重编译为边界映射无效且不产生候选。

    不变量：
        父工作流源码和修订均保持不变；子 v2 新增必填输入后，父 v1 调用不能
        继续以旧 Applied Source 证明组合边界有效。
    """

    working_dir = tmp_path / "authority"
    service = _service_without_source_monitor(working_dir)
    try:
        _register_child_source(service, working_dir)
        _register_parent_source(service, working_dir)
        _apply_source(service, WORKFLOW_UUID, SOURCE)
        _apply_source(service, PARENT_WORKFLOW_UUID, PARENT_SOURCE)
        parent_applied = service.get_authoring(PARENT_WORKFLOW_UUID)
        parent_source_hash = parent_applied["draft"]["draft_hash"]
        parent_revision = parent_applied["workflow_revision"]

        _apply_source(service, WORKFLOW_UUID, CHILD_V2_SOURCE)

        parent_after_child_v2 = service.get_authoring(PARENT_WORKFLOW_UUID)
        assert parent_after_child_v2["draft"]["draft_hash"] == parent_source_hash
        assert parent_after_child_v2["workflow_revision"] == parent_revision
        assert parent_after_child_v2["candidate"] is None
        assert [
            item["code"] for item in parent_after_child_v2["draft"]["diagnostics"]
        ] == ["composite_boundary_mapping_invalid"]
    finally:
        service.close()


def test_refresh_enumeration_failure_does_not_turn_committed_apply_into_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """证明 post-commit 注册源码枚举失败不改变已提交 Apply 的成功结果。

    参数：
        tmp_path: 隔离工作流数据库与可编辑包源码的临时目录。
        monkeypatch: 在公共注册源码枚举 seam 注入异常的 pytest fixture。

    返回：
        无；断言 Apply 返回成功、修订推进、目录已发布且异常留有日志。

    不变量：
        SQLite Apply 与完整目录发布均早于刷新枚举；派生刷新属于提交后
        best-effort 工作，不能把已提交事实伪装成失败。
    """

    working_dir = tmp_path / "authority"
    service = _service_without_source_monitor(working_dir)
    try:
        candidate_hash = _prepare_child_apply(service, working_dir)
        previous_revision = service.get_workflow(WORKFLOW_UUID)["revision"]
        logged_refresh_failures: list[tuple[object, BaseException | None]] = []

        def fail_registered_source_enumeration() -> list[dict[str, Any]]:
            """注入 post-commit 注册源码枚举故障。

            返回：
                永不返回。

            异常：
                RuntimeError: 每次调用都抛出，模拟派生刷新基础设施故障。
            """

            raise RuntimeError("injected registered source enumeration failure")

        def record_refresh_exception(
            message: object, *args: object, **kwargs: object
        ) -> None:
            """记录刷新日志调用及调用点仍活跃的异常上下文。

            参数：
                message: ``Logger.exception`` 接收的格式字符串。
                args: 日志格式化位置参数。
                kwargs: 日志调用关键字参数。

            返回：
                无；把消息与 ``sys.exc_info`` 当前异常写入测试观测列表。

            不变量：
                spy 必须在生产 ``except`` 块内被调用，因而异常上下文不得为空。
            """

            del args, kwargs
            logged_refresh_failures.append((message, sys.exc_info()[1]))

        monkeypatch.setattr(
            service,
            "list_registered_sources",
            fail_registered_source_enumeration,
        )
        monkeypatch.setattr(
            workflow_service_module._LOGGER,
            "exception",
            record_refresh_exception,
        )
        applied = service.apply_authoring(
            WORKFLOW_UUID,
            candidate_hash=candidate_hash,
        )

        assert applied["apply_result"]["workflow_revision"] == previous_revision + 1
        assert service.get_workflow(WORKFLOW_UUID)["revision"] == previous_revision + 1
        assert WORKFLOW_TEMPLATE_NAME in _catalog_identities(service)
        assert len(logged_refresh_failures) == 1
        logged_message, logged_exception = logged_refresh_failures[0]
        assert "刷新" in str(logged_message)
        assert isinstance(logged_exception, RuntimeError)
        assert str(logged_exception) == "injected registered source enumeration failure"
    finally:
        service.close()
