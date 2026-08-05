"""可编辑包（Editable Package）首次安装工作流定义的原子合同测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest

from unilabos.workflow.store import StoreConflict, StoreNotFound, WorkflowStore

WORKFLOW_A_UUID = "11111111-1111-4111-8111-111111111111"
WORKFLOW_B_UUID = "22222222-2222-4222-8222-222222222222"


def _registration(
    *,
    workflow_uuid: str = WORKFLOW_A_UUID,
    package_id: str = "alpha_lab",
    package_root: str = "/workspace/alpha_lab",
    relative_path: str = "workflows/demo.py",
    source_uri: str | None = None,
) -> dict[str, str]:
    """构造一项已通过源码发现（Source Discovery）校验的注册行。

    参数：``workflow_uuid`` 是清单声明的工作流（Workflow）身份；包身份、包目录、
    相对路径与来源 URI 共同形成工作流源码（Workflow Source）身份。
    返回：可交给存储安装接口的独立字典；本函数不读写文件或数据库。
    """

    # ``resolved_source_uri`` 是跨重启稳定的工作流源码（Workflow Source）寻址身份。
    resolved_source_uri = source_uri or f"package://{package_id}/{relative_path}"
    return {
        "workflow_uuid": workflow_uuid,
        "package_id": package_id,
        "package_root": package_root,
        "relative_path": relative_path,
        "source_uri": resolved_source_uri,
    }


def _assert_absent(
    store: WorkflowStore,
    workflow_uuid: str,
) -> None:
    """断言一个身份没有留下定义、来源或空创作事实。

    参数：``store`` 是本用例唯一工作流写模型；``workflow_uuid`` 是应完全回滚的
    工作流（Workflow）身份。返回：无；任一事实残留都会触发断言失败。
    """

    with pytest.raises(StoreNotFound):
        store.get_workflow(workflow_uuid)
    with pytest.raises(StoreNotFound):
        store.get_source_registration(workflow_uuid)
    # ``authoring_record`` 的空时间表示没有持久创作行，而不是已有空候选。
    authoring_record = store.get_authoring_record(workflow_uuid)
    assert authoring_record["update_time"] is None


@pytest.fixture()
def workflow_store(tmp_path: Path) -> WorkflowStore:
    """创建隔离的真实 SQLite 工作流写模型。

    参数：``tmp_path`` 是 pytest 提供的独立目录。返回：初始化完成的
    ``WorkflowStore``；用例结束后关闭连接。
    """

    # ``store`` 是单个测试内唯一允许提交工作流（Workflow）事实的本地权威。
    store = WorkflowStore(tmp_path / "workflow_history.db")
    try:
        yield store
    finally:
        store.close()


def test_missing_definition_is_created_with_stable_manifest_provenance(
    workflow_store: WorkflowStore,
) -> None:
    """缺失身份应创建最小工作流骨架、来源注册与空创作事实。

    参数：``workflow_store`` 是真实 SQLite 工作流权威。返回：无；测试冻结首次名称、
    修订和清单来源追溯（Manifest Provenance），且不触发源码执行或应用。
    """

    # ``registration`` 是显式授权清单产生的唯一可信身份输入。
    registration = _registration()

    installed = workflow_store.install_discovered_sources((registration,))

    # ``workflow`` 是首次安装生成的 Backend-shaped 工作流（Workflow）骨架。
    workflow = workflow_store.get_workflow(WORKFLOW_A_UUID)
    assert installed == [workflow_store.get_source_registration(WORKFLOW_A_UUID)]
    assert workflow == {
        "uuid": WORKFLOW_A_UUID,
        "create_time": workflow["create_time"],
        "update_time": workflow["update_time"],
        "meta_data": {
            "unilab": {
                "source_bootstrap": {
                    "kind": "editable_package_manifest",
                    "package_id": "alpha_lab",
                    "relative_path": "workflows/demo.py",
                    "source_uri": "package://alpha_lab/workflows/demo.py",
                }
            }
        },
        "name": "alpha_lab.demo",
        "tags": [],
        "revision": 1,
    }
    # ``authoring_record`` 证明同一事务已经创建空创作事实，但没有编译候选。
    authoring_record = workflow_store.get_authoring_record(WORKFLOW_A_UUID)
    assert authoring_record["diagnostics"] == []
    assert authoring_record["candidate"] is None
    assert authoring_record["update_time"] is not None


def test_active_definition_is_reused_without_overwriting_user_fields_on_restart(
    tmp_path: Path,
) -> None:
    """活动工作流定义在重复安装和重启后保持用户字段与修订不变。

    参数：``tmp_path`` 保存可关闭后重开的 SQLite 文件。返回：无；测试证明首次来源
    安装只补来源与创作事实，不覆盖名称、描述、元数据、标签或修订。
    """

    database_path = tmp_path / "workflow_history.db"
    # ``registration`` 是两次进程生命周期都使用的同一工作流源码（Workflow Source）。
    registration = _registration()
    first = WorkflowStore(database_path)
    first.create_workflow(
        workflow_uuid=WORKFLOW_A_UUID,
        name="用户命名的实验",
        tags=["用户标签"],
        description="用户说明",
        meta_data={"owner": "operator"},
    )
    first.save_graph(WORKFLOW_A_UUID, revision=1, nodes=[], edges=[])
    before = first.get_workflow(WORKFLOW_A_UUID)
    first.install_discovered_sources((registration,))
    first.close()

    reopened = WorkflowStore(database_path)
    try:
        reopened.install_discovered_sources((registration,))
        after = reopened.get_workflow(WORKFLOW_A_UUID)
    finally:
        reopened.close()

    assert after == before
    assert after["revision"] == 2


def test_soft_deleted_definition_blocks_whole_batch_without_resurrection(
    workflow_store: WorkflowStore,
) -> None:
    """软删除工作流定义必须关闭式拒绝整批安装且不得复活。

    参数：``workflow_store`` 保存一个已软删除身份。返回：无；测试把缺失身份放在
    被删除身份之前，证明分类完成后才写入并且整批零部分提交。
    """

    workflow_store.create_workflow(
        workflow_uuid=WORKFLOW_B_UUID,
        name="已删除定义",
        tags=[],
        description=None,
        meta_data={},
    )
    workflow_store.delete_workflow(WORKFLOW_B_UUID)
    # ``registrations`` 的首项本可创建，后项软删除必须使两项一起失败。
    registrations = (
        _registration(),
        _registration(
            workflow_uuid=WORKFLOW_B_UUID,
            relative_path="workflows/deleted.py",
        ),
    )

    with pytest.raises(StoreConflict):
        workflow_store.install_discovered_sources(registrations)

    _assert_absent(workflow_store, WORKFLOW_A_UUID)
    with pytest.raises(StoreNotFound):
        workflow_store.get_workflow(WORKFLOW_B_UUID)
    with pytest.raises(StoreNotFound):
        workflow_store.get_source_registration(WORKFLOW_B_UUID)


def test_batch_uuid_path_uri_and_package_root_conflicts_leave_no_facts(
    tmp_path: Path,
) -> None:
    """批内四类来源身份冲突必须在任何工作流骨架写入前失败。

    参数：``tmp_path`` 为四种冲突各提供独立 SQLite。返回：无；测试覆盖工作流 UUID、
    物理路径、来源 URI 与包目录身份冲突，并逐库验证零部分事实。
    """

    # ``conflicting_batches`` 逐项表达一种独立的来源身份冲突，不依赖数据库约束顺序。
    conflicting_batches = {
        "workflow_uuid": (
            _registration(),
            _registration(relative_path="workflows/other.py"),
        ),
        "physical_path": (
            _registration(),
            _registration(
                workflow_uuid=WORKFLOW_B_UUID,
                source_uri="package://alpha_lab/workflows/other.py",
            ),
        ),
        "source_uri": (
            _registration(),
            _registration(
                workflow_uuid=WORKFLOW_B_UUID,
                relative_path="workflows/other.py",
                source_uri="package://alpha_lab/workflows/demo.py",
            ),
        ),
        "package_root": (
            _registration(),
            _registration(
                workflow_uuid=WORKFLOW_B_UUID,
                package_root="/workspace/other-alpha",
                relative_path="workflows/other.py",
            ),
        ),
    }
    for conflict_name, registrations in conflicting_batches.items():
        # ``store`` 隔离当前冲突，确保上一种失败不会影响下一种断言。
        store = WorkflowStore(tmp_path / f"{conflict_name}.db")
        try:
            with pytest.raises(StoreConflict):
                store.install_discovered_sources(registrations)
            _assert_absent(store, WORKFLOW_A_UUID)
            _assert_absent(store, WORKFLOW_B_UUID)
        finally:
            store.close()


def test_late_invalid_registration_rolls_back_earlier_missing_definition(
    workflow_store: WorkflowStore,
) -> None:
    """后项注册字段失败时不得留下前项工作流骨架。

    参数：``workflow_store`` 是真实 SQLite 权威。返回：无；测试用后项缺失 URI 模拟
    整批预检失败，并断言前项定义、来源和创作事实全部不存在。
    """

    valid_registration = _registration()
    # ``late_invalid_registration`` 缺少稳定来源 URI，代表后项确定性无效输入。
    late_invalid_registration: dict[str, Any] = _registration(
        workflow_uuid=WORKFLOW_B_UUID,
        relative_path="workflows/b.py",
    )
    late_invalid_registration.pop("source_uri")

    with pytest.raises(StoreConflict):
        workflow_store.install_discovered_sources(
            (valid_registration, late_invalid_registration)
        )

    _assert_absent(workflow_store, WORKFLOW_A_UUID)
    _assert_absent(workflow_store, WORKFLOW_B_UUID)


def test_before_commit_failure_rolls_back_definition_source_and_authoring(
    workflow_store: WorkflowStore,
) -> None:
    """提交前固定目录复核失败必须回滚同事务中的全部新事实。

    参数：``workflow_store`` 是真实 SQLite 权威。返回：无；注入的回调模拟固定包根
    目录发生变化，原异常向上传播且定义、来源与创作事实均不可见。
    """

    def reject_changed_root() -> None:
        """模拟提交前发现可编辑包根目录身份已变化。

        参数：无。返回：无；始终抛出 ``RuntimeError`` 以证明事务回滚。
        """

        raise RuntimeError("固定包目录身份已变化")

    # ``before_commit`` 是 SQL 全部完成后、真正提交前的最后安全复核点。
    before_commit: Callable[[], None] = reject_changed_root
    with pytest.raises(RuntimeError, match="固定包目录身份已变化"):
        workflow_store.install_discovered_sources(
            (_registration(),),
            before_commit=before_commit,
        )

    _assert_absent(workflow_store, WORKFLOW_A_UUID)
