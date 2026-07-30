"""Phase 01 第十三轮 Candidate 语义与 HTTP 边界合同测试。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.models import WorkflowEdgeWrite, WorkflowNodeWrite
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
NODE_A_UUID = "20000000-0000-4000-8000-000000000001"
NODE_B_UUID = "20000000-0000-4000-8000-000000000002"
NODE_C_UUID = "20000000-0000-4000-8000-000000000003"
UNKNOWN_NODE_UUID = "20000000-0000-4000-8000-000000000099"
UPDATED_EDGE_UUID = "30000000-0000-4000-8000-000000000001"
DELETED_EDGE_UUID = "30000000-0000-4000-8000-000000000002"
CREATED_EDGE_UUID = "30000000-0000-4000-8000-000000000003"
UNKNOWN_EDGE_UUID = "30000000-0000-4000-8000-000000000099"
NODE_TEMPLATE_UUID = "40000000-0000-4000-8000-000000000001"
RESOURCE_TEMPLATE_UUID = "50000000-0000-4000-8000-000000000001"
SOURCE_HANDLE_UUID = "60000000-0000-4000-8000-000000000001"
TARGET_HANDLE_UUID = "60000000-0000-4000-8000-000000000002"
SECOND_TARGET_HANDLE_UUID = "60000000-0000-4000-8000-000000000003"
UNKNOWN_HANDLE_UUID = "60000000-0000-4000-8000-000000000099"
CATALOG_FINGERPRINT = f"sha256:{'d' * 64}"

INVALID_INPUT = {
    "code": 400,
    "error": {
        "code": "invalid_input",
        "message": "提交内容格式不正确",
    },
}
CANDIDATE_INVALID_ERROR = {
    "code": 422,
    "error": {
        "code": "candidate_invalid",
        "message": "工作流校验失败，请检查节点、连线和输入输出",
    },
}
CANDIDATE_INVALID_DIAGNOSTIC = {
    "severity": "error",
    "code": "candidate_invalid",
    "message": "工作流校验失败，请检查节点、连线和输入输出",
}


class Round13Compiler:
    """Produce exact valid controls and one deliberately invalid bundle variant."""

    compiler_version = "phase-01-review-round-13"
    template_catalog_fingerprint = CATALOG_FINGERPRINT

    def __init__(
        self,
        *,
        candidate_case: str = "exact-graph",
        diagnostics: Any = None,
        revalidation_case: str | None = None,
        revalidation_diagnostics: Any = None,
    ) -> None:
        self.candidate_case = candidate_case
        self.diagnostics = [] if diagnostics is None else diagnostics
        self.revalidation_case = revalidation_case
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
        del workflow_uuid, workflow_revision, source_uri
        self.compile_count += 1
        case = (
            self.revalidation_case
            if self.compile_count > 1 and self.revalidation_case is not None
            else self.candidate_case
        )
        diagnostics = (
            self.revalidation_diagnostics
            if self.compile_count > 1 and self.revalidation_diagnostics is not None
            else self.diagnostics
        )
        graph, changeset, source_map = self._bundle(
            case=case,
            applied_graph=applied_graph,
        )
        return {
            "diagnostics": deepcopy(diagnostics),
            "graph": graph,
            "normalized_python_source": python_source,
            "source_map": source_map,
            "changeset": changeset,
            "compiler_version": self.compiler_version,
            "template_catalog_fingerprint": self.template_catalog_fingerprint,
        }

    @classmethod
    def _bundle(
        cls,
        *,
        case: str,
        applied_graph: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        if case == "exact-source-only" or case.startswith("source-only-"):
            graph = deepcopy(applied_graph)
            changeset = cls._source_only_changeset()
        else:
            graph = cls._changed_graph(applied_graph)
            changeset = cls._graph_changeset()
        source_map = [
            {
                "workflow_node_uuid": NODE_A_UUID,
                "start_line": 1,
                "start_column": 1,
                "end_line": 1,
                "end_column": 8,
            }
        ]

        if case == "source-only-workflow-name-changed":
            graph["workflow"]["name"] = "not semantically source-only"
        elif case == "source-only-node-changed":
            graph["nodes"][0]["name"] = "not semantically source-only"
        elif case == "source-only-edge-deleted":
            graph["edges"].pop()
        elif case == "source-only-catalog-changed":
            graph["node_templates"][0]["display_name"] = "Different"
        elif case == "source-only-reserved-control-changed":
            graph["workflow"]["meta_data"]["unilab"]["version"] = 2
        elif case.startswith("missing-"):
            changeset[case.removeprefix("missing-")] = []
        elif case == "duplicate-node-uuid-in-changeset":
            changeset["created_node_uuids"] = [NODE_C_UUID, NODE_C_UUID]
        elif case == "duplicate-edge-uuid-in-changeset":
            changeset["created_edge_uuids"] = [
                CREATED_EDGE_UUID,
                CREATED_EDGE_UUID,
            ]
        elif case == "node-uuid-in-mutually-exclusive-lists":
            changeset["updated_node_uuids"].append(NODE_C_UUID)
        elif case == "edge-uuid-in-mutually-exclusive-lists":
            changeset["updated_edge_uuids"].append(CREATED_EDGE_UUID)
        elif case == "unknown-node-uuid-in-changeset":
            changeset["updated_node_uuids"] = [UNKNOWN_NODE_UUID]
        elif case == "unknown-edge-uuid-in-changeset":
            changeset["updated_edge_uuids"] = [UNKNOWN_EDGE_UUID]
        elif case == "reserved-flag-true-without-change":
            changeset["reserved_metadata_changed"] = True
        elif case == "reserved-flag-false-with-change":
            graph["workflow"]["meta_data"]["unilab"]["version"] = 2
        elif case == "source-map-node-not-in-candidate":
            source_map[0]["workflow_node_uuid"] = NODE_B_UUID
        elif case == "self-loop":
            graph["edges"][0]["target_node_uuid"] = NODE_A_UUID
        elif case == "two-node-cycle":
            graph["edges"][1]["source_node_uuid"] = NODE_C_UUID
            graph["edges"][1]["target_node_uuid"] = NODE_A_UUID
        elif case == "duplicate-target-provider":
            graph["edges"][1]["target_handle_uuid"] = TARGET_HANDLE_UUID
        elif case == "edge-handle-direction-invalid":
            graph["edges"][0]["source_handle_uuid"] = TARGET_HANDLE_UUID
        elif case == "edge-node-not-in-candidate":
            graph["edges"][0]["target_node_uuid"] = UNKNOWN_NODE_UUID
        elif case == "edge-handle-not-in-catalog":
            graph["edges"][0]["target_handle_uuid"] = UNKNOWN_HANDLE_UUID
        elif case == "duplicate-candidate-node-uuid":
            graph["nodes"].append(deepcopy(graph["nodes"][0]))

        return graph, changeset, source_map

    @staticmethod
    def _changed_graph(applied_graph: dict[str, Any]) -> dict[str, Any]:
        applied_by_node = {
            node["uuid"]: deepcopy(node) for node in applied_graph["nodes"]
        }
        applied_by_edge = {
            edge["uuid"]: deepcopy(edge) for edge in applied_graph["edges"]
        }
        updated_node = applied_by_node[NODE_A_UUID]
        updated_node["name"] = "updated candidate node A"
        created_node = deepcopy(applied_by_node[NODE_B_UUID])
        created_node["uuid"] = NODE_C_UUID
        created_node["name"] = "created candidate node C"

        updated_edge = applied_by_edge[UPDATED_EDGE_UUID]
        updated_edge["target_node_uuid"] = NODE_C_UUID
        created_edge = deepcopy(applied_by_edge[DELETED_EDGE_UUID])
        created_edge["uuid"] = CREATED_EDGE_UUID
        created_edge["target_node_uuid"] = NODE_C_UUID
        created_edge["meta_data"] = {"candidate": "created"}
        return {
            "workflow": deepcopy(applied_graph["workflow"]),
            "nodes": [updated_node, created_node],
            "edges": [updated_edge, created_edge],
            "node_templates": deepcopy(applied_graph["node_templates"]),
            "handle_templates": deepcopy(applied_graph["handle_templates"]),
        }

    @staticmethod
    def _graph_changeset() -> dict[str, Any]:
        return {
            "kind": "graph",
            "created_node_uuids": [NODE_C_UUID],
            "updated_node_uuids": [NODE_A_UUID],
            "deleted_node_uuids": [NODE_B_UUID],
            "created_edge_uuids": [CREATED_EDGE_UUID],
            "updated_edge_uuids": [UPDATED_EDGE_UUID],
            "deleted_edge_uuids": [DELETED_EDGE_UUID],
            "reserved_metadata_changed": False,
        }

    @staticmethod
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


@pytest.fixture()
def store(tmp_path: Path):
    opened = WorkflowStore(tmp_path / "workflow.db")
    try:
        yield opened
    finally:
        opened.close()


def _node(node_uuid: str, name: str) -> WorkflowNodeWrite:
    return WorkflowNodeWrite(
        uuid=node_uuid,
        workflow_node_template_uuid=NODE_TEMPLATE_UUID,
        name=name,
        status="idle",
        type="compute",
        pose={},
        param={},
        execution_policy={},
        disabled=False,
        minimized=False,
        meta_data={},
    )


def _edge(
    edge_uuid: str,
    *,
    target_handle_uuid: str,
    label: str,
) -> WorkflowEdgeWrite:
    return WorkflowEdgeWrite(
        uuid=edge_uuid,
        source_node_uuid=NODE_A_UUID,
        target_node_uuid=NODE_B_UUID,
        source_handle_uuid=SOURCE_HANDLE_UUID,
        target_handle_uuid=target_handle_uuid,
        meta_data={"applied": label},
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
            (
                SECOND_TARGET_HANDLE_UUID,
                "second_input",
                "target",
                "Second input",
            ),
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


def _authoring_service(
    store: WorkflowStore,
    tmp_path: Path,
    *,
    compiler: Round13Compiler,
) -> tuple[WorkflowService, int]:
    service = WorkflowService(store, compiler=compiler)
    store.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="phase 01 review round 13",
        tags=[],
        description=None,
        meta_data={"unilab": {"version": 1}, "public": "stable"},
    )
    _seed_template_catalog(store)
    service.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=[
            _node(NODE_A_UUID, "applied node A"),
            _node(NODE_B_UUID, "applied node B"),
        ],
        edges=[
            _edge(
                UPDATED_EDGE_UUID,
                target_handle_uuid=TARGET_HANDLE_UUID,
                label="will update",
            ),
            _edge(
                DELETED_EDGE_UUID,
                target_handle_uuid=SECOND_TARGET_HANDLE_UUID,
                label="will delete",
            ),
        ],
    )
    package_root = tmp_path / "package"
    package_root.mkdir()
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="phase_01_review_round_13",
        package_root=package_root,
        relative_path="workflows/review.py",
    )
    return service, 2


def _save_draft(client: TestClient, *, revision: int) -> Any:
    return client.put(
        f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/draft",
        json={
            "python_source": "build()\n",
            "expected_draft_hash": None,
            "expected_workflow_revision": revision,
        },
    )


def _apply(client: TestClient, aggregate: dict[str, Any]) -> Any:
    return client.post(
        f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/apply",
        json={
            "expected_draft_hash": aggregate["draft"]["draft_hash"],
            "expected_workflow_revision": aggregate["workflow_revision"],
            "expected_candidate_hash": aggregate["candidate"]["candidate_hash"],
        },
    )


def _assert_saved_candidate_invalid(response: Any) -> None:
    payload = response.json()
    aggregate = payload["data"]
    assert {
        "status": response.status_code,
        "envelope_code": payload["code"],
        "state": aggregate["state"],
        "candidate": aggregate["candidate"],
        "saved_source": aggregate["draft"]["python_source"],
        "has_draft_hash": bool(aggregate["draft"]["draft_hash"]),
        "diagnostics": aggregate["draft"]["diagnostics"],
    } == {
        "status": 200,
        "envelope_code": 0,
        "state": "draft_invalid",
        "candidate": None,
        "saved_source": "build()\n",
        "has_draft_hash": True,
        "diagnostics": [CANDIDATE_INVALID_DIAGNOSTIC],
    }


def _raw_json_request(
    client: TestClient,
    method: str,
    path: str,
    body: str,
) -> Any:
    return client.request(
        method,
        path,
        content=body.encode("utf-8"),
        headers={"content-type": "application/json"},
    )


def _json_payload(response: Any) -> dict[str, Any] | None:
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()
    return None


INVALID_CROSS_FIELD_CASES = [
    pytest.param(
        "source-only-workflow-name-changed",
        id="source-only-requires-complete-workflow-equivalence",
    ),
    pytest.param(
        "source-only-node-changed",
        id="source-only-requires-complete-node-equivalence",
    ),
    pytest.param(
        "source-only-edge-deleted",
        id="source-only-requires-complete-edge-equivalence",
    ),
    pytest.param(
        "source-only-catalog-changed",
        id="source-only-requires-complete-catalog-equivalence",
    ),
    pytest.param(
        "source-only-reserved-control-changed",
        id="source-only-requires-complete-control-equivalence",
    ),
    *[
        pytest.param(
            f"missing-{field}",
            id=f"changeset-exact-{field.replace('_', '-')}",
        )
        for field in (
            "created_node_uuids",
            "updated_node_uuids",
            "deleted_node_uuids",
            "created_edge_uuids",
            "updated_edge_uuids",
            "deleted_edge_uuids",
        )
    ],
    pytest.param(
        "duplicate-node-uuid-in-changeset",
        id="changeset-rejects-duplicate-node-uuid",
    ),
    pytest.param(
        "duplicate-edge-uuid-in-changeset",
        id="changeset-rejects-duplicate-edge-uuid",
    ),
    pytest.param(
        "node-uuid-in-mutually-exclusive-lists",
        id="changeset-rejects-node-list-overlap",
    ),
    pytest.param(
        "edge-uuid-in-mutually-exclusive-lists",
        id="changeset-rejects-edge-list-overlap",
    ),
    pytest.param(
        "unknown-node-uuid-in-changeset",
        id="changeset-rejects-unknown-node-uuid",
    ),
    pytest.param(
        "unknown-edge-uuid-in-changeset",
        id="changeset-rejects-unknown-edge-uuid",
    ),
    pytest.param(
        "reserved-flag-true-without-change",
        id="reserved-metadata-flag-must-not-overreport",
    ),
    pytest.param(
        "reserved-flag-false-with-change",
        id="reserved-metadata-flag-must-not-underreport",
    ),
    pytest.param(
        "source-map-node-not-in-candidate",
        id="source-map-node-must-exist-in-candidate",
    ),
    pytest.param("self-loop", id="candidate-rejects-self-loop"),
    pytest.param("two-node-cycle", id="candidate-rejects-two-node-cycle"),
    pytest.param(
        "duplicate-target-provider",
        id="candidate-rejects-duplicate-target-provider",
    ),
    pytest.param(
        "edge-handle-direction-invalid",
        id="candidate-rejects-wrong-handle-direction",
    ),
    pytest.param(
        "edge-node-not-in-candidate",
        id="candidate-rejects-edge-node-outside-graph",
    ),
    pytest.param(
        "edge-handle-not-in-catalog",
        id="candidate-rejects-edge-handle-outside-catalog",
    ),
    pytest.param(
        "duplicate-candidate-node-uuid",
        id="candidate-rejects-duplicate-node-uuid",
    ),
]


@pytest.mark.parametrize("candidate_case", INVALID_CROSS_FIELD_CASES)
def test_candidate_bundle_cross_field_invariant_is_checked_before_signing(
    store: WorkflowStore,
    tmp_path: Path,
    candidate_case: str,
) -> None:
    service, revision = _authoring_service(
        store,
        tmp_path,
        compiler=Round13Compiler(candidate_case=candidate_case),
    )

    with TestClient(
        create_workflow_app(service),
        raise_server_exceptions=False,
    ) as client:
        response = _save_draft(client, revision=revision)

    _assert_saved_candidate_invalid(response)


@pytest.mark.parametrize(
    ("candidate_case", "expected_state"),
    [
        ("exact-graph", "unapplied_graph"),
        ("exact-source-only", "unapplied_source_only"),
    ],
    ids=["exact-six-way-graph-diff", "complete-source-only-equivalence"],
)
def test_candidate_bundle_exact_semantic_controls_are_signed(
    store: WorkflowStore,
    tmp_path: Path,
    candidate_case: str,
    expected_state: str,
) -> None:
    service, revision = _authoring_service(
        store,
        tmp_path,
        compiler=Round13Compiler(candidate_case=candidate_case),
    )

    with TestClient(create_workflow_app(service)) as client:
        response = _save_draft(client, revision=revision)

    payload = response.json()
    candidate = payload["data"]["candidate"]
    assert {
        "status": response.status_code,
        "state": payload["data"]["state"],
        "has_candidate_hash": bool(candidate["candidate_hash"]),
        "changeset": candidate["changeset"],
    } == {
        "status": 200,
        "state": expected_state,
        "has_candidate_hash": True,
        "changeset": (
            Round13Compiler._graph_changeset()
            if candidate_case == "exact-graph"
            else Round13Compiler._source_only_changeset()
        ),
    }


@pytest.mark.parametrize(
    "revalidation_case",
    [
        pytest.param(
            "missing-created_node_uuids",
            id="apply-rechecks-exact-changeset",
        ),
        pytest.param(
            "source-only-node-changed",
            id="apply-rechecks-source-only-equivalence",
        ),
        pytest.param("self-loop", id="apply-rechecks-topology"),
        pytest.param(
            "source-map-node-not-in-candidate",
            id="apply-rechecks-source-map-membership",
        ),
    ],
)
def test_apply_recompilation_rechecks_candidate_cross_field_invariants(
    store: WorkflowStore,
    tmp_path: Path,
    revalidation_case: str,
) -> None:
    service, revision = _authoring_service(
        store,
        tmp_path,
        compiler=Round13Compiler(revalidation_case=revalidation_case),
    )

    with TestClient(
        create_workflow_app(service),
        raise_server_exceptions=False,
    ) as client:
        draft = _save_draft(client, revision=revision)
        response = _apply(client, draft.json()["data"])
        after = client.get(f"/api/v1/workflows/{WORKFLOW_UUID}")

    assert {
        "status": response.status_code,
        "body": _json_payload(response),
        "revision_after": after.json()["data"]["revision"],
    } == {
        "status": 422,
        "body": CANDIDATE_INVALID_ERROR,
        "revision_after": revision,
    }


MALFORMED_DIAGNOSTICS = [
    pytest.param({}, id="diagnostics-not-array"),
    pytest.param(["not-an-object"], id="diagnostic-not-object"),
    pytest.param(
        {"code": "compiler_error", "message": "bad"},
        id="severity-missing",
    ),
    pytest.param(
        {"severity": "error", "message": "bad"},
        id="code-missing",
    ),
    pytest.param(
        {"severity": "error", "code": "compiler_error"},
        id="message-missing",
    ),
    pytest.param(
        {"severity": "", "code": "compiler_error", "message": "bad"},
        id="severity-empty",
    ),
    pytest.param(
        {"severity": "error", "code": "", "message": "bad"},
        id="code-empty",
    ),
    pytest.param(
        {"severity": "error", "code": "compiler_error", "message": ""},
        id="message-empty",
    ),
    pytest.param(
        {"severity": " \t", "code": "compiler_error", "message": "bad"},
        id="severity-blank",
    ),
    pytest.param(
        {"severity": "error", "code": " \t", "message": "bad"},
        id="code-blank",
    ),
    pytest.param(
        {"severity": "error", "code": "compiler_error", "message": " \t"},
        id="message-blank",
    ),
    pytest.param(
        {"severity": 1, "code": "compiler_error", "message": "bad"},
        id="severity-not-string",
    ),
    pytest.param(
        {"severity": "error", "code": 1, "message": "bad"},
        id="code-not-string",
    ),
    pytest.param(
        {"severity": "error", "code": "compiler_error", "message": 1},
        id="message-not-string",
    ),
    pytest.param(
        {
            "severity": "error",
            "code": "compiler_error",
            "message": "bad",
            "frontend_only": True,
        },
        id="diagnostic-extra-field",
    ),
]


@pytest.mark.parametrize("diagnostics", MALFORMED_DIAGNOSTICS)
def test_draft_malformed_diagnostics_use_stable_candidate_invalid_aggregate(
    store: WorkflowStore,
    tmp_path: Path,
    diagnostics: Any,
) -> None:
    diagnostic_payload = diagnostics if isinstance(diagnostics, list) else [diagnostics]
    if diagnostics == {}:
        diagnostic_payload = {}
    service, revision = _authoring_service(
        store,
        tmp_path,
        compiler=Round13Compiler(diagnostics=diagnostic_payload),
    )

    with TestClient(
        create_workflow_app(service),
        raise_server_exceptions=False,
    ) as client:
        response = _save_draft(client, revision=revision)

    _assert_saved_candidate_invalid(response)


@pytest.mark.parametrize(
    "severity",
    ["error", "ERROR", "Error", "eRrOr"],
)
def test_structured_error_diagnostic_blocks_candidate_case_insensitively(
    store: WorkflowStore,
    tmp_path: Path,
    severity: str,
) -> None:
    diagnostic = {
        "severity": severity,
        "code": "compiler_error",
        "message": "compiler rejected the draft",
    }
    service, revision = _authoring_service(
        store,
        tmp_path,
        compiler=Round13Compiler(diagnostics=[diagnostic]),
    )

    with TestClient(create_workflow_app(service)) as client:
        response = _save_draft(client, revision=revision)

    payload = response.json()["data"]
    assert {
        "status": response.status_code,
        "state": payload["state"],
        "candidate": payload["candidate"],
        "diagnostics": payload["draft"]["diagnostics"],
    } == {
        "status": 200,
        "state": "draft_invalid",
        "candidate": None,
        "diagnostics": [diagnostic],
    }


def test_structured_warning_diagnostic_keeps_valid_candidate(
    store: WorkflowStore,
    tmp_path: Path,
) -> None:
    diagnostic = {
        "severity": "warning",
        "code": "compiler_warning",
        "message": "compiler accepted with a warning",
    }
    service, revision = _authoring_service(
        store,
        tmp_path,
        compiler=Round13Compiler(diagnostics=[diagnostic]),
    )

    with TestClient(create_workflow_app(service)) as client:
        response = _save_draft(client, revision=revision)

    payload = response.json()["data"]
    assert {
        "status": response.status_code,
        "state": payload["state"],
        "has_candidate": payload["candidate"] is not None,
        "diagnostics": payload["draft"]["diagnostics"],
    } == {
        "status": 200,
        "state": "unapplied_graph",
        "has_candidate": True,
        "diagnostics": [diagnostic],
    }


@pytest.mark.parametrize(
    "revalidation_diagnostics",
    [
        pytest.param({}, id="apply-diagnostics-not-array"),
        pytest.param(
            [{"severity": "error", "code": "compiler_error"}],
            id="apply-diagnostic-missing-message",
        ),
        pytest.param(
            [
                {
                    "severity": "error",
                    "code": "compiler_error",
                    "message": "bad",
                    "frontend_only": True,
                }
            ],
            id="apply-diagnostic-extra-field",
        ),
    ],
)
def test_apply_malformed_diagnostics_use_stable_candidate_invalid_error(
    store: WorkflowStore,
    tmp_path: Path,
    revalidation_diagnostics: Any,
) -> None:
    service, revision = _authoring_service(
        store,
        tmp_path,
        compiler=Round13Compiler(
            revalidation_diagnostics=revalidation_diagnostics,
        ),
    )

    with TestClient(
        create_workflow_app(service),
        raise_server_exceptions=False,
    ) as client:
        draft = _save_draft(client, revision=revision)
        response = _apply(client, draft.json()["data"])
        after = client.get(f"/api/v1/workflows/{WORKFLOW_UUID}")

    assert {
        "status": response.status_code,
        "body": _json_payload(response),
        "revision_after": after.json()["data"]["revision"],
    } == {
        "status": 422,
        "body": CANDIDATE_INVALID_ERROR,
        "revision_after": revision,
    }


def test_raw_http_depth_1100_legal_json_mirrors_frozen_backend(
    store: WorkflowStore,
) -> None:
    service = WorkflowService(store)
    nested = '{"level":' * 1100 + "0" + "}" * 1100
    body = (
        '{"name":"deep valid","tags":[],"description":null,"meta_data":' + nested + "}"
    )

    with TestClient(
        create_workflow_app(service),
        raise_server_exceptions=False,
    ) as client:
        response = _raw_json_request(client, "POST", "/api/v1/workflows", body)
        lookup = client.get(
            "/api/v1/workflows",
            params={"name": "deep valid"},
        )

    assert response.status_code == 201
    assert '"code":0' in response.text
    assert '"name":"deep valid"' in response.text
    assert '"detail"' not in response.text
    assert lookup.status_code == 200
    assert '"name":"deep valid"' in lookup.text
    assert '"detail"' not in lookup.text


def test_raw_http_truncated_deep_json_is_invalid_input_with_zero_side_effects(
    store: WorkflowStore,
) -> None:
    service = WorkflowService(store)
    nested = '{"level":' * 20 + "0" + "}" * 19
    body = (
        '{"name":"truncated deep","tags":[],"description":null,"meta_data":'
        + nested
        + "}"
    )

    with TestClient(
        create_workflow_app(service),
        raise_server_exceptions=False,
    ) as client:
        response = _raw_json_request(client, "POST", "/api/v1/workflows", body)
        lookup = client.get(
            "/api/v1/workflows",
            params={"name": "truncated deep"},
        )

    assert {
        "status": response.status_code,
        "body": response.json(),
        "detail_leaked": '"detail"' in response.text,
        "lookup_status": lookup.status_code,
        "lookup_items": lookup.json()["data"]["items"],
    } == {
        "status": 400,
        "body": INVALID_INPUT,
        "detail_leaked": False,
        "lookup_status": 200,
        "lookup_items": [],
    }
