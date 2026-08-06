"""工作流应用（Apply）后的创作编译器代际并发发布测试。"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow.service import WorkflowService
from unilabos.workflow.source_discovery import (
    EditableSourceDiscoveryPlan,
    EditableSourceRegistration,
)
from unilabos.workflow.store import WorkflowStore

from .test_authoring_engine import (
    ANALYZE_NODE_UUID,
    PREPARE_NODE_UUID,
    WORKFLOW_UUID,
    _engine,
    _source,
)

SECOND_WORKFLOW_UUID = "10000000-0000-4000-8000-000000000002"
SECOND_PREPARE_NODE_UUID = "20000000-0000-4000-8000-000000000011"
SECOND_ANALYZE_NODE_UUID = "20000000-0000-4000-8000-000000000012"


def _second_source() -> str:
    """生成具有独立工作流和节点身份的第二份工作流源码。

    参数：无。返回：保持相同动作目录语义、但使用第二组工作流（Workflow）与
    工作流节点（WorkflowNode）UUID 的可信作者源码。
    """

    return (
        _source()
        .replace(WORKFLOW_UUID, SECOND_WORKFLOW_UUID)
        .replace(PREPARE_NODE_UUID, SECOND_PREPARE_NODE_UUID)
        .replace(ANALYZE_NODE_UUID, SECOND_ANALYZE_NODE_UUID)
    )


def _register_sources(
    service: WorkflowService,
    package_root: Path,
) -> None:
    """一次授权两个独立工作流源码（Workflow Source）。

    参数：``service`` 是待注册来源的工作流服务（WorkflowService）；
    ``package_root`` 是本测试唯一显式授权的包目录。返回：无；注册计划固定物理
    根目录身份，源码文件随后由保存草稿（Save Draft）创建。
    """

    package_root.joinpath("workflows").mkdir(parents=True)
    root_metadata = package_root.lstat()
    relative_paths = ("workflows/first.py", "workflows/second.py")
    workflow_uuids = (WORKFLOW_UUID, SECOND_WORKFLOW_UUID)
    plan = EditableSourceDiscoveryPlan(
        registrations=tuple(
            EditableSourceRegistration(
                workflow_uuid=workflow_uuid,
                package_id="lab",
                package_root=package_root,
                relative_path=relative_path,
                source_uri=f"package://lab/{relative_path}",
            )
            for workflow_uuid, relative_path in zip(
                workflow_uuids,
                relative_paths,
                strict=True,
            )
        ),
        root_identities=(
            ((package_root, (root_metadata.st_dev, root_metadata.st_ino))),
        ),
    )
    service.replace_discovered_source_authorizations(plan)


def _create_workflow(service: WorkflowService, workflow_uuid: str) -> None:
    """创建一个可进入可信创作生命周期的工作流（Workflow）。

    参数：``service`` 是本测试唯一服务；``workflow_uuid`` 是待创建的稳定身份。
    返回：无；新定义固定从工作流修订（Workflow Revision）1 开始。
    """

    service.create_workflow(
        workflow_uuid=workflow_uuid,
        name=f"workflow-{workflow_uuid[-4:]}",
        tags=[],
        description="目录代际并发发布测试",
        meta_data={},
    )


def _save_candidate(
    service: WorkflowService,
    *,
    workflow_uuid: str,
    python_source: str,
) -> dict[str, Any]:
    """保存源码并取得服务端签发的候选版本（Candidate）。

    参数：``service`` 是已授权的工作流服务；``workflow_uuid`` 与
    ``python_source`` 共同指定本次草稿。返回：非空候选版本；候选缺失时测试
    立即失败。
    """

    aggregate = service.save_draft(
        workflow_uuid,
        python_source=python_source,
        expected_draft_hash=None,
        expected_workflow_revision=1,
    )
    candidate = aggregate["candidate"]
    assert candidate is not None
    return candidate


def test_older_concurrent_apply_cannot_overwrite_newer_compiler_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """较早应用的迟到刷新不得覆盖较新目录代际。

    参数：``tmp_path`` 提供真实 SQLite 与两个源码文件的隔离目录；
    ``monkeypatch`` 只在真实提交返回后记录第二个线性化点。返回：无；用事件
    闸门确定性交错两个不同工作流的应用（Apply），断言较新提交发布的编译器
    代际仍是最终权威。线程异常会作为测试失败原样暴露。
    """

    first_rebuild_entered = threading.Event()
    release_first_rebuild = threading.Event()
    second_commit_completed = threading.Event()
    second_rebuild_entered = threading.Event()
    release_second_rebuild = threading.Event()
    rebuild_guard = threading.Lock()
    rebuild_calls = 0
    older_catalog_generation = _engine()
    newer_catalog_generation = _engine()

    def rebuild_compiler() -> Any:
        """按调用顺序返回可区分的目录编译器代际。

        参数：无。返回：第一次调用等待第二次应用发布后返回较旧代际；第二次
        调用立即返回较新代际。异常：闸门超时以断言失败暴露测试死锁。
        """

        nonlocal rebuild_calls
        with rebuild_guard:
            rebuild_calls += 1
            call_number = rebuild_calls
        if call_number == 1:
            first_rebuild_entered.set()
            assert release_first_rebuild.wait(timeout=5)
            return older_catalog_generation
        assert call_number == 2
        second_rebuild_entered.set()
        assert release_second_rebuild.wait(timeout=5)
        return newer_catalog_generation

    service = WorkflowService(
        WorkflowStore(tmp_path / "workflow.db"),
        compiler=_engine(),
        compiler_rebuilder=rebuild_compiler,
    )
    _create_workflow(service, WORKFLOW_UUID)
    _create_workflow(service, SECOND_WORKFLOW_UUID)
    _register_sources(service, tmp_path / "package")
    first_candidate = _save_candidate(
        service,
        workflow_uuid=WORKFLOW_UUID,
        python_source=_source(),
    )
    second_candidate = _save_candidate(
        service,
        workflow_uuid=SECOND_WORKFLOW_UUID,
        python_source=_second_source(),
    )
    first_worker_failures: list[BaseException] = []
    second_worker_failures: list[BaseException] = []
    original_apply_candidate: Callable[..., Any] = (
        service._store.apply_authoring_candidate
    )

    def observe_second_commit(*args: Any, **kwargs: Any) -> Any:
        """在真实 SQLite 应用事务返回后记录第二个工作流的线性化点。

        参数：``args`` 与 ``kwargs`` 原样转交存储公共操作。返回：真实存储结果；
        只有第二个工作流提交成功后才打开事件，不替换或缩短事务语义。
        """

        result = original_apply_candidate(*args, **kwargs)
        if kwargs.get("workflow_uuid") == SECOND_WORKFLOW_UUID:
            second_commit_completed.set()
        return result

    monkeypatch.setattr(
        service._store,
        "apply_authoring_candidate",
        observe_second_commit,
    )

    def apply_first_workflow() -> None:
        """在线程中应用第一个工作流并捕获任何失败。

        参数：无；闭包读取第一个候选哈希。返回：无；异常写入
        ``first_worker_failures``，供主线程在解除闸门后确定性断言。
        """

        try:
            service.apply_authoring(
                WORKFLOW_UUID,
                candidate_hash=first_candidate["candidate_hash"],
            )
        except BaseException as error:  # noqa: BLE001 - 测试需回传线程异常
            first_worker_failures.append(error)

    def apply_second_workflow() -> None:
        """在线程中应用第二个工作流并捕获任何失败。

        参数：无；闭包读取第二个候选哈希。返回：无；异常写入
        ``second_worker_failures``。测试允许正确实现串行化刷新临界区，但不允许
        较旧代际在第二个应用提交完成后成为最终权威。
        """

        try:
            service.apply_authoring(
                SECOND_WORKFLOW_UUID,
                candidate_hash=second_candidate["candidate_hash"],
            )
        except BaseException as error:  # noqa: BLE001 - 测试需回传线程异常
            second_worker_failures.append(error)

    first_worker = threading.Thread(target=apply_first_workflow, daemon=True)
    second_worker = threading.Thread(target=apply_second_workflow, daemon=True)
    first_worker.start()
    try:
        assert first_rebuild_entered.wait(timeout=5)
        second_worker.start()
        assert second_commit_completed.wait(timeout=5)
        if second_rebuild_entered.wait(timeout=1):
            # 当前无刷新临界区的实现进入此路径：先让较新代际发布，再释放旧刷新。
            release_second_rebuild.set()
            second_worker.join(timeout=5)
            assert not second_worker.is_alive()
            assert service.compiler is newer_catalog_generation
            release_first_rebuild.set()
        else:
            # 正确实现也可以串行化刷新发布；先让旧刷新退出，再允许新代际发布。
            release_first_rebuild.set()
            assert second_rebuild_entered.wait(timeout=5)
            release_second_rebuild.set()
    finally:
        release_first_rebuild.set()
        release_second_rebuild.set()
        first_worker.join(timeout=5)
        second_worker.join(timeout=5)
        service.close()

    assert not first_worker.is_alive()
    assert not second_worker.is_alive()
    assert first_worker_failures == []
    assert second_worker_failures == []
    assert rebuild_calls == 2
    assert service.compiler is newer_catalog_generation
