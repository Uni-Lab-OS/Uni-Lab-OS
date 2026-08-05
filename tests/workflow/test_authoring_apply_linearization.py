"""可信工作流创作应用的线性化与并发冲突测试。"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import unilabos.workflow.service as workflow_service_module
from unilabos.workflow.service import WorkflowConflict, WorkflowService
from unilabos.workflow.store import WorkflowStore

from .test_authoring_engine import WORKFLOW_UUID, _engine, _source

_EXTERNAL_SOURCE = "# 外部编辑必须保留\n"


@pytest.fixture()
def authoring_runtime(
    tmp_path: Path,
) -> Iterator[tuple[WorkflowService, Path, Path]]:
    """创建带独立源码工作区与真实 SQLite 的工作流创作运行环境。

    参数：``tmp_path`` 是 pytest 提供的隔离目录。返回：依次产出工作流服务
    （WorkflowService）、规范源码路径和数据库路径。异常：夹具退出时始终关闭
    服务连接，测试内的领域异常由各用例自行断言。
    """

    # ``database_path`` 是本轮真实写者争用共用的工作流数据库路径。
    database_path = tmp_path / "workflow.db"
    package_root = tmp_path / "package"
    source_path = package_root / "workflows" / "sample.py"
    source_path.parent.mkdir(parents=True)
    service = WorkflowService(WorkflowStore(database_path), compiler=_engine())
    service.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="Linearized workflow",
        tags=[],
        description="线性化测试工作流",
        meta_data={},
    )
    service.replace_active_editable_source_authorization(
        workflow_uuid=WORKFLOW_UUID,
        package_id="lab",
        package_root=package_root,
        relative_path="workflows/sample.py",
    )
    try:
        yield service, source_path, database_path
    finally:
        service.close()


def _save_candidate(service: WorkflowService) -> dict[str, Any]:
    """保存草稿并返回服务端签发的候选版本（Candidate）。

    参数：``service`` 是已完成源码授权的工作流服务。返回：包含候选哈希
    （Candidate Hash）的完整持久候选。异常：候选缺失时测试立即失败；保存草稿
    的领域异常原样传播。
    """

    aggregate = service.save_draft(
        WORKFLOW_UUID,
        python_source=_source(),
        expected_draft_hash=None,
        expected_workflow_revision=1,
    )
    # ``candidate`` 是服务端持久化后允许客户端引用的唯一候选事实。
    candidate = aggregate["candidate"]
    assert candidate is not None
    return candidate


def test_apply_rejects_candidate_when_derived_draft_is_stale(
    authoring_runtime: tuple[WorkflowService, Path, Path],
) -> None:
    """应用应从候选推导草稿哈希并拒绝已被外部编辑的源码。

    参数：``authoring_runtime`` 提供真实源码和 SQLite。返回：无；断言冲突前后
    工作流图不变，且不会覆盖外部源码。异常：只接受稳定的
    ``draft_hash_conflict``。
    """

    service, source_path, _database_path = authoring_runtime
    candidate = _save_candidate(service)
    before_graph = service.get_graph(WORKFLOW_UUID)
    source_path.write_text(_EXTERNAL_SOURCE, encoding="utf-8")

    with pytest.raises(WorkflowConflict) as failure:
        service.apply_authoring(
            WORKFLOW_UUID,
            candidate_hash=candidate["candidate_hash"],
        )

    assert failure.value.code == "draft_hash_conflict"
    assert service.get_graph(WORKFLOW_UUID) == before_graph
    assert source_path.read_text(encoding="utf-8") == _EXTERNAL_SOURCE


def test_apply_rejects_candidate_when_derived_revision_is_stale(
    authoring_runtime: tuple[WorkflowService, Path, Path],
) -> None:
    """应用应从候选推导工作流修订并拒绝已推进的图。

    参数：``authoring_runtime`` 提供真实源码和 SQLite。返回：无；断言旧候选
    不能再次推进工作流修订（Workflow Revision）。异常：只接受稳定的
    ``workflow_revision_conflict``。
    """

    service, _source_path, _database_path = authoring_runtime
    candidate = _save_candidate(service)
    service.save_graph(WORKFLOW_UUID, revision=1, nodes=[], edges=[])

    with pytest.raises(WorkflowConflict) as failure:
        service.apply_authoring(
            WORKFLOW_UUID,
            candidate_hash=candidate["candidate_hash"],
        )

    assert failure.value.code == "workflow_revision_conflict"
    assert service.get_graph(WORKFLOW_UUID)["workflow"]["revision"] == 2
    assert service.get_graph(WORKFLOW_UUID)["nodes"] == []


def test_apply_rechecks_catalog_authority_inside_write_transaction(
    authoring_runtime: tuple[WorkflowService, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """目录指纹在事务线性化点变化时必须回滚候选应用。

    参数：``authoring_runtime`` 提供真实源码和 SQLite；``monkeypatch`` 控制目录
    权威（Catalog Authority）只在事务内复核时切换世代。返回：无；断言公共应用
    返回 ``template_catalog_conflict``，且图和修订均未推进。异常：不接受事务外
    预检成功后继续提交旧目录候选。
    """

    service, _source_path, _database_path = authoring_runtime
    candidate = _save_candidate(service)
    expected_fingerprint = candidate["template_catalog_fingerprint"]
    changed_fingerprint = "sha256:" + "f" * 64
    # ``authority_reads`` 只记录本次应用中目录权威的可观察读取次数。
    authority_reads = 0

    def catalog_fingerprint_at_linearization() -> str:
        """在事务外两次预检后模拟目录权威世代变化。

        参数：无；闭包读取当前调用次数。返回：前两次返回候选目录指纹，第三次
        返回新指纹。异常：无，用于验证事务内回调确实再次读取目录权威。
        """

        nonlocal authority_reads
        authority_reads += 1
        if authority_reads <= 2:
            return expected_fingerprint
        return changed_fingerprint

    monkeypatch.setattr(
        service,
        "_catalog_fingerprint",
        catalog_fingerprint_at_linearization,
    )

    with pytest.raises(WorkflowConflict) as failure:
        service.apply_authoring(
            WORKFLOW_UUID,
            candidate_hash=candidate["candidate_hash"],
        )

    assert failure.value.code == "template_catalog_conflict"
    assert authority_reads == 3
    assert service.get_graph(WORKFLOW_UUID)["workflow"]["revision"] == 1
    assert service.get_graph(WORKFLOW_UUID)["nodes"] == []


def test_apply_rereads_revision_after_real_sqlite_writer_contention(
    authoring_runtime: tuple[WorkflowService, Path, Path],
) -> None:
    """应用获得写事务后必须重读真实 SQLite 并发写者提交的修订。

    参数：``authoring_runtime`` 提供共享数据库路径。返回：无；外部写者先持有
    ``BEGIN IMMEDIATE``，应用线程等待后只能得到修订冲突，不能留下候选图。
    异常：工作线程只允许返回 ``workflow_revision_conflict``。
    """

    service, _source_path, database_path = authoring_runtime
    candidate = _save_candidate(service)
    writer = sqlite3.connect(database_path)
    writer.execute("PRAGMA busy_timeout = 5000")
    writer.execute("BEGIN IMMEDIATE")
    writer.execute(
        "UPDATE workflow SET revision = 2 WHERE uuid = ?",
        (WORKFLOW_UUID,),
    )
    started = threading.Event()
    # ``failures`` 保存应用线程的唯一稳定领域冲突，不承担跨线程状态权威。
    failures: list[BaseException] = []

    def apply_in_worker() -> None:
        """在独立线程应用旧候选以制造真实写事务等待。

        参数：无；闭包使用已授权服务和候选。返回：无；捕获的领域异常写入
        ``failures`` 供主线程断言，未捕获异常同样保留以暴露测试失败。
        """

        started.set()
        try:
            service.apply_authoring(
                WORKFLOW_UUID,
                candidate_hash=candidate["candidate_hash"],
            )
        except BaseException as error:  # noqa: BLE001 - 测试需回传线程异常
            failures.append(error)

    worker = threading.Thread(target=apply_in_worker, daemon=True)
    worker.start()
    assert started.wait(timeout=1)
    worker.join(timeout=0.1)
    assert worker.is_alive()
    writer.commit()
    writer.close()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], WorkflowConflict)
    assert failures[0].code == "workflow_revision_conflict"
    assert service.get_graph(WORKFLOW_UUID)["workflow"]["revision"] == 2
    assert service.get_graph(WORKFLOW_UUID)["nodes"] == []


def test_external_edit_after_linearization_survives_postcommit_writeback(
    authoring_runtime: tuple[WorkflowService, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """线性化后出现的外部编辑必须令提交后源码 CAS 写回降级而非覆盖。

    参数：``authoring_runtime`` 提供真实源码和 SQLite；``monkeypatch`` 仅在提交后
    写回适配器调用前插入外部编辑。返回：无；图事务保持成功，响应包含待写回
    警告，规范源码保留外部内容。异常：提交后的 CAS 冲突不得伪装成应用失败。
    """

    service, source_path, _database_path = authoring_runtime
    candidate = _save_candidate(service)
    original_write = workflow_service_module.write_registered_source

    def edit_then_write(
        registration: dict[str, Any],
        content: bytes,
        *,
        expected_hash: Any,
    ) -> None:
        """先模拟外部源码编辑，再调用真实源码工作区 CAS 写入。

        参数：``registration`` 是来源注册；``content`` 是规范化源码字节；
        ``expected_hash`` 是线性化时观测的旧草稿哈希。返回：无。异常：真实适配器
        必须因哈希变化抛出源码工作区冲突，服务随后转为可恢复警告。
        """

        source_path.write_text(_EXTERNAL_SOURCE, encoding="utf-8")
        original_write(registration, content, expected_hash=expected_hash)

    monkeypatch.setattr(
        workflow_service_module,
        "write_registered_source",
        edit_then_write,
    )

    result = service.apply_authoring(
        WORKFLOW_UUID,
        candidate_hash=candidate["candidate_hash"],
    )

    assert result["apply_result"]["workflow_revision"] == 2
    assert result["apply_result"]["warnings"] == [
        {
            "code": "draft_writeback_pending",
            "message": "工作流已应用，但本地源码同步失败；OS 已保留可恢复的源码记录。",
        }
    ]
    assert len(service.get_graph(WORKFLOW_UUID)["nodes"]) == 2
    assert source_path.read_text(encoding="utf-8") == _EXTERNAL_SOURCE
