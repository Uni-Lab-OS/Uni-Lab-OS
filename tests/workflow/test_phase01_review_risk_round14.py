"""Phase 01 第十四轮 Apply TOCTOU 与严格 JSON proof 风险测试。"""

from __future__ import annotations

import os
import sys
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from unilabos.app.workflow_api import (
    create_workflow_app,
    create_workflow_router,
)
from unilabos.workflow.models import WorkflowNodeWrite
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
NODE_UUID = "20000000-0000-4000-8000-000000000001"
NODE_TEMPLATE_UUID = "40000000-0000-4000-8000-000000000001"
RESOURCE_TEMPLATE_UUID = "50000000-0000-4000-8000-000000000001"
SOURCE_HANDLE_UUID = "60000000-0000-4000-8000-000000000001"
TARGET_HANDLE_UUID = "60000000-0000-4000-8000-000000000002"
CATALOG_C1 = f"sha256:{'1' * 64}"
CATALOG_C2 = f"sha256:{'2' * 64}"
SOURCE = "build()\n"
NORMALIZED_SOURCE = SOURCE
CANDIDATE_INVALID_DIAGNOSTIC = {
    "severity": "error",
    "code": "candidate_invalid",
    "message": "工作流校验失败，请检查节点、连线和输入输出",
}


def _changeset() -> dict[str, Any]:
    return {
        "kind": "source_only",
        "created_node_uuids": [],
        "updated_node_uuids": [],
        "deleted_node_uuids": [],
        "created_edge_uuids": [],
        "updated_edge_uuids": [],
        "deleted_edge_uuids": [],
        "reserved_metadata_changed": False,
    }


def _bundle(
    *,
    applied_graph: dict[str, Any],
    python_source: str,
    fingerprint: str,
) -> dict[str, Any]:
    return {
        "diagnostics": [],
        "graph": deepcopy(applied_graph),
        "normalized_python_source": (
            python_source if python_source.endswith("\n") else python_source + "\n"
        ),
        "source_map": [
            {
                "workflow_node_uuid": NODE_UUID,
                "start_line": 1,
                "start_column": 1,
                "end_line": 1,
                "end_column": 7,
            }
        ],
        "changeset": _changeset(),
        "compiler_version": "phase-01-risk-round14-v1",
        "template_catalog_fingerprint": fingerprint,
    }


class BlockingCompiler:
    compiler_version = "phase-01-risk-round14-v1"

    def __init__(self) -> None:
        self.current_fingerprint = CATALOG_C1
        self.result_fingerprint = CATALOG_C1
        self.calls = 0
        self.apply_compile_entered = threading.Event()
        self.release_apply_compile = threading.Event()

    @property
    def template_catalog_fingerprint(self) -> str:
        return self.current_fingerprint

    def compile(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        python_source: str,
        source_uri: str,
        applied_graph: dict[str, Any],
    ) -> dict[str, Any]:
        del workflow_uuid, workflow_revision, source_uri
        self.calls += 1
        if self.calls == 2:
            self.apply_compile_entered.set()
            if not self.release_apply_compile.wait(timeout=3):
                raise TimeoutError("test did not release Apply compilation")
        return _bundle(
            applied_graph=applied_graph,
            python_source=python_source,
            fingerprint=self.result_fingerprint,
        )


class StrictProofCompiler:
    compiler_version = "phase-01-risk-round14-proof-v1"
    template_catalog_fingerprint = CATALOG_C1

    def __init__(self, candidate_value: bool | int | float) -> None:
        self.candidate_value = candidate_value

    def compile(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        python_source: str,
        source_uri: str,
        applied_graph: dict[str, Any],
    ) -> dict[str, Any]:
        del workflow_uuid, workflow_revision, source_uri
        result = _bundle(
            applied_graph=applied_graph,
            python_source=python_source,
            fingerprint=CATALOG_C1,
        )
        result["graph"]["nodes"][0]["param"] = {
            "proof": self.candidate_value,
        }
        return result


def _node(*, proof_value: bool | int | float = 0) -> WorkflowNodeWrite:
    return WorkflowNodeWrite(
        uuid=NODE_UUID,
        workflow_node_template_uuid=NODE_TEMPLATE_UUID,
        name="applied node",
        status="idle",
        type="compute",
        pose={},
        param={"proof": proof_value},
        execution_policy={},
        disabled=False,
        minimized=False,
        meta_data={},
    )


def _seed_template_catalog(store: WorkflowStore) -> None:
    timestamp = "2026-07-31T00:00:00Z"
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO workflow_node_template(
                uuid, create_time, update_time, meta_data, authority_id,
                resource_template_uuid, name, display_name, class, goal,
                goal_default, feedback, result, schema, type, icon, header,
                footer, node_type
            ) VALUES (?, ?, ?, '{}', 'os-local', ?, 'source', 'Source', NULL,
                      '{}', '{}', '{}', '{}', NULL, 'action', NULL, NULL, NULL,
                      'compute')
            """,
            (
                NODE_TEMPLATE_UUID,
                timestamp,
                timestamp,
                RESOURCE_TEMPLATE_UUID,
            ),
        )
        for handle_uuid, handle_key, io_type, display_name in (
            (SOURCE_HANDLE_UUID, "result", "source", "Result"),
            (TARGET_HANDLE_UUID, "input", "target", "Input"),
        ):
            connection.execute(
                """
                INSERT INTO workflow_handle_template(
                    uuid, create_time, update_time, meta_data, authority_id,
                    workflow_node_template_uuid, handle_key, io_type,
                    display_name, type, required, data_source, data_key
                ) VALUES (?, ?, ?, '{}', 'os-local', ?, ?, ?, ?, 'number', 0,
                          NULL, NULL)
                """,
                (
                    handle_uuid,
                    timestamp,
                    timestamp,
                    NODE_TEMPLATE_UUID,
                    handle_key,
                    io_type,
                    display_name,
                ),
            )


def _open_authoring(
    tmp_path: Path,
    *,
    compiler: Any,
    proof_value: bool | int | float = 0,
) -> tuple[WorkflowStore, WorkflowService, Path]:
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store, compiler=compiler)
    service.create_workflow(
        name="phase 01 risk round 14",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )
    _seed_template_catalog(store)
    service.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=[_node(proof_value=proof_value)],
        edges=[],
    )
    package_root = tmp_path / "package"
    package_root.mkdir()
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="phase_01_risk_round_14",
        package_root=package_root,
        relative_path="workflows/review.py",
    )
    return store, service, package_root / "workflows" / "review.py"


def _save_candidate(service: WorkflowService) -> dict[str, Any]:
    saved = service.save_draft(
        WORKFLOW_UUID,
        python_source=SOURCE,
        expected_draft_hash=None,
        expected_workflow_revision=2,
    )
    assert saved["candidate"] is not None
    return saved


def _authority_snapshot(
    store: WorkflowStore,
    service: WorkflowService,
) -> dict[str, Any]:
    record = store.get_authoring_record(WORKFLOW_UUID)
    return {
        "graph": service.get_graph(WORKFLOW_UUID),
        "candidate": record["candidate"],
        "candidate_hash": record["candidate_hash"],
        "applied_source": record["applied_source"],
        "diagnostics": record["diagnostics"],
        "events": service.list_events(after_id=0)["items"],
    }


def _start_apply(
    service: WorkflowService,
    saved: dict[str, Any],
) -> tuple[threading.Thread, dict[str, Any]]:
    outcome: dict[str, Any] = {}

    def apply() -> None:
        try:
            outcome["result"] = service.apply_authoring(
                WORKFLOW_UUID,
                candidate_hash=saved["candidate"]["candidate_hash"],
            )
        except WorkflowError as error:
            outcome["error"] = {
                "code": error.code,
                "status": error.status,
            }
        except Exception as error:  # noqa: BLE001 - surface thread leakage
            outcome["unexpected"] = type(error).__name__

    thread = threading.Thread(target=apply, name="round14-apply")
    thread.start()
    return thread, outcome


def _finish_apply(
    compiler: BlockingCompiler,
    thread: threading.Thread,
) -> bool:
    compiler.release_apply_compile.set()
    thread.join(timeout=3)
    return not thread.is_alive()


def _fsync_parent(path: Path) -> None:
    descriptor = os.open(
        path.parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("mutation", ["in-place", "atomic-replace"])
def test_apply_rechecks_draft_bytes_after_blocking_compiler_before_transaction(
    tmp_path: Path,
    mutation: str,
) -> None:
    compiler = BlockingCompiler()
    store, service, source_path = _open_authoring(
        tmp_path,
        compiler=compiler,
    )
    thread: threading.Thread | None = None
    try:
        saved = _save_candidate(service)
        before = _authority_snapshot(store, service)
        thread, outcome = _start_apply(service, saved)
        compile_was_blocked = compiler.apply_compile_entered.wait(timeout=2)
        assert compile_was_blocked

        external_source = f"external_{mutation.replace('-', '_')}()\n".encode()
        if mutation == "in-place":
            with source_path.open("r+b") as stream:
                stream.seek(0)
                stream.write(external_source)
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())
        else:
            replacement = source_path.with_name(".round14-external.tmp")
            with replacement.open("xb") as stream:
                stream.write(external_source)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(replacement, source_path)
            _fsync_parent(source_path)

        thread_finished = _finish_apply(compiler, thread)
        thread = None
        after = _authority_snapshot(store, service)
        canonical_after = source_path.read_bytes()
    finally:
        compiler.release_apply_compile.set()
        if thread is not None:
            thread.join(timeout=3)
        store.close()

    assert {
        "compile_was_blocked": compile_was_blocked,
        "thread_finished": thread_finished,
        "outcome": outcome,
        "authority_unchanged": after == before,
        "external_source_preserved": canonical_after == external_source,
    } == {
        "compile_was_blocked": True,
        "thread_finished": True,
        "outcome": {
            "error": {
                "code": "draft_hash_conflict",
                "status": 409,
            }
        },
        "authority_unchanged": True,
        "external_source_preserved": True,
    }


def test_apply_rechecks_catalog_after_blocking_compiler_before_transaction(
    tmp_path: Path,
) -> None:
    compiler = BlockingCompiler()
    store, service, source_path = _open_authoring(
        tmp_path,
        compiler=compiler,
    )
    thread: threading.Thread | None = None
    try:
        saved = _save_candidate(service)
        before = _authority_snapshot(store, service)
        source_before = source_path.read_bytes()
        thread, outcome = _start_apply(service, saved)
        compile_was_blocked = compiler.apply_compile_entered.wait(timeout=2)
        assert compile_was_blocked

        compiler.current_fingerprint = CATALOG_C2
        # 恶意/过时 compiler 仍返回 C1 bundle。
        assert compiler.result_fingerprint == CATALOG_C1
        thread_finished = _finish_apply(compiler, thread)
        thread = None
        after = _authority_snapshot(store, service)
        source_after = source_path.read_bytes()
    finally:
        compiler.release_apply_compile.set()
        if thread is not None:
            thread.join(timeout=3)
        store.close()

    assert {
        "compile_was_blocked": compile_was_blocked,
        "thread_finished": thread_finished,
        "outcome": outcome,
        "authority_unchanged": after == before,
        "source_unchanged": source_after == source_before,
    } == {
        "compile_was_blocked": True,
        "thread_finished": True,
        "outcome": {
            "error": {
                "code": "template_catalog_conflict",
                "status": 409,
            }
        },
        "authority_unchanged": True,
        "source_unchanged": True,
    }


@pytest.mark.parametrize(
    ("applied_value", "candidate_value"),
    [
        pytest.param(True, 1, id="true-vs-integer-one"),
        pytest.param(1, 1.0, id="integer-one-vs-float-one"),
    ],
)
def test_source_only_json_proof_is_type_strict(
    tmp_path: Path,
    applied_value: bool | int | float,
    candidate_value: bool | int | float,
) -> None:
    compiler = StrictProofCompiler(candidate_value)
    store, service, source_path = _open_authoring(
        tmp_path,
        compiler=compiler,
        proof_value=applied_value,
    )
    try:
        graph_before = service.get_graph(WORKFLOW_UUID)
        saved = service.save_draft(
            WORKFLOW_UUID,
            python_source=SOURCE,
            expected_draft_hash=None,
            expected_workflow_revision=2,
        )
        graph_after = service.get_graph(WORKFLOW_UUID)
    finally:
        store.close()

    assert {
        "state": saved["state"],
        "candidate": saved["candidate"],
        "diagnostics": saved["draft"]["diagnostics"],
        "graph_unchanged": graph_after == graph_before,
        "canonical": source_path.read_bytes(),
    } == {
        "state": "draft_invalid",
        "candidate": None,
        "diagnostics": [CANDIDATE_INVALID_DIAGNOSTIC],
        "graph_unchanged": True,
        "canonical": SOURCE.encode(),
    }


def test_identical_json_type_control_can_issue_candidate(
    tmp_path: Path,
) -> None:
    compiler = StrictProofCompiler(1)
    store, service, _ = _open_authoring(
        tmp_path,
        compiler=compiler,
        proof_value=1,
    )
    try:
        saved = _save_candidate(service)
    finally:
        store.close()

    assert {
        "state": saved["state"],
        "has_candidate": saved["candidate"] is not None,
        "diagnostics": saved["draft"]["diagnostics"],
    } == {
        "state": "unapplied_source_only",
        "has_candidate": True,
        "diagnostics": [],
    }


@pytest.mark.parametrize("factory", ["router", "app"])
def test_workflow_api_initialization_does_not_change_global_recursion_limit(
    tmp_path: Path,
    factory: str,
) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store)
    original_limit = sys.getrecursionlimit()
    test_limit = 1500
    try:
        sys.setrecursionlimit(test_limit)
        before = sys.getrecursionlimit()
        if factory == "router":
            create_workflow_router(service)
        else:
            create_workflow_app(service)
        after = sys.getrecursionlimit()
    finally:
        sys.setrecursionlimit(original_limit)
        store.close()

    assert {
        "before": before,
        "after": after,
        "restored": sys.getrecursionlimit(),
    } == {
        "before": test_limit,
        "after": test_limit,
        "restored": original_limit,
    }
