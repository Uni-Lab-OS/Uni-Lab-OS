"""Phase 01 第十四轮深层 JSON proof 与源码坐标 follow-up 合同测试。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow.models import WorkflowNodeWrite
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
NODE_UUID = "20000000-0000-4000-8000-000000000001"
NODE_TEMPLATE_UUID = "40000000-0000-4000-8000-000000000001"
RESOURCE_TEMPLATE_UUID = "50000000-0000-4000-8000-000000000001"
CATALOG_FINGERPRINT = f"sha256:{'b' * 64}"
DEEP_JSON_DEPTH = 1_300
SOURCE = "build()\n"

CANDIDATE_INVALID_DIAGNOSTIC = {
    "severity": "error",
    "code": "candidate_invalid",
    "message": "工作流校验失败，请检查节点、连线和输入输出",
}
_UNCHANGED = object()


def _nested_json(depth: int, leaf: Any) -> Any:
    value = leaf
    for level in range(depth):
        value = {"next": value} if level % 2 else [value]
    return value


def _source_range(
    *,
    start_line: int,
    start_column: int,
    end_line: int,
    end_column: int,
) -> dict[str, int]:
    return {
        "start_line": start_line,
        "start_column": start_column,
        "end_line": end_line,
        "end_column": end_column,
    }


def _source_map_entry(
    *,
    start_line: int,
    start_column: int,
    end_line: int,
    end_column: int,
) -> dict[str, Any]:
    return {
        "workflow_node_uuid": NODE_UUID,
        **_source_range(
            start_line=start_line,
            start_column=start_column,
            end_line=end_line,
            end_column=end_column,
        ),
    }


def _source_only_changeset() -> dict[str, Any]:
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


class FollowupCompiler:
    compiler_version = "phase-01-review-contract-round14-followup"
    template_catalog_fingerprint = CATALOG_FINGERPRINT

    def __init__(
        self,
        *,
        normalized_python_source: str = SOURCE,
        source_map: list[dict[str, Any]] | None = None,
        diagnostics: list[dict[str, Any]] | None = None,
        candidate_leaf: Any = _UNCHANGED,
    ) -> None:
        self.normalized_python_source = normalized_python_source
        self.source_map = (
            [
                _source_map_entry(
                    start_line=1,
                    start_column=1,
                    end_line=1,
                    end_column=8,
                )
            ]
            if source_map is None
            else source_map
        )
        self.diagnostics = [] if diagnostics is None else diagnostics
        self.candidate_leaf = candidate_leaf

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
        graph = applied_graph
        if self.candidate_leaf is not _UNCHANGED:
            applied_node = applied_graph["nodes"][0]
            graph = {
                **applied_graph,
                "nodes": [
                    {
                        **applied_node,
                        "param": {
                            "proof": _nested_json(
                                DEEP_JSON_DEPTH,
                                self.candidate_leaf,
                            )
                        },
                    }
                ],
            }
        return {
            "diagnostics": self.diagnostics,
            "graph": graph,
            "normalized_python_source": self.normalized_python_source,
            "source_map": self.source_map,
            "changeset": _source_only_changeset(),
            "compiler_version": self.compiler_version,
            "template_catalog_fingerprint": self.template_catalog_fingerprint,
        }


def _node(param: dict[str, Any]) -> WorkflowNodeWrite:
    return WorkflowNodeWrite(
        uuid=NODE_UUID,
        workflow_node_template_uuid=None,
        name="round 14 follow-up node",
        status="idle",
        type="compute",
        pose={},
        param=param,
        execution_policy={},
        disabled=False,
        minimized=False,
        meta_data={},
    )


def _open_authoring(
    tmp_path: Path,
    *,
    compiler: FollowupCompiler,
    applied_leaf: Any = 0,
    deep_workflow_fields: bool = False,
    deep_node_param: bool = False,
) -> tuple[WorkflowStore, WorkflowService]:
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store, compiler=compiler)
    workflow_meta_data = (
        {"deep": _nested_json(DEEP_JSON_DEPTH, "same")} if deep_workflow_fields else {}
    )
    workflow_tags = (
        [{"deep": _nested_json(DEEP_JSON_DEPTH, "same")}]
        if deep_workflow_fields
        else []
    )
    service.create_workflow(
        name="phase 01 review round 14 follow-up",
        tags=workflow_tags,
        description=None,
        meta_data=workflow_meta_data,
        workflow_uuid=WORKFLOW_UUID,
    )
    service.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=[
            _node(
                {
                    "proof": _nested_json(
                        DEEP_JSON_DEPTH,
                        applied_leaf,
                    )
                }
                if deep_node_param
                else {"proof": applied_leaf}
            )
        ],
        edges=[],
    )
    package_root = tmp_path / "package"
    package_root.mkdir()
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="phase_01_review_contract_round14_followup",
        package_root=package_root,
        relative_path="workflows/review.py",
    )
    return store, service


def _save_draft(
    service: WorkflowService,
    *,
    python_source: str = SOURCE,
) -> dict[str, Any]:
    return service.save_draft(
        WORKFLOW_UUID,
        python_source=python_source,
        expected_draft_hash=None,
        expected_workflow_revision=2,
    )


def _assert_candidate_invalid(aggregate: dict[str, Any]) -> None:
    assert {
        "state": aggregate["state"],
        "candidate": aggregate["candidate"],
        "diagnostics": aggregate["draft"]["diagnostics"],
    } == {
        "state": "draft_invalid",
        "candidate": None,
        "diagnostics": [CANDIDATE_INVALID_DIAGNOSTIC],
    }


def test_identical_legal_deep_json_can_complete_source_only_candidate_proof(
    tmp_path: Path,
) -> None:
    assert DEEP_JSON_DEPTH > sys.getrecursionlimit()
    store, service = _open_authoring(
        tmp_path,
        compiler=FollowupCompiler(),
        applied_leaf="same",
        deep_workflow_fields=True,
        deep_node_param=True,
    )
    try:
        aggregate = _save_draft(service)
    finally:
        store.close()

    assert {
        "state": aggregate["state"],
        "has_candidate": aggregate["candidate"] is not None,
        "changeset_kind": aggregate["candidate"]["changeset"]["kind"],
        "diagnostics": aggregate["draft"]["diagnostics"],
    } == {
        "state": "unapplied_source_only",
        "has_candidate": True,
        "changeset_kind": "source_only",
        "diagnostics": [],
    }


@pytest.mark.parametrize(
    ("applied_leaf", "candidate_leaf"),
    [
        pytest.param(True, 1, id="deep-bool-vs-integer"),
        pytest.param(1, 1.0, id="deep-integer-vs-number"),
    ],
)
def test_deep_source_only_candidate_proof_remains_type_strict(
    tmp_path: Path,
    applied_leaf: Any,
    candidate_leaf: Any,
) -> None:
    assert DEEP_JSON_DEPTH > sys.getrecursionlimit()
    store, service = _open_authoring(
        tmp_path,
        compiler=FollowupCompiler(candidate_leaf=candidate_leaf),
        applied_leaf=applied_leaf,
        deep_node_param=True,
    )
    try:
        aggregate = _save_draft(service)
    finally:
        store.close()

    _assert_candidate_invalid(aggregate)


def _seed_unique_items_template(store: WorkflowStore) -> None:
    timestamp = "2026-07-31T00:00:00Z"
    schema = (
        '{"type":"object","properties":{"values":{"type":"array","uniqueItems":true}}}'
    )
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO workflow_node_template(
                uuid, create_time, update_time, meta_data, authority_id,
                resource_template_uuid, name, display_name, class, goal,
                goal_default, feedback, result, schema, type, icon, header,
                footer, node_type
            ) VALUES (?, ?, ?, '{}', 'os-local', ?, 'deep_unique',
                      'Deep unique', NULL, '{}', '{}', '{}', '{}', ?,
                      'action', NULL, NULL, NULL, 'compute')
            """,
            (
                NODE_TEMPLATE_UUID,
                timestamp,
                timestamp,
                RESOURCE_TEMPLATE_UUID,
                schema,
            ),
        )


def test_public_graph_validation_accepts_distinct_legal_deep_unique_items(
    tmp_path: Path,
) -> None:
    assert DEEP_JSON_DEPTH > sys.getrecursionlimit()
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store)
    service.create_workflow(
        name="deep graph validation",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )
    _seed_unique_items_template(store)
    try:
        saved = service.save_graph(
            WORKFLOW_UUID,
            revision=1,
            nodes=[
                WorkflowNodeWrite(
                    uuid=NODE_UUID,
                    workflow_node_template_uuid=NODE_TEMPLATE_UUID,
                    name="deep unique node",
                    status="idle",
                    type="compute",
                    pose={},
                    param={
                        "values": [
                            _nested_json(DEEP_JSON_DEPTH, "left"),
                            _nested_json(DEEP_JSON_DEPTH, "right"),
                        ]
                    },
                    execution_policy={},
                    disabled=False,
                    minimized=False,
                    meta_data={},
                )
            ],
            edges=[],
        )
    finally:
        store.close()

    assert saved["workflow"]["revision"] == 2
    assert [item["uuid"] for item in saved["nodes"]] == [NODE_UUID]


def test_source_map_form_feed_does_not_create_a_second_python_line(
    tmp_path: Path,
) -> None:
    python_source = "x=1;\fy=2\n"
    compiler = FollowupCompiler(
        normalized_python_source=python_source,
        source_map=[
            _source_map_entry(
                start_line=2,
                start_column=1,
                end_line=2,
                end_column=4,
            )
        ],
    )
    store, service = _open_authoring(tmp_path, compiler=compiler)
    try:
        aggregate = _save_draft(service, python_source=python_source)
    finally:
        store.close()

    _assert_candidate_invalid(aggregate)


def test_diagnostic_form_feed_does_not_create_a_second_draft_line(
    tmp_path: Path,
) -> None:
    python_source = "x=1;\fy=2\n"
    diagnostic = {
        "severity": "warning",
        "code": "FORM_FEED_RANGE",
        "message": "换页符不是 Python 源码换行",
        "source_range": _source_range(
            start_line=2,
            start_column=1,
            end_line=2,
            end_column=4,
        ),
    }
    compiler = FollowupCompiler(
        normalized_python_source=python_source,
        source_map=[
            _source_map_entry(
                start_line=1,
                start_column=1,
                end_line=1,
                end_column=2,
            )
        ],
        diagnostics=[diagnostic],
    )
    store, service = _open_authoring(tmp_path, compiler=compiler)
    try:
        aggregate = _save_draft(service, python_source=python_source)
    finally:
        store.close()

    _assert_candidate_invalid(aggregate)


def test_source_map_accepts_eof_position_after_trailing_newline(
    tmp_path: Path,
) -> None:
    python_source = "x=1\n"
    compiler = FollowupCompiler(
        normalized_python_source=python_source,
        source_map=[
            _source_map_entry(
                start_line=2,
                start_column=1,
                end_line=2,
                end_column=1,
            )
        ],
    )
    store, service = _open_authoring(tmp_path, compiler=compiler)
    try:
        aggregate = _save_draft(service, python_source=python_source)
    finally:
        store.close()

    assert aggregate["state"] == "unapplied_source_only"
    assert aggregate["candidate"] is not None
    assert aggregate["candidate"]["source_map"] == compiler.source_map
    assert aggregate["draft"]["diagnostics"] == []


def test_source_map_uses_one_based_utf16_code_unit_columns(
    tmp_path: Path,
) -> None:
    python_source = "变量=1\n"
    compiler = FollowupCompiler(
        normalized_python_source=python_source,
        source_map=[
            _source_map_entry(
                start_line=1,
                start_column=1,
                end_line=1,
                end_column=5,
            )
        ],
    )
    store, service = _open_authoring(tmp_path, compiler=compiler)
    try:
        aggregate = _save_draft(service, python_source=python_source)
    finally:
        store.close()

    assert aggregate["state"] == "unapplied_source_only"
    assert aggregate["candidate"] is not None
    assert aggregate["candidate"]["source_map"] == compiler.source_map
    assert aggregate["draft"]["diagnostics"] == []
