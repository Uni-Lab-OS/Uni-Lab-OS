"""工作流源码（Workflow Source）的当前配置授权生命周期合同。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow import composition
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_A_UUID = "11111111-1111-4111-8111-111111111111"
WORKFLOW_B_UUID = "22222222-2222-4222-8222-222222222222"
HASH_TOKEN = f"sha256:{'a' * 64}"


@pytest.fixture(autouse=True)
def clean_composition() -> Any:
    """隔离每个用例使用的进程级工作流组合根。

    参数：无。返回：pytest 生命周期控制值；前后均重置服务、监视器和授权身份。
    """

    composition.reset_workflow_service_for_test()
    try:
        yield
    finally:
        composition.reset_workflow_service_for_test()


def _seed_workflows(working_dir: Path) -> None:
    """创建两个不依赖源码授权读取的已应用工作流定义。

    参数：``working_dir`` 决定持久工作流数据库。返回：无；数据库在返回前关闭。
    """

    store = WorkflowStore(working_dir / "workflow_history.db")
    service = WorkflowService(store)
    try:
        for workflow_uuid in (WORKFLOW_A_UUID, WORKFLOW_B_UUID):
            service.create_workflow(
                workflow_uuid=workflow_uuid,
                name=f"workflow-{workflow_uuid[:8]}",
                tags=[],
                description=None,
                meta_data={},
            )
    finally:
        service.close()


def _write_package(
    selected_root: Path,
    *,
    package_id: str,
    workflow_uuid: str,
) -> Path:
    """创建含一个规范源码声明的显式授权包。

    参数：``selected_root`` 是启动 allowlist 目录；``package_id`` 是稳定包身份；
    ``workflow_uuid`` 是被绑定的工作流身份。返回：规范 Python 源码路径。
    """

    source_path = selected_root / package_id / "workflows" / "demo.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(f"value = '{package_id}'\n", encoding="utf-8")
    selected_root.joinpath("package.yaml").write_text(
        "package:\n"
        f"  name: {package_id}\n"
        "workflows:\n"
        f"  - workflow_uuid: {workflow_uuid}\n"
        f"    source: {package_id}/workflows/demo.py\n",
        encoding="utf-8",
    )
    return source_path


def _assert_source_access_rejected(service: WorkflowService, workflow_uuid: str) -> None:
    """证明未授权来源的全部创作文件入口返回同一稳定错误。

    参数：``service`` 是本轮本地工作流权威；``workflow_uuid`` 是持久存在但未在
    当前 allowlist 的工作流身份。返回：无；任何文件访问成功或错误不一致均失败。
    """

    operations: tuple[Callable[[], Any], ...] = (
        lambda: service.get_authoring(workflow_uuid),
        lambda: service.reconcile_registered_source(workflow_uuid),
        lambda: service.save_draft(
            workflow_uuid,
            python_source="must_not_write = True\n",
            expected_draft_hash=None,
            expected_workflow_revision=1,
        ),
        lambda: service.apply_authoring(
            workflow_uuid,
            expected_draft_hash=HASH_TOKEN,
            expected_workflow_revision=1,
            expected_candidate_hash=HASH_TOKEN,
        ),
    )
    for operation in operations:
        with pytest.raises(WorkflowError) as caught:
            operation()
        assert caught.value.code == "workflow_not_found"


def test_restart_uses_only_current_roots_and_exact_reauthorization_recovers(
    tmp_path: Path,
) -> None:
    """A+B 注册历史重启为仅 A 时必须停用 B，完全相同身份重授权后恢复。

    参数：``tmp_path`` 隔离持久数据库和两个授权包。返回：无；证明历史注册不是
    当前授权，停用不删除已应用工作流定义，重授权仍复用原身份。
    """

    working_dir = tmp_path / "runtime"
    root_a = tmp_path / "editable-a"
    root_b = tmp_path / "editable-b"
    _seed_workflows(working_dir)
    _write_package(
        root_a,
        package_id="alpha_lab",
        workflow_uuid=WORKFLOW_A_UUID,
    )
    source_b = _write_package(
        root_b,
        package_id="beta_lab",
        workflow_uuid=WORKFLOW_B_UUID,
    )

    first = composition.compose_workflow_runtime(
        working_dir,
        editable_package_roots=(root_a, root_b),
    )
    assert {row["workflow_uuid"] for row in first.list_registered_sources()} == {
        WORKFLOW_A_UUID,
        WORKFLOW_B_UUID,
    }
    composition.reset_workflow_service_for_test()

    hidden_root_b = tmp_path / "inactive-beta"
    root_b.rename(hidden_root_b)
    only_a = composition.compose_workflow_runtime(
        working_dir,
        editable_package_roots=(root_a,),
    )
    assert [row["workflow_uuid"] for row in only_a.list_registered_sources()] == [
        WORKFLOW_A_UUID
    ]
    _assert_source_access_rejected(only_a, WORKFLOW_B_UUID)
    assert only_a.get_workflow(WORKFLOW_B_UUID)["uuid"] == WORKFLOW_B_UUID
    composition.reset_workflow_service_for_test()

    hidden_root_b.rename(root_b)
    reauthorized = composition.compose_workflow_runtime(
        working_dir,
        editable_package_roots=(root_b,),
    )
    assert reauthorized.get_authoring(WORKFLOW_B_UUID)["draft"][
        "python_source"
    ] == source_b.read_text(encoding="utf-8")


def test_empty_current_roots_do_not_activate_or_read_historical_sources(
    tmp_path: Path,
) -> None:
    """空 allowlist 重启不得访问任何既有来源路径。

    参数：``tmp_path`` 隔离持久数据库和历史包。返回：无；证明工作流定义仍可读，
    但来源枚举和全部创作文件入口均拒绝历史注册。
    """

    working_dir = tmp_path / "runtime"
    root_a = tmp_path / "editable-a"
    _seed_workflows(working_dir)
    _write_package(
        root_a,
        package_id="alpha_lab",
        workflow_uuid=WORKFLOW_A_UUID,
    )
    composition.compose_workflow_runtime(
        working_dir,
        editable_package_roots=(root_a,),
    )
    composition.reset_workflow_service_for_test()
    root_a.rename(tmp_path / "historical-alpha")

    empty = composition.compose_workflow_runtime(
        working_dir,
        editable_package_roots=(),
    )

    assert empty.list_registered_sources() == []
    _assert_source_access_rejected(empty, WORKFLOW_A_UUID)
    assert empty.get_workflow(WORKFLOW_A_UUID)["uuid"] == WORKFLOW_A_UUID


def test_single_source_replacement_activates_exact_identity_only(
    tmp_path: Path,
) -> None:
    """单项来源授权替换必须复用规范不变量并显式激活其唯一身份。

    参数：``tmp_path`` 隔离数据库和包目录。返回：无；证明重启后历史行不活跃，
    完全相同注册可重新激活且不能静默重绑定。
    """

    database_path = tmp_path / "workflow.db"
    package_root = tmp_path / "alpha_lab"
    source_path = package_root / "workflows" / "demo.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("value = 'alpha'\n", encoding="utf-8")
    first = WorkflowService(WorkflowStore(database_path))
    first.create_workflow(
        workflow_uuid=WORKFLOW_A_UUID,
        name="alpha",
        tags=[],
        description=None,
        meta_data={},
    )
    first.replace_active_editable_source_authorization(
        workflow_uuid=WORKFLOW_A_UUID,
        package_id="alpha_lab",
        package_root=package_root,
        relative_path="workflows/demo.py",
    )
    assert [row["workflow_uuid"] for row in first.list_registered_sources()] == [
        WORKFLOW_A_UUID
    ]
    first.close()

    reopened = WorkflowService(WorkflowStore(database_path))
    try:
        assert reopened.list_registered_sources() == []
        _assert_source_access_rejected(reopened, WORKFLOW_A_UUID)
        reopened.replace_active_editable_source_authorization(
            workflow_uuid=WORKFLOW_A_UUID,
            package_id="alpha_lab",
            package_root=package_root,
            relative_path="workflows/demo.py",
        )
        assert reopened.get_authoring(WORKFLOW_A_UUID)["draft"] is not None
    finally:
        reopened.close()


def test_single_source_command_explicitly_replaces_active_authorization_set(
    tmp_path: Path,
) -> None:
    """单项来源命令必须用 replace 名称公开其整集合替换语义。

    参数：``tmp_path`` 隔离数据库和两份工作流源码（Workflow Source）。返回：
    无；第二次命令后只有 B 保持活动授权，且不再公开误导为增量添加的旧方法名。
    """

    service = WorkflowService(WorkflowStore(tmp_path / "workflow.db"))
    package_a = tmp_path / "alpha"
    package_b = tmp_path / "beta"
    for workflow_uuid, package_root in (
        (WORKFLOW_A_UUID, package_a),
        (WORKFLOW_B_UUID, package_b),
    ):
        service.create_workflow(
            workflow_uuid=workflow_uuid,
            name=f"workflow-{workflow_uuid[:8]}",
            tags=[],
            description=None,
            meta_data={},
        )
        source_path = package_root / "workflows" / "demo.py"
        source_path.parent.mkdir(parents=True)
        source_path.write_text("value = True\n", encoding="utf-8")

    try:
        service.replace_active_editable_source_authorization(
            workflow_uuid=WORKFLOW_A_UUID,
            package_id="alpha",
            package_root=package_a,
            relative_path="workflows/demo.py",
        )
        service.replace_active_editable_source_authorization(
            workflow_uuid=WORKFLOW_B_UUID,
            package_id="beta",
            package_root=package_b,
            relative_path="workflows/demo.py",
        )
        active_workflows = {
            row["workflow_uuid"] for row in service.list_registered_sources()
        }
    finally:
        service.close()

    assert active_workflows == {WORKFLOW_B_UUID}
    assert not hasattr(WorkflowService, "register_editable_source")
