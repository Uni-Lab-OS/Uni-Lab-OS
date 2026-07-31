"""Phase 01A 单工作区 Authority 与单 token Apply 公共合同测试。"""

from __future__ import annotations

import multiprocessing
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.composition import (
    compose_workflow_runtime,
    reset_workflow_service_for_test,
)
from unilabos.workflow.models import CandidateCompilation
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
SECOND_WORKFLOW_UUID = "22222222-2222-4222-8222-222222222222"
NODE_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CATALOG_FINGERPRINT = f"sha256:{'c' * 64}"
INVALID_INPUT = {
    "code": 400,
    "error": {
        "code": "invalid_input",
        "message": "提交内容格式不正确",
    },
}


class NormalizingCompiler:
    """只补末尾换行，便于区分 Candidate 预览与已物化 Candidate。"""

    compiler_version = "phase-01a-contract-v1"
    template_catalog_fingerprint = CATALOG_FINGERPRINT

    def compile(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        python_source: str,
        source_uri: str,
        applied_graph: dict[str, Any],
    ) -> CandidateCompilation:
        del workflow_uuid, workflow_revision, source_uri
        normalized = (
            python_source if python_source.endswith("\n") else python_source + "\n"
        )
        return CandidateCompilation(
            diagnostics=[],
            graph={
                "workflow": applied_graph["workflow"],
                "nodes": [
                    {
                        "uuid": NODE_UUID,
                        "workflow_node_template_uuid": None,
                        "parent_uuid": None,
                        "material_uuid": None,
                        "name": "phase 01A candidate node",
                        "status": "idle",
                        "type": "compute",
                        "icon": None,
                        "pose": {},
                        "param": {},
                        "footer": None,
                        "action_name": None,
                        "action_type": None,
                        "execution_policy": {},
                        "disabled": False,
                        "minimized": False,
                        "script": None,
                        "description": None,
                        "meta_data": {},
                    }
                ],
                "edges": [],
                "node_templates": [],
                "handle_templates": [],
            },
            normalized_python_source=normalized,
            source_map=[
                {
                    "workflow_node_uuid": NODE_UUID,
                    "start_line": 1,
                    "start_column": 1,
                    "end_line": 1,
                    "end_column": max(1, len(normalized.rstrip("\n"))),
                }
            ],
            changeset={
                "kind": "graph",
                "created_node_uuids": [NODE_UUID],
                "updated_node_uuids": [],
                "deleted_node_uuids": [],
                "created_edge_uuids": [],
                "updated_edge_uuids": [],
                "deleted_edge_uuids": [],
                "reserved_metadata_changed": False,
            },
            compiler_version=self.compiler_version,
            template_catalog_fingerprint=self.template_catalog_fingerprint,
        )


@contextmanager
def _authoring_client(
    tmp_path: Path,
) -> Iterator[tuple[TestClient, Path]]:
    store = WorkflowStore(tmp_path / "unilabos_data" / "workflow.db")
    service = WorkflowService(store, compiler=NormalizingCompiler())
    service.create_workflow(
        name="phase 01A authoring",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )
    package_root = tmp_path / "package"
    package_root.mkdir()
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="phase_01a_contract",
        package_root=package_root,
        relative_path="workflows/demo.py",
    )
    try:
        with TestClient(create_workflow_app(service)) as client:
            yield client, package_root / "workflows" / "demo.py"
    finally:
        store.close()


def _save_draft(
    client: TestClient,
    python_source: str,
    *,
    expected_draft_hash: str | None = None,
) -> dict[str, Any]:
    response = client.put(
        f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/draft",
        json={
            "python_source": python_source,
            "expected_draft_hash": expected_draft_hash,
            "expected_workflow_revision": 1,
        },
    )
    assert response.status_code == 200
    return response.json()["data"]


def test_apply_accepts_one_server_candidate_hash(tmp_path: Path) -> None:
    with _authoring_client(tmp_path) as (client, _draft_path):
        saved = _save_draft(client, "result = build()\n")

        response = client.post(
            f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/apply",
            json={"candidate_hash": saved["candidate"]["candidate_hash"]},
        )

    assert response.status_code == 200
    assert response.json()["data"]["apply_result"]["warnings"] == []


def test_apply_rejects_legacy_three_token_request(tmp_path: Path) -> None:
    with _authoring_client(tmp_path) as (client, _draft_path):
        saved = _save_draft(client, "result = build()\n")

        response = client.post(
            f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/apply",
            json={
                "expected_draft_hash": saved["draft"]["draft_hash"],
                "expected_workflow_revision": 1,
                "expected_candidate_hash": saved["candidate"]["candidate_hash"],
            },
        )

    assert response.status_code == 400
    assert response.json() == INVALID_INPUT


def test_apply_rejects_extra_client_candidate_bundle(tmp_path: Path) -> None:
    with _authoring_client(tmp_path) as (client, _draft_path):
        saved = _save_draft(client, "result = build()\n")

        response = client.post(
            f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/apply",
            json={
                "candidate_hash": saved["candidate"]["candidate_hash"],
                "candidate": saved["candidate"],
            },
        )

    assert response.status_code == 400
    assert response.json() == INVALID_INPUT


def test_apply_rejects_candidate_not_materialized_as_draft(tmp_path: Path) -> None:
    with _authoring_client(tmp_path) as (client, draft_path):
        saved = _save_draft(client, "result=build()")
        original_bytes = draft_path.read_bytes()

        response = client.post(
            f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/apply",
            json={"candidate_hash": saved["candidate"]["candidate_hash"]},
        )
        authoring = client.get(
            f"/api/v1/workflows/{WORKFLOW_UUID}/authoring"
        ).json()["data"]

    assert response.status_code == 409
    assert response.json() == {
        "code": 409,
        "error": {
            "code": "candidate_not_materialized",
            "message": "请先接受并保存规范化源码，再应用工作流",
        },
    }
    assert draft_path.read_bytes() == original_bytes
    assert authoring["workflow_revision"] == 1
    assert authoring["candidate"]["candidate_hash"] == (
        saved["candidate"]["candidate_hash"]
    )


def test_materialized_apply_does_not_open_any_file_for_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _authoring_client(tmp_path) as (client, draft_path):
        preview = _save_draft(client, "result=build()")
        normalized_source = preview["candidate"]["normalized_python_source"]
        materialized = _save_draft(
            client,
            normalized_source,
            expected_draft_hash=preview["draft"]["draft_hash"],
        )
        original_bytes = draft_path.read_bytes()
        write_attempts: list[str] = []
        original_open = os.open
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

        def reject_file_write(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            if flags & write_flags:
                write_attempts.append(os.fsdecode(path))
                raise AssertionError("Apply 不得打开任何文件写入")
            if dir_fd is None:
                return original_open(path, flags, mode)
            return original_open(path, flags, mode, dir_fd=dir_fd)

        with monkeypatch.context() as patch:
            # os.open 是 package Draft 文件系统边界；SQLite 写入不经过该 Python API。
            patch.setattr(os, "open", reject_file_write)
            response = client.post(
                f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/apply",
                json={
                    "candidate_hash": materialized["candidate"]["candidate_hash"]
                },
            )

        final_bytes = draft_path.read_bytes()

    assert response.status_code == 200
    assert response.json()["data"]["apply_result"]["warnings"] == []
    assert write_attempts == []
    assert final_bytes == original_bytes


def _compose_same_workspace_in_child(
    working_dir: str,
    outcome: Any,
) -> None:
    """以全新解释器模拟第二个 OS Authority 进程。"""

    started_at = time.monotonic()
    try:
        compose_workflow_runtime(working_dir)
    except RuntimeError as error:
        outcome.put(("rejected", str(error), time.monotonic() - started_at))
    except Exception as error:  # noqa: BLE001 - 子进程需将意外类型回传父进程
        outcome.put(
            (
                "unexpected_error",
                type(error).__name__,
                str(error),
                time.monotonic() - started_at,
            )
        )
    else:
        outcome.put(("opened", "", time.monotonic() - started_at))
    finally:
        reset_workflow_service_for_test()


def test_one_workspace_authority_manages_many_workflows_and_rejects_second_process(
    tmp_path: Path,
) -> None:
    working_dir = tmp_path / "unilabos_data"
    reset_workflow_service_for_test()
    try:
        service = compose_workflow_runtime(working_dir)
        for workflow_uuid, name in (
            (WORKFLOW_UUID, "first workflow"),
            (SECOND_WORKFLOW_UUID, "second workflow"),
        ):
            service.create_workflow(
                name=name,
                tags=[],
                description=None,
                meta_data={},
                workflow_uuid=workflow_uuid,
            )

        listed_before = service.list_workflows(page=1, page_size=20)["items"]
        context = multiprocessing.get_context("spawn")
        outcome = context.Queue()
        process = context.Process(
            target=_compose_same_workspace_in_child,
            args=(str(working_dir), outcome),
        )
        process.start()
        process.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
            pytest.fail("第二个 OS Authority 进程未立即结束")
        child_result = outcome.get(timeout=2)
        listed_after = service.list_workflows(page=1, page_size=20)["items"]
    finally:
        reset_workflow_service_for_test()

    assert {item["uuid"] for item in listed_before} == {
        WORKFLOW_UUID,
        SECOND_WORKFLOW_UUID,
    }
    assert child_result[0] == "rejected"
    assert child_result[-1] < 1.0
    assert {item["uuid"] for item in listed_after} == {
        WORKFLOW_UUID,
        SECOND_WORKFLOW_UUID,
    }
