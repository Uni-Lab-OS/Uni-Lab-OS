"""Phase 01 第十四轮 HTTP JSON 与 Authoring 诊断范围合同测试。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
NODE_UUID = "20000000-0000-4000-8000-000000000001"
CATALOG_FINGERPRINT = f"sha256:{'a' * 64}"

INVALID_INPUT = {
    "code": 400,
    "error": {
        "code": "invalid_input",
        "message": "提交内容格式不正确",
    },
}
CANDIDATE_INVALID_DIAGNOSTIC = {
    "severity": "error",
    "code": "candidate_invalid",
    "message": "工作流校验失败，请检查节点、连线和输入输出",
}
CANDIDATE_INVALID_ERROR = {
    "code": 422,
    "error": {
        "code": "candidate_invalid",
        "message": "工作流校验失败，请检查节点、连线和输入输出",
    },
}


def _range(
    *,
    line: int,
    start_column: int = 1,
    end_column: int = 2,
) -> dict[str, int]:
    return {
        "start_line": line,
        "start_column": start_column,
        "end_line": line,
        "end_column": end_column,
    }


class Round14Compiler:
    compiler_version = "phase-01-review-round-14"
    template_catalog_fingerprint = CATALOG_FINGERPRINT

    def __init__(
        self,
        *,
        normalized_python_source: str,
        diagnostics: list[dict[str, Any]] | None = None,
        source_map: list[dict[str, Any]] | None = None,
        revalidation_diagnostics: list[dict[str, Any]] | None = None,
    ) -> None:
        self.normalized_python_source = normalized_python_source
        self.diagnostics = [] if diagnostics is None else diagnostics
        self.source_map = [] if source_map is None else source_map
        self.revalidation_diagnostics = revalidation_diagnostics
        self.compile_count = 0

    def compile(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        python_source: str,
        source_uri: str,
        applied_graph: dict[str, Any],
    ) -> dict[str, Any]:
        del workflow_uuid, workflow_revision, python_source, source_uri
        self.compile_count += 1
        diagnostics = (
            self.revalidation_diagnostics
            if self.compile_count > 1 and self.revalidation_diagnostics is not None
            else self.diagnostics
        )
        return {
            "diagnostics": deepcopy(diagnostics),
            "graph": {
                "workflow": deepcopy(applied_graph["workflow"]),
                "nodes": [
                    {
                        "uuid": NODE_UUID,
                        "workflow_node_template_uuid": None,
                        "name": "round 14 candidate node",
                        "status": "idle",
                        "type": "compute",
                        "pose": {},
                        "param": {},
                        "execution_policy": {},
                        "disabled": False,
                        "minimized": False,
                        "meta_data": {},
                    }
                ],
                "edges": [],
                "node_templates": [],
                "handle_templates": [],
            },
            "normalized_python_source": self.normalized_python_source,
            "source_map": deepcopy(self.source_map),
            "changeset": {
                "kind": "graph",
                "created_node_uuids": [NODE_UUID],
                "updated_node_uuids": [],
                "deleted_node_uuids": [],
                "created_edge_uuids": [],
                "updated_edge_uuids": [],
                "deleted_edge_uuids": [],
                "reserved_metadata_changed": False,
            },
            "compiler_version": self.compiler_version,
            "template_catalog_fingerprint": self.template_catalog_fingerprint,
        }


@contextmanager
def _authoring_client(
    tmp_path: Path,
    compiler: Round14Compiler,
) -> Iterator[TestClient]:
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store, compiler=compiler)
    service.create_workflow(
        name="phase 01 review round 14",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )
    package_root = tmp_path / "package"
    package_root.mkdir()
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="phase_01_review_round_14",
        package_root=package_root,
        relative_path="workflows/review.py",
    )
    try:
        with TestClient(create_workflow_app(service)) as client:
            yield client
    finally:
        store.close()


def _save_draft(client: TestClient, python_source: str) -> Any:
    return client.put(
        f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/draft",
        json={
            "python_source": python_source,
            "expected_draft_hash": None,
            "expected_workflow_revision": 1,
        },
    )


@pytest.mark.parametrize(
    "non_finite",
    ["NaN", "Infinity", "-Infinity"],
    ids=["nan", "positive-infinity", "negative-infinity"],
)
def test_raw_http_rejects_non_finite_number_in_ignored_field_without_side_effects(
    tmp_path: Path,
    non_finite: str,
) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    try:
        with TestClient(create_workflow_app(WorkflowService(store))) as client:
            response = client.post(
                "/api/v1/workflows",
                content=(
                    '{"name":"must not persist","tags":[],"meta_data":{},'
                    f'"future_extension":{{"nested":[{non_finite}]}}}}'
                ).encode(),
                headers={"content-type": "application/json"},
            )
            listed = client.get("/api/v1/workflows").json()["data"]
    finally:
        store.close()

    assert {
        "status": response.status_code,
        "body": response.json(),
        "items": listed["items"],
        "total": listed["total"],
    } == {
        "status": 400,
        "body": INVALID_INPUT,
        "items": [],
        "total": 0,
    }


@pytest.mark.parametrize(
    ("draft_source", "normalized_source", "source_range"),
    [
        (
            "draft line one\ndraft line two\ndraft line three\n",
            "normalized line one\n",
            _range(line=3),
        ),
        (
            "this draft line is deliberately long\n",
            "x\n",
            _range(line=1, start_column=10, end_column=20),
        ),
    ],
    ids=["line-outside-normalized-source", "column-outside-normalized-source"],
)
def test_source_map_range_must_fit_normalized_python_source(
    tmp_path: Path,
    draft_source: str,
    normalized_source: str,
    source_range: dict[str, int],
) -> None:
    compiler = Round14Compiler(
        normalized_python_source=normalized_source,
        source_map=[
            {
                "workflow_node_uuid": NODE_UUID,
                **source_range,
            }
        ],
    )

    with _authoring_client(tmp_path, compiler) as client:
        response = _save_draft(client, draft_source)

    aggregate = response.json()["data"]
    assert {
        "status": response.status_code,
        "state": aggregate["state"],
        "candidate": aggregate["candidate"],
        "diagnostics": aggregate["draft"]["diagnostics"],
    } == {
        "status": 200,
        "state": "draft_invalid",
        "candidate": None,
        "diagnostics": [CANDIDATE_INVALID_DIAGNOSTIC],
    }


@pytest.mark.parametrize(
    ("draft_source", "normalized_source", "diagnostic_range"),
    [
        (
            "draft line one\n",
            "normalized line one\nnormalized line two\nnormalized line three\n",
            _range(line=3),
        ),
        (
            "x\n",
            "this normalized line is deliberately long\n",
            _range(line=1, start_column=10, end_column=20),
        ),
    ],
    ids=["line-outside-draft-source", "column-outside-draft-source"],
)
def test_diagnostic_range_must_fit_exact_draft_source(
    tmp_path: Path,
    draft_source: str,
    normalized_source: str,
    diagnostic_range: dict[str, int],
) -> None:
    compiler = Round14Compiler(
        normalized_python_source=normalized_source,
        diagnostics=[
            {
                "severity": "warning",
                "code": "OUT_OF_RANGE",
                "message": "此范围只在规范化源码中存在",
                "source_range": diagnostic_range,
            }
        ],
    )

    with _authoring_client(tmp_path, compiler) as client:
        response = _save_draft(client, draft_source)

    aggregate = response.json()["data"]
    assert {
        "status": response.status_code,
        "state": aggregate["state"],
        "candidate": aggregate["candidate"],
        "diagnostics": aggregate["draft"]["diagnostics"],
    } == {
        "status": 200,
        "state": "draft_invalid",
        "candidate": None,
        "diagnostics": [CANDIDATE_INVALID_DIAGNOSTIC],
    }


def test_apply_revalidation_rejects_diagnostic_range_outside_saved_draft(
    tmp_path: Path,
) -> None:
    draft_source = "first line\nsecond line\n"
    valid_diagnostic = {
        "severity": "warning",
        "code": "VALID_RANGE",
        "message": "合法范围必须能在 Draft DTO 中持久化",
        "source_range": _range(line=2, start_column=1, end_column=6),
    }
    invalid_revalidation = {
        **valid_diagnostic,
        "code": "INVALID_REVALIDATION_RANGE",
        "source_range": _range(line=4),
    }
    compiler = Round14Compiler(
        normalized_python_source=draft_source,
        diagnostics=[valid_diagnostic],
        revalidation_diagnostics=[invalid_revalidation],
    )

    with _authoring_client(tmp_path, compiler) as client:
        draft_response = _save_draft(client, draft_source)
        aggregate = draft_response.json()["data"]
        apply_response = client.post(
            f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/apply",
            json={
                "expected_draft_hash": aggregate["draft"]["draft_hash"],
                "expected_workflow_revision": 1,
                "expected_candidate_hash": aggregate["candidate"]["candidate_hash"],
            },
        )
        graph = client.get(f"/api/v1/workflows/{WORKFLOW_UUID}/graph").json()["data"]

    assert aggregate["draft"]["diagnostics"] == [valid_diagnostic]
    assert {
        "status": apply_response.status_code,
        "body": apply_response.json(),
        "workflow_revision": graph["workflow"]["revision"],
        "nodes": graph["nodes"],
    } == {
        "status": 422,
        "body": CANDIDATE_INVALID_ERROR,
        "workflow_revision": 1,
        "nodes": [],
    }


def test_error_severity_surrounded_by_whitespace_still_blocks_candidate(
    tmp_path: Path,
) -> None:
    compiler = Round14Compiler(
        normalized_python_source="build()\n",
        diagnostics=[
            {
                "severity": " error ",
                "code": "WHITESPACE_ERROR",
                "message": "错误级别两侧的空白不能绕过阻断",
            }
        ],
    )

    with _authoring_client(tmp_path, compiler) as client:
        response = _save_draft(client, "build()\n")

    aggregate = response.json()["data"]
    assert response.status_code == 200
    assert aggregate["state"] == "draft_invalid"
    assert aggregate["candidate"] is None
    assert aggregate["draft"]["diagnostics"][0]["code"] == "WHITESPACE_ERROR"
