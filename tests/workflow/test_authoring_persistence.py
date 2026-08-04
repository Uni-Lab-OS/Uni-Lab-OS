"""可信工作流创作的草稿、候选和应用持久化合同测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.service import WorkflowConflict, WorkflowService
from unilabos.workflow.store import WorkflowStore

from .test_authoring_engine import WORKFLOW_UUID, _engine, _source


@pytest.fixture()
def authoring_service(tmp_path: Path) -> tuple[WorkflowService, Path]:
    """创建带真实 SQLite 与受控包目录的工作流服务（WorkflowService）。

    参数说明：``tmp_path`` 是 pytest 隔离目录；返回服务和规范作者源码路径，
    测试结束后关闭数据库连接。
    """

    package_root = tmp_path / "package"
    workflows_dir = package_root / "workflows"
    workflows_dir.mkdir(parents=True)
    source_path = workflows_dir / "sample.py"
    service = WorkflowService(
        WorkflowStore(tmp_path / "workflow.db"),
        compiler=_engine(),
    )
    service.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="Persisted name",
        tags=["keep"],
        description="Persisted description",
        meta_data={"owner": "keep"},
    )
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="lab",
        package_root=package_root,
        relative_path="workflows/sample.py",
    )
    try:
        yield service, source_path
    finally:
        service.close()


def test_draft_candidate_apply_is_atomic_and_backend_shaped(
    authoring_service: tuple[WorkflowService, Path],
) -> None:
    """草稿应产生候选，应用应原子更新图、修订和规范源码。"""

    service, source_path = authoring_service
    draft = service.save_draft(
        WORKFLOW_UUID,
        python_source=_source(),
        expected_draft_hash=None,
        expected_workflow_revision=1,
    )

    candidate = draft["candidate"]
    assert candidate is not None
    assert candidate["candidate_hash"].startswith("sha256:")
    assert candidate["template_catalog_fingerprint"] == (
        service.compiler.template_catalog_fingerprint
    )
    assert service.get_graph(WORKFLOW_UUID)["nodes"] == []

    applied = service.apply_authoring(
        WORKFLOW_UUID,
        expected_draft_hash=draft["draft"]["draft_hash"],
        expected_workflow_revision=1,
        expected_candidate_hash=candidate["candidate_hash"],
    )

    graph = service.get_graph(WORKFLOW_UUID)
    assert graph["workflow"]["revision"] == 2
    assert len(graph["nodes"]) == 2
    assert applied["apply_result"]["workflow_revision"] == 2
    authoring = applied["authoring"]
    assert source_path.read_text(encoding="utf-8") == authoring["draft"]["python_source"]
    assert authoring["candidate"] is None


def test_stale_candidate_fails_without_partial_graph_or_source_writeback(
    authoring_service: tuple[WorkflowService, Path],
) -> None:
    """候选哈希冲突必须保持已应用图和作者草稿不变。"""

    service, source_path = authoring_service
    draft = service.save_draft(
        WORKFLOW_UUID,
        python_source=_source(),
        expected_draft_hash=None,
        expected_workflow_revision=1,
    )
    before_graph = service.get_graph(WORKFLOW_UUID)
    before_source = source_path.read_text(encoding="utf-8")

    with pytest.raises(WorkflowConflict) as failure:
        service.apply_authoring(
            WORKFLOW_UUID,
            expected_draft_hash=draft["draft"]["draft_hash"],
            expected_workflow_revision=1,
            expected_candidate_hash="sha256:" + "0" * 64,
        )

    assert failure.value.code == "candidate_hash_conflict"
    assert service.get_graph(WORKFLOW_UUID) == before_graph
    assert source_path.read_text(encoding="utf-8") == before_source


def test_authoring_http_keeps_backend_response_envelope(
    authoring_service: tuple[WorkflowService, Path],
) -> None:
    """工作流创作 HTTP 接口必须保持后端（Backend）响应外层结构。"""

    service, _source_path = authoring_service
    client = TestClient(create_workflow_app(service))

    response = client.put(
        f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/draft",
        json={
            "python_source": _source(),
            "expected_draft_hash": None,
            "expected_workflow_revision": 1,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"code", "data"}
    assert payload["code"] == 0
    assert payload["data"]["candidate"]["candidate_hash"].startswith("sha256:")
