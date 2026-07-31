"""Phase 01A2 Draft 线性化与工作区 Authority 生命周期公共合同。"""

from __future__ import annotations

import os
import sqlite3
import threading
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
NODE_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CATALOG_FINGERPRINT = f"sha256:{'c' * 64}"


class DraftLinearizationCompiler:
    """为真实 HTTP Apply 暴露编译完成边界，不替换任何 Service/Store 行为。"""

    compiler_version = "phase-01a2-contract-v1"
    template_catalog_fingerprint = CATALOG_FINGERPRINT

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._block_next_compile = False
        self.compile_entered = threading.Event()
        self.release_compile = threading.Event()
        self.compile_released = threading.Event()
        self.worker_native_id: int | None = None

    def block_next_compile(self) -> None:
        """将下一次编译设为 Apply 预检同步点。"""

        with self._guard:
            self._block_next_compile = True
            self.worker_native_id = None
            self.compile_entered.clear()
            self.release_compile.clear()
            self.compile_released.clear()

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
        with self._guard:
            should_block = self._block_next_compile
            self._block_next_compile = False
        if should_block:
            self.worker_native_id = threading.get_native_id()
            self.compile_entered.set()
            if not self.release_compile.wait(timeout=3):
                raise TimeoutError("测试未释放 Apply 编译")
            self.compile_released.set()

        normalized_source = (
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
                        "name": "Phase 01A2 Candidate 节点",
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
            normalized_python_source=normalized_source,
            source_map=[
                {
                    "workflow_node_uuid": NODE_UUID,
                    "start_line": 1,
                    "start_column": 1,
                    "end_line": 1,
                    "end_column": max(1, len(normalized_source.rstrip("\n"))),
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
    compiler: DraftLinearizationCompiler,
) -> Iterator[tuple[TestClient, Path, Path]]:
    database_path = tmp_path / "unilabos_data" / "workflow.db"
    store = WorkflowStore(database_path)
    service = WorkflowService(store, compiler=compiler)
    service.create_workflow(
        name="Phase 01A2 Draft 线性化",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )
    package_root = tmp_path / "package"
    package_root.mkdir()
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="phase_01a2_contract",
        package_root=package_root,
        relative_path="workflows/demo.py",
    )
    try:
        with TestClient(create_workflow_app(service)) as client:
            yield client, package_root / "workflows" / "demo.py", database_path
    finally:
        store.close()


def _save_materialized_candidate(client: TestClient) -> dict[str, Any]:
    response = client.put(
        f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/draft",
        json={
            "python_source": "result = build()\n",
            "expected_draft_hash": None,
            "expected_workflow_revision": 1,
        },
    )
    assert response.status_code == 200
    return response.json()["data"]


def _get_authoring(client: TestClient) -> dict[str, Any]:
    response = client.get(f"/api/v1/workflows/{WORKFLOW_UUID}/authoring")
    assert response.status_code == 200
    return response.json()["data"]


def _atomic_replace(path: Path, content: bytes) -> None:
    replacement = path.with_name(".phase-01a2-external.tmp")
    with replacement.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(replacement, path)
    parent_descriptor = os.open(
        path.parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
    )
    try:
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)


def _wait_for_sqlite_writer_contention(
    native_thread_id: int,
    *,
    timeout: float = 3,
) -> None:
    """等待请求线程进入 SQLite busy handler，避免用 sleep 猜测竞态窗口。"""

    wait_channel = Path(f"/proc/self/task/{native_thread_id}/wchan")
    deadline = time.monotonic() + timeout
    observed: set[str] = set()
    while time.monotonic() < deadline:
        try:
            channel = wait_channel.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            pytest.fail("HTTP Apply 请求线程在进入 SQLite 竞争前退出")
        observed.add(channel)
        if channel == "hrtimer_nanosleep":
            return
        os.sched_yield()
    pytest.fail(f"未观察到真实 SQLite writer contention：{sorted(observed)!r}")


def test_事务线性化点拒绝竞争期间发生的外部_draft_替换(tmp_path: Path) -> None:
    compiler = DraftLinearizationCompiler()
    with _authoring_client(tmp_path, compiler) as (
        client,
        draft_path,
        database_path,
    ):
        saved = _save_materialized_candidate(client)
        before = _get_authoring(client)
        writer = sqlite3.connect(database_path, isolation_level=None)
        writer.execute("PRAGMA busy_timeout = 0")
        writer.execute("BEGIN IMMEDIATE")
        response: dict[str, Any] = {}

        def apply() -> None:
            response["value"] = client.post(
                f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/apply",
                json={"candidate_hash": saved["candidate"]["candidate_hash"]},
            )

        compiler.block_next_compile()
        thread = threading.Thread(target=apply, name="phase-01a2-http-apply")
        thread.start()
        try:
            assert compiler.compile_entered.wait(timeout=2)
            assert compiler.worker_native_id is not None
            compiler.release_compile.set()
            assert compiler.compile_released.wait(timeout=2)
            _wait_for_sqlite_writer_contention(compiler.worker_native_id)

            external_source = b"result = externally_edited()\n"
            _atomic_replace(draft_path, external_source)
            writer.rollback()
            thread.join(timeout=3)
            assert not thread.is_alive()

            apply_response = response["value"]
            after = _get_authoring(client)
            preserved_source = draft_path.read_bytes()
        finally:
            compiler.release_compile.set()
            if writer.in_transaction:
                writer.rollback()
            writer.close()
            thread.join(timeout=3)

    assert apply_response.status_code == 409
    assert apply_response.json()["error"]["code"] == "draft_hash_conflict"
    assert after["workflow_revision"] == before["workflow_revision"] == 1
    assert after["applied_source"] == before["applied_source"] is None
    assert preserved_source == external_source


def test_同进程同工作区重复装配返回同一_authority_service(
    tmp_path: Path,
) -> None:
    working_dir = tmp_path / "unilabos_data"
    reset_workflow_service_for_test()
    try:
        first = compose_workflow_runtime(working_dir)
        second = compose_workflow_runtime(working_dir)
    finally:
        reset_workflow_service_for_test()

    assert second is first


def test_同进程运行中的_authority_明确拒绝切换工作区(tmp_path: Path) -> None:
    first_working_dir = tmp_path / "first"
    second_working_dir = tmp_path / "second"
    reset_workflow_service_for_test()
    try:
        compose_workflow_runtime(first_working_dir)
        with pytest.raises(RuntimeError, match="switch working_dir"):
            compose_workflow_runtime(second_working_dir)
    finally:
        reset_workflow_service_for_test()


def test_reset_释放工作区租约后可以重新装配_authority(tmp_path: Path) -> None:
    working_dir = tmp_path / "unilabos_data"
    reset_workflow_service_for_test()
    try:
        previous = compose_workflow_runtime(working_dir)
        reset_workflow_service_for_test()
        replacement = compose_workflow_runtime(working_dir)
    finally:
        reset_workflow_service_for_test()

    assert replacement is not previous
