"""C1 R1 Catalog publication lifecycle 的公开 composition RED。"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from unilabos.workflow import composition
from unilabos.workflow.catalog import (
    CatalogAuthority,
    TemplateCatalog,
    TemplateCatalogUnavailable,
)
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore

from .test_c1_published_workflow_contract import (
    HOST_RESOURCE_TEMPLATE_UUID,
    WORKFLOW_UUID,
    _package_catalog,
)

AUTHORITY = CatalogAuthority(authority_id="os-c1-lifecycle", kind="local")
DEVICE_SOURCE_IDENTITY = "community.c1_lifecycle.measurement_device"
DEVICE_RESOURCE_TEMPLATE_UUID = "62000000-0000-4000-8000-000000000001"
WORKFLOW_TEMPLATE_NAME = f"workflow:{WORKFLOW_UUID}"
# 父工作流（Workflow）与组合调用节点（WorkflowNode）的稳定测试身份。
PARENT_WORKFLOW_UUID = "51000000-0000-4000-8000-000000000002"
PARENT_INVOCATION_NODE_UUID = "55000000-0000-4000-8000-000000000002"
SOURCE = f'''from unilabos.workflow.authoring import workflow_definition


@workflow_definition(
    workflow_uuid="{WORKFLOW_UUID}",
    displayname="Lifecycle child",
)
def prepare_sample():
    pass
'''
PARENT_SOURCE = f'''from c1_published_lab.workflows.child import prepare_sample
from unilabos.workflow.authoring import workflow_definition


@workflow_definition(
    workflow_uuid="{PARENT_WORKFLOW_UUID}",
    displayname="Lifecycle parent",
)
def prepare_parent():
    # unilab:node_uuid={PARENT_INVOCATION_NODE_UUID}
    prepared = prepare_sample()
'''


class _StaticResourceTemplateIdentityIndex:
    """Registry authority 在 composition 边界注入的完成态只读 identity view。"""

    def __init__(self, *, include_host: bool) -> None:
        self._by_source = {
            DEVICE_SOURCE_IDENTITY: DEVICE_RESOURCE_TEMPLATE_UUID,
        }
        if include_host:
            self._by_source["host_node"] = HOST_RESOURCE_TEMPLATE_UUID
        self._by_uuid = {value: key for key, value in self._by_source.items()}

    def resolve_symbol(self, source_identity: str) -> str:
        return self._by_source[source_identity]

    def identify_uuid(self, resource_template_uuid: str) -> str:
        return self._by_uuid[resource_template_uuid]


@pytest.fixture(autouse=True)
def _clean_composition() -> Iterator[None]:
    composition.reset_workflow_service_for_test()
    try:
        yield
    finally:
        composition.reset_workflow_service_for_test()


def _action_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "goal": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"value": {"type": "number"}},
                "required": ["value"],
            },
            "result": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"result": {"type": "number"}},
                "required": ["result"],
            },
        },
        "required": ["goal", "result"],
        "x-unilabos-action-contract": {
            "version": 1,
            "input_order": ["value"],
            "output_order": ["result"],
            "resource_template_symbols": {"goal": {}, "result": {}},
        },
    }


def _registry_snapshot(*, include_host: bool) -> dict[str, Any]:
    action_schema = _action_schema()
    snapshot: dict[str, Any] = {
        "measurement_device": {
            "source_fqid": DEVICE_SOURCE_IDENTITY,
            "display_name": "Measurement device",
            "class": {
                "module": "c1_lifecycle.device:MeasurementDevice",
                "action_value_mappings": {
                    "measure": {
                        "displayname": "Measure",
                        "description": "Measure one value",
                        "schema": action_schema,
                        "goal": {"value": "value"},
                        "goal_default": {},
                        "feedback": {},
                        "result": {"result": "result"},
                        "type": "UniLabJsonCommand",
                        "node_type": "device",
                    }
                },
            },
        }
    }
    if include_host:
        snapshot["host_node"] = {
            "source_fqid": "host_node",
            "display_name": "Host Node",
            "class": {
                "module": "unilabos.ros.nodes.presets.host_node:HostNode",
                "action_value_mappings": {},
            },
        }
    return snapshot


def _compose(
    working_dir: Path,
    *,
    include_host: bool,
) -> WorkflowService:
    return composition.compose_workflow_runtime(
        working_dir,
        authority=AUTHORITY,
        registry_snapshot=_registry_snapshot(include_host=include_host),
        resource_template_identity_resolver=(
            _StaticResourceTemplateIdentityIndex(include_host=include_host)
        ),
        workflow_package_catalogs=(_package_catalog(),),
    )


def _catalog(service: WorkflowService) -> TemplateCatalog:
    compiler = service.compiler
    assert compiler is not None
    catalog = compiler.template_catalog
    assert isinstance(catalog, TemplateCatalog)
    return catalog


def _catalog_identities(service: WorkflowService) -> dict[str, str]:
    with _catalog(service).snapshot(AUTHORITY) as snapshot:
        return {
            str(item["name"]): str(item["uuid"]) for item in snapshot.node_templates
        }


def _register_child_source(service: WorkflowService, working_dir: Path) -> None:
    package_root = working_dir / "c1_published_lab"
    (package_root / "workflows").mkdir(parents=True, exist_ok=True)
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="c1_published_lab",
        package_root=package_root,
        relative_path="workflows/child.py",
    )


def _prepare_child_apply(service: WorkflowService, working_dir: Path) -> str:
    _register_child_source(service, working_dir)
    workflow = service.get_workflow(WORKFLOW_UUID)
    aggregate = service.save_draft(
        WORKFLOW_UUID,
        python_source=SOURCE,
        expected_draft_hash=None,
        expected_workflow_revision=workflow["revision"],
    )
    candidate = aggregate["candidate"]
    assert candidate is not None, aggregate["diagnostics"]
    if aggregate["draft"]["python_source"] != candidate["normalized_python_source"]:
        aggregate = service.save_draft(
            WORKFLOW_UUID,
            python_source=candidate["normalized_python_source"],
            expected_draft_hash=aggregate["draft"]["draft_hash"],
            expected_workflow_revision=workflow["revision"],
        )
        candidate = aggregate["candidate"]
        assert candidate is not None, aggregate["diagnostics"]
    assert aggregate["draft"]["python_source"] == candidate["normalized_python_source"]
    return str(candidate["candidate_hash"])


def _apply_child(service: WorkflowService, working_dir: Path) -> dict[str, Any]:
    return service.apply_authoring(
        WORKFLOW_UUID,
        candidate_hash=_prepare_child_apply(service, working_dir),
    )


@contextmanager
def _catalog_insert_failure(database_path: Path) -> Iterator[None]:
    """用真实 SQLite trigger 注入 workflow template INSERT 失败。"""

    fault_store = WorkflowStore(database_path)
    try:
        with fault_store.transaction() as connection:
            connection.execute(
                f"""
                CREATE TRIGGER c1_fail_workflow_template_insert
                BEFORE INSERT ON workflow_node_template
                WHEN NEW.name = '{WORKFLOW_TEMPLATE_NAME}'
                BEGIN
                    SELECT RAISE(ABORT, 'injected C1 catalog replace failure');
                END
                """
            )
        yield
    finally:
        with fault_store.transaction() as connection:
            connection.execute(
                "DROP TRIGGER IF EXISTS c1_fail_workflow_template_insert"
            )
        fault_store.close()


def test_startup_without_eligible_child_does_not_require_host_node(
    tmp_path: Path,
) -> None:
    working_dir = tmp_path / "authority"

    service = _compose(working_dir, include_host=False)

    identities = _catalog_identities(service)
    assert "measure" in identities
    assert WORKFLOW_TEMPLATE_NAME not in identities


def test_restart_rebuilds_one_complete_registry_framework_and_workflow_catalog(
    tmp_path: Path,
) -> None:
    working_dir = tmp_path / "authority"
    service = _compose(working_dir, include_host=True)
    _apply_child(service, working_dir)
    before_restart = _catalog_identities(service)

    composition.reset_workflow_service_for_test()
    restarted = _compose(working_dir, include_host=True)
    after_restart = _catalog_identities(restarted)

    assert {"measure", "material_source", "group", WORKFLOW_TEMPLATE_NAME} <= set(
        after_restart
    )
    assert after_restart == before_restart


def test_child_apply_waits_for_catalog_guard_then_publishes_new_graph_and_contract(
    tmp_path: Path,
) -> None:
    working_dir = tmp_path / "authority"
    service = _compose(working_dir, include_host=True)
    candidate_hash = _prepare_child_apply(service, working_dir)
    catalog = _catalog(service)
    previous_revision = service.get_workflow(WORKFLOW_UUID)["revision"]
    started = Event()

    def apply() -> dict[str, Any]:
        started.set()
        return service.apply_authoring(
            WORKFLOW_UUID,
            candidate_hash=candidate_hash,
        )

    with ThreadPoolExecutor(max_workers=1) as executor:
        with catalog.snapshot(AUTHORITY):
            future = executor.submit(apply)
            assert started.wait(timeout=1)
            with pytest.raises(FutureTimeout):
                future.result(timeout=0.1)
            assert service.get_workflow(WORKFLOW_UUID)["revision"] == previous_revision
            assert WORKFLOW_TEMPLATE_NAME not in _catalog_identities(service)
        applied = future.result(timeout=3)

    assert applied["apply_result"]["workflow_revision"] == previous_revision + 1
    assert service.get_workflow(WORKFLOW_UUID)["revision"] == previous_revision + 1
    assert WORKFLOW_TEMPLATE_NAME in _catalog_identities(service)


def test_child_apply_recompiles_registered_parent_without_parent_draft_save(
    tmp_path: Path,
) -> None:
    """证明子工作流 Apply 后自动刷新依赖它的父工作流候选且无需重存父 Draft。

    参数：
        tmp_path: 隔离工作流权威数据库与可编辑包源码的临时目录。

    返回：
        无；断言发布后的父工作流候选与诊断已由同一源码重新编译。

    不变量：
        父工作流源码字节不变；子工作流 Apply 发布目录后不得保留旧的
        ``composite_child_unapplied`` 派生诊断。
    """

    working_dir = tmp_path / "authority"
    service = _compose(working_dir, include_host=True)
    _register_child_source(service, working_dir)
    service.create_workflow(
        workflow_uuid=PARENT_WORKFLOW_UUID,
        name="Lifecycle parent",
        tags=[],
        description=None,
        meta_data={},
    )
    # 父工作流源码（Workflow Source）与子源码共享同一个获授权可编辑包边界。
    package_root = working_dir / "c1_published_lab"
    service.register_editable_source(
        workflow_uuid=PARENT_WORKFLOW_UUID,
        package_id="c1_published_lab",
        package_root=package_root,
        relative_path="workflows/parent.py",
    )
    parent_workflow = service.get_workflow(PARENT_WORKFLOW_UUID)
    parent_before_apply = service.save_draft(
        PARENT_WORKFLOW_UUID,
        python_source=PARENT_SOURCE,
        expected_draft_hash=None,
        expected_workflow_revision=parent_workflow["revision"],
    )

    assert parent_before_apply["candidate"] is None
    assert [item["code"] for item in parent_before_apply["draft"]["diagnostics"]] == [
        "composite_child_unapplied"
    ]

    service.apply_authoring(
        WORKFLOW_UUID,
        candidate_hash=_prepare_child_apply(service, working_dir),
    )

    parent_after_apply = service.get_authoring(PARENT_WORKFLOW_UUID)
    assert parent_after_apply["draft"]["diagnostics"] == []
    assert parent_after_apply["candidate"] is not None


def test_restart_recompiles_unchanged_parent_after_child_was_applied(
    tmp_path: Path,
) -> None:
    """证明重启恢复会重新编译引用已应用子工作流的未变父 Draft。

    参数：
        tmp_path: 隔离持久工作流权威与可编辑包源码的临时目录。

    返回：
        无；断言重新 compose 后父工作流候选有效且陈旧诊断已清除。

    不变量：
        恢复不能只凭父源码 hash 未变而跳过编译；已发布子工作流目录是影响
        父候选有效性的外部编译事实。
    """

    working_dir = tmp_path / "authority"
    service = _compose(working_dir, include_host=True)
    _register_child_source(service, working_dir)
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
    parent_workflow = service.get_workflow(PARENT_WORKFLOW_UUID)
    parent_before_apply = service.save_draft(
        PARENT_WORKFLOW_UUID,
        python_source=PARENT_SOURCE,
        expected_draft_hash=None,
        expected_workflow_revision=parent_workflow["revision"],
    )
    stale_draft = parent_before_apply["draft"]
    stale_diagnostics = stale_draft["diagnostics"]
    assert [item["code"] for item in stale_diagnostics] == ["composite_child_unapplied"]

    service.apply_authoring(
        WORKFLOW_UUID,
        candidate_hash=_prepare_child_apply(service, working_dir),
    )
    composition.reset_workflow_service_for_test()

    # 模拟升级前或崩溃窗口遗留的派生编译记录；已应用图和权威源码均不回退。
    stale_store = WorkflowStore(working_dir / "workflow.db")
    try:
        stale_store.record_draft_compilation(
            workflow_uuid=PARENT_WORKFLOW_UUID,
            draft_hash=stale_draft["draft_hash"],
            draft_update_time=stale_draft["update_time"],
            diagnostics=stale_diagnostics,
            candidate_hash=None,
            candidate=None,
            event_data={
                "workflow_uuid": PARENT_WORKFLOW_UUID,
                "cause": "recovered",
                "workflow_revision": parent_workflow["revision"],
                "draft_hash": stale_draft["draft_hash"],
                "candidate_hash": None,
            },
        )
    finally:
        stale_store.close()

    restarted = _compose(working_dir, include_host=True)
    recovered_parent = restarted.get_authoring(PARENT_WORKFLOW_UUID)

    assert recovered_parent["draft"]["diagnostics"] == []
    assert recovered_parent["candidate"] is not None


def test_replace_failure_marks_catalog_unavailable_until_restart_rebuilds(
    tmp_path: Path,
) -> None:
    working_dir = tmp_path / "authority"
    service = _compose(working_dir, include_host=True)
    candidate_hash = _prepare_child_apply(service, working_dir)
    catalog = _catalog(service)
    previous_revision = service.get_workflow(WORKFLOW_UUID)["revision"]

    with _catalog_insert_failure(working_dir / "workflow.db"):
        with pytest.raises(WorkflowError) as caught:
            service.apply_authoring(
                WORKFLOW_UUID,
                candidate_hash=candidate_hash,
            )

        assert caught.value.code == "template_catalog_unavailable"
        assert service.get_workflow(WORKFLOW_UUID)["revision"] == previous_revision + 1
        with pytest.raises(TemplateCatalogUnavailable), catalog.snapshot(AUTHORITY):
            pass

    composition.reset_workflow_service_for_test()
    restarted = _compose(working_dir, include_host=True)
    assert WORKFLOW_TEMPLATE_NAME in _catalog_identities(restarted)
