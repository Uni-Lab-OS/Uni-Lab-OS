"""Phase 01 第十三轮 Candidate 语义与 raw JSON 风险测试。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.models import WorkflowNodeWrite
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
NODE_UUID = "20000000-0000-4000-8000-000000000001"
CREATED_NODE_UUID = "20000000-0000-4000-8000-000000000002"
EDGE_UUID = "30000000-0000-4000-8000-000000000001"
DANGLING_NODE_UUID = "20000000-0000-4000-8000-000000000099"
NODE_TEMPLATE_UUID = "40000000-0000-4000-8000-000000000001"
RESOURCE_TEMPLATE_UUID = "50000000-0000-4000-8000-000000000001"
SOURCE_HANDLE_UUID = "60000000-0000-4000-8000-000000000001"
TARGET_HANDLE_UUID = "60000000-0000-4000-8000-000000000002"
CATALOG_FINGERPRINT = f"sha256:{'d' * 64}"
SOURCE = "build()"
NORMALIZED_SOURCE = "build()\n"

CANDIDATE_INVALID = {
    "code": 422,
    "error": {
        "code": "candidate_invalid",
        "message": "工作流校验失败，请检查节点、连线和输入输出",
    },
}
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

SOURCE_ONLY_ATTACKS = (
    "workflow-name",
    "workflow-meta",
    "nodes",
    "edges",
    "node-param",
    "handle-template",
)
GRAPH_ATTACKS = (
    "hidden-node-update",
    "forged-node-update",
    "duplicate-created-edge",
    "cross-node-lifecycle",
    "cross-edge-lifecycle",
    "reserved-hidden",
    "reserved-forged",
    "source-map-dangling",
)
DIAGNOSTIC_ATTACKS = (
    "item-integer",
    "severity-integer",
    "code-integer",
    "message-list",
    "severity-missing",
    "code-missing",
    "message-missing",
)


def _empty_changeset(kind: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "created_node_uuids": [],
        "updated_node_uuids": [],
        "deleted_node_uuids": [],
        "created_edge_uuids": [],
        "updated_edge_uuids": [],
        "deleted_edge_uuids": [],
        "reserved_metadata_changed": False,
    }


def _source_map(workflow_node_uuid: str = NODE_UUID) -> list[dict[str, Any]]:
    return [
        {
            "workflow_node_uuid": workflow_node_uuid,
            "start_line": 1,
            "start_column": 1,
            "end_line": 1,
            "end_column": 7,
        }
    ]


def _edge() -> dict[str, Any]:
    return {
        "uuid": EDGE_UUID,
        "source_node_uuid": NODE_UUID,
        "target_node_uuid": NODE_UUID,
        "source_handle_uuid": SOURCE_HANDLE_UUID,
        "target_handle_uuid": TARGET_HANDLE_UUID,
        "meta_data": {},
    }


def _created_node(applied_graph: dict[str, Any]) -> dict[str, Any]:
    node = deepcopy(applied_graph["nodes"][0])
    node["uuid"] = CREATED_NODE_UUID
    node["name"] = "created candidate node"
    node["param"] = {"created": True}
    return node


def _valid_bundle(
    applied_graph: dict[str, Any],
    python_source: str,
    *,
    kind: str = "source_only",
) -> dict[str, Any]:
    graph = deepcopy(applied_graph)
    changeset = _empty_changeset(kind)
    if kind == "graph":
        graph["nodes"][0]["param"] = {"honest": True}
        changeset["updated_node_uuids"] = [NODE_UUID]
    return {
        "diagnostics": [],
        "graph": graph,
        "normalized_python_source": (
            python_source if python_source.endswith("\n") else python_source + "\n"
        ),
        "source_map": _source_map(),
        "changeset": changeset,
        "compiler_version": "phase-01-risk-round13-v1",
        "template_catalog_fingerprint": CATALOG_FINGERPRINT,
    }


def _source_only_attack_bundle(
    applied_graph: dict[str, Any],
    python_source: str,
    attack: str,
) -> dict[str, Any]:
    result = _valid_bundle(applied_graph, python_source)
    graph = result["graph"]
    if attack == "workflow-name":
        graph["workflow"]["name"] = "hidden source-only name"
    elif attack == "workflow-meta":
        graph["workflow"]["meta_data"] = {"hidden": {"source_only": True}}
    elif attack == "nodes":
        graph["nodes"].append(_created_node(applied_graph))
    elif attack == "edges":
        graph["edges"].append(_edge())
    elif attack == "node-param":
        graph["nodes"][0]["param"] = {"hidden": ["source_only"]}
    elif attack == "handle-template":
        graph["handle_templates"][0]["display_name"] = "Hidden handle change"
    else:
        raise AssertionError(f"unknown source-only attack: {attack}")
    return result


def _graph_attack_bundle(
    applied_graph: dict[str, Any],
    python_source: str,
    attack: str,
) -> dict[str, Any]:
    result = _valid_bundle(applied_graph, python_source)
    graph = result["graph"]
    changeset = _empty_changeset("graph")
    source_map = _source_map()

    if attack == "hidden-node-update":
        graph["nodes"][0]["param"] = {"hidden": "update"}
    elif attack == "forged-node-update":
        changeset["updated_node_uuids"] = [NODE_UUID]
    elif attack == "duplicate-created-edge":
        graph["edges"].append(_edge())
        changeset["created_edge_uuids"] = [EDGE_UUID, EDGE_UUID]
    elif attack == "cross-node-lifecycle":
        changeset["created_node_uuids"] = [NODE_UUID]
        changeset["updated_node_uuids"] = [NODE_UUID]
        changeset["deleted_node_uuids"] = [NODE_UUID]
    elif attack == "cross-edge-lifecycle":
        graph["edges"].append(_edge())
        changeset["created_edge_uuids"] = [EDGE_UUID]
        changeset["updated_edge_uuids"] = [EDGE_UUID]
        changeset["deleted_edge_uuids"] = [EDGE_UUID]
    elif attack == "reserved-hidden":
        graph["workflow"]["meta_data"] = {"unilab": {"hidden": True}}
    elif attack == "reserved-forged":
        changeset["reserved_metadata_changed"] = True
    elif attack == "source-map-dangling":
        graph["nodes"][0]["param"] = {"honest": "update"}
        changeset["updated_node_uuids"] = [NODE_UUID]
        source_map = _source_map(DANGLING_NODE_UUID)
    else:
        raise AssertionError(f"unknown graph attack: {attack}")

    result["graph"] = graph
    result["changeset"] = changeset
    result["source_map"] = source_map
    return result


def _diagnostic_attack_bundle(
    applied_graph: dict[str, Any],
    python_source: str,
    attack: str,
) -> dict[str, Any]:
    result = _valid_bundle(applied_graph, python_source)
    diagnostic: Any = {
        "severity": "warning",
        "code": "round13_warning",
        "message": "well-shaped control warning",
    }
    if attack == "item-integer":
        result["diagnostics"] = [1]
        return result
    if attack == "severity-integer":
        diagnostic["severity"] = 1
    elif attack == "code-integer":
        diagnostic["code"] = 1
    elif attack == "message-list":
        diagnostic["message"] = ["not", "text"]
    elif attack == "severity-missing":
        diagnostic.pop("severity")
    elif attack == "code-missing":
        diagnostic.pop("code")
    elif attack == "message-missing":
        diagnostic.pop("message")
    else:
        raise AssertionError(f"unknown diagnostics attack: {attack}")
    result["diagnostics"] = [diagnostic]
    return result


class ScenarioCompiler:
    compiler_version = "phase-01-risk-round13-v1"
    template_catalog_fingerprint = CATALOG_FINGERPRINT

    def __init__(
        self,
        *,
        first: tuple[str, str | None],
        second: tuple[str, str | None] | None = None,
    ) -> None:
        self.first = first
        self.second = second
        self.calls = 0

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
        scenario = self.first if self.calls == 1 or self.second is None else self.second
        category, attack = scenario
        if category == "valid":
            return _valid_bundle(
                applied_graph,
                python_source,
                kind=attack or "source_only",
            )
        if category == "source-only":
            assert attack is not None
            return _source_only_attack_bundle(
                applied_graph,
                python_source,
                attack,
            )
        if category == "graph":
            assert attack is not None
            return _graph_attack_bundle(
                applied_graph,
                python_source,
                attack,
            )
        if category == "diagnostics":
            assert attack is not None
            return _diagnostic_attack_bundle(
                applied_graph,
                python_source,
                attack,
            )
        raise AssertionError(f"unknown compiler scenario: {scenario!r}")


def _node() -> WorkflowNodeWrite:
    return WorkflowNodeWrite(
        uuid=NODE_UUID,
        workflow_node_template_uuid=NODE_TEMPLATE_UUID,
        name="applied node",
        status="idle",
        type="compute",
        pose={},
        param={},
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
    compiler: Any,
) -> tuple[WorkflowStore, WorkflowService, Path]:
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store, compiler=compiler)
    service.create_workflow(
        name="phase 01 risk round 13",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )
    _seed_template_catalog(store)
    service.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=[_node()],
        edges=[],
    )
    package_root = tmp_path / "package"
    package_root.mkdir()
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="phase_01_risk_round_13",
        package_root=package_root,
        relative_path="workflows/review.py",
    )
    return store, service, package_root / "workflows" / "review.py"


def _save_draft(client: TestClient) -> Any:
    return client.put(
        f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/draft",
        json={
            "python_source": SOURCE,
            "expected_draft_hash": None,
            "expected_workflow_revision": 2,
        },
    )


def _apply_candidate(client: TestClient, aggregate: dict[str, Any]) -> Any:
    candidate = aggregate["candidate"]
    assert candidate is not None
    return client.post(
        f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/apply",
        json={
            "expected_draft_hash": aggregate["draft"]["draft_hash"],
            "expected_workflow_revision": 2,
            "expected_candidate_hash": candidate["candidate_hash"],
        },
    )


def _source_bytes(source_path: Path) -> bytes | None:
    try:
        return source_path.read_bytes()
    except FileNotFoundError:
        return None


def _snapshot(
    service: WorkflowService,
    source_path: Path,
) -> dict[str, Any]:
    return {
        "graph": service.get_graph(WORKFLOW_UUID),
        "authoring": service.get_authoring(WORKFLOW_UUID),
        "source": _source_bytes(source_path),
        "events": service.list_events(after_id=0)["items"],
    }


def _json_payload(response: Any) -> dict[str, Any] | None:
    if not response.headers.get("content-type", "").startswith("application/json"):
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _assert_draft_candidate_invalid(
    *,
    response: Any,
    graph_before: dict[str, Any],
    service: WorkflowService,
    source_path: Path,
    event_cursor: int,
) -> None:
    payload = _json_payload(response) or {}
    aggregate = payload.get("data") or {}
    draft = aggregate.get("draft") or {}
    events = service.list_events(after_id=event_cursor)["items"]
    assert {
        "status": response.status_code,
        "envelope_code": payload.get("code"),
        "state": aggregate.get("state"),
        "candidate": aggregate.get("candidate"),
        "draft_source": draft.get("python_source"),
        "diagnostics": draft.get("diagnostics"),
        "graph_unchanged": service.get_graph(WORKFLOW_UUID) == graph_before,
        "canonical": _source_bytes(source_path),
        "event_causes": [event["data"]["cause"] for event in events],
    } == {
        "status": 200,
        "envelope_code": 0,
        "state": "draft_invalid",
        "candidate": None,
        "draft_source": SOURCE,
        "diagnostics": [CANDIDATE_INVALID_DIAGNOSTIC],
        "graph_unchanged": True,
        "canonical": SOURCE.encode(),
        "event_causes": ["draft_saved"],
    }


def _exercise_semantic_attack(
    *,
    tmp_path: Path,
    category: str,
    attack: str,
    stage: str,
) -> None:
    compiler = ScenarioCompiler(
        first=((category, attack) if stage == "draft" else ("valid", "source_only")),
        second=(category, attack) if stage == "apply" else None,
    )
    store, service, source_path = _open_authoring(tmp_path, compiler)
    try:
        graph_before = service.get_graph(WORKFLOW_UUID)
        event_items = service.list_events(after_id=0)["items"]
        event_cursor = event_items[-1]["id"] if event_items else 0
        with TestClient(
            create_workflow_app(service),
            raise_server_exceptions=False,
        ) as client:
            draft_response = _save_draft(client)
            if stage == "draft":
                _assert_draft_candidate_invalid(
                    response=draft_response,
                    graph_before=graph_before,
                    service=service,
                    source_path=source_path,
                    event_cursor=event_cursor,
                )
                return

            assert draft_response.status_code == 200
            saved = draft_response.json()["data"]
            assert saved["candidate"] is not None
            before_apply = _snapshot(service, source_path)
            apply_response = _apply_candidate(client, saved)
            after_apply = _snapshot(service, source_path)

        assert {
            "status": apply_response.status_code,
            "body": _json_payload(apply_response),
            "compiler_calls": compiler.calls,
            "state_unchanged": after_apply == before_apply,
        } == {
            "status": 422,
            "body": CANDIDATE_INVALID,
            "compiler_calls": 2,
            "state_unchanged": True,
        }
    finally:
        store.close()


@pytest.mark.parametrize("kind", ["source_only", "graph"], ids=["source-only", "graph"])
def test_honest_candidate_control_can_be_saved_and_applied(
    tmp_path: Path,
    kind: str,
) -> None:
    compiler = ScenarioCompiler(first=("valid", kind))
    store, service, source_path = _open_authoring(tmp_path, compiler)
    try:
        with TestClient(create_workflow_app(service)) as client:
            draft_response = _save_draft(client)
            saved = draft_response.json()["data"]
            applied_response = _apply_candidate(client, saved)

        payload = applied_response.json()
        graph = service.get_graph(WORKFLOW_UUID)
        authoring = service.get_authoring(WORKFLOW_UUID)
        assert {
            "draft_status": draft_response.status_code,
            "draft_state": saved["state"],
            "apply_status": applied_response.status_code,
            "apply_kind": payload["data"]["apply_result"]["kind"],
            "workflow_revision": graph["workflow"]["revision"],
            "node_param": graph["nodes"][0]["param"],
            "state": authoring["state"],
            "candidate": authoring["candidate"],
            "canonical": _source_bytes(source_path),
            "compiler_calls": compiler.calls,
        } == {
            "draft_status": 200,
            "draft_state": (
                "unapplied_source_only" if kind == "source_only" else "unapplied_graph"
            ),
            "apply_status": 200,
            "apply_kind": kind,
            "workflow_revision": 2 if kind == "source_only" else 3,
            "node_param": {} if kind == "source_only" else {"honest": True},
            "state": "applied",
            "candidate": None,
            "canonical": NORMALIZED_SOURCE.encode(),
            "compiler_calls": 2,
        }
    finally:
        store.close()


@pytest.mark.parametrize("stage", ["draft", "apply"])
@pytest.mark.parametrize("attack", SOURCE_ONLY_ATTACKS)
def test_source_only_cannot_hide_any_graph_change(
    tmp_path: Path,
    attack: str,
    stage: str,
) -> None:
    _exercise_semantic_attack(
        tmp_path=tmp_path,
        category="source-only",
        attack=attack,
        stage=stage,
    )


@pytest.mark.parametrize("stage", ["draft", "apply"])
@pytest.mark.parametrize("attack", GRAPH_ATTACKS)
def test_graph_changeset_and_source_map_must_match_candidate_graph(
    tmp_path: Path,
    attack: str,
    stage: str,
) -> None:
    _exercise_semantic_attack(
        tmp_path=tmp_path,
        category="graph",
        attack=attack,
        stage=stage,
    )


@pytest.mark.parametrize("attack", DIAGNOSTIC_ATTACKS)
def test_apply_malformed_compiler_diagnostics_is_stable_and_side_effect_free(
    tmp_path: Path,
    attack: str,
) -> None:
    compiler = ScenarioCompiler(
        first=("valid", "source_only"),
        second=("diagnostics", attack),
    )
    store, service, source_path = _open_authoring(tmp_path, compiler)
    try:
        with TestClient(
            create_workflow_app(service),
            raise_server_exceptions=False,
        ) as client:
            draft_response = _save_draft(client)
            saved = draft_response.json()["data"]
            assert saved["candidate"] is not None
            before_apply = _snapshot(service, source_path)
            apply_response = _apply_candidate(client, saved)
            after_apply = _snapshot(service, source_path)

        assert {
            "status": apply_response.status_code,
            "body": _json_payload(apply_response),
            "is_plaintext_500": (
                apply_response.status_code == 500
                and _json_payload(apply_response) is None
            ),
            "compiler_calls": compiler.calls,
            "state_unchanged": after_apply == before_apply,
        } == {
            "status": 422,
            "body": CANDIDATE_INVALID,
            "is_plaintext_500": False,
            "compiler_calls": 2,
            "state_unchanged": True,
        }
    finally:
        store.close()


def _deep_json_value(depth: int = 1500) -> str:
    return ("[" * depth) + "0" + ("]" * depth)


def _raw_body(
    endpoint: str,
    shape: str,
    *,
    saved: dict[str, Any] | None,
) -> bytes:
    if shape == "malformed":
        return b'{"python_source":"must not persist"'
    deep = _deep_json_value()
    if endpoint == "draft":
        return (
            "{"
            '"python_source":"must not persist",'
            '"expected_draft_hash":null,'
            '"expected_workflow_revision":2,'
            f'"unexpected":{deep}'
            "}"
        ).encode()
    assert saved is not None
    candidate = saved["candidate"]
    assert candidate is not None
    return (
        "{"
        f'"expected_draft_hash":"{saved["draft"]["draft_hash"]}",'
        '"expected_workflow_revision":2,'
        f'"expected_candidate_hash":"{candidate["candidate_hash"]}",'
        f'"unexpected":{deep}'
        "}"
    ).encode()


@pytest.mark.parametrize("endpoint", ["draft", "apply"])
@pytest.mark.parametrize("shape", ["malformed", "deep"])
def test_raw_json_failure_has_stable_envelope_and_no_authoring_side_effect(
    tmp_path: Path,
    endpoint: str,
    shape: str,
) -> None:
    compiler = ScenarioCompiler(first=("valid", "source_only"))
    store, service, source_path = _open_authoring(tmp_path, compiler)
    try:
        with TestClient(
            create_workflow_app(service),
            raise_server_exceptions=False,
        ) as client:
            saved: dict[str, Any] | None = None
            if endpoint == "apply":
                saved_response = _save_draft(client)
                saved = saved_response.json()["data"]
                assert saved["candidate"] is not None
            before = _snapshot(service, source_path)
            response = client.request(
                "PUT" if endpoint == "draft" else "POST",
                (
                    f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/draft"
                    if endpoint == "draft"
                    else f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/apply"
                ),
                content=_raw_body(endpoint, shape, saved=saved),
                headers={"content-type": "application/json"},
            )
            after = _snapshot(service, source_path)

        assert {
            "status": response.status_code,
            "body": _json_payload(response),
            "is_plaintext_500": (
                response.status_code == 500 and _json_payload(response) is None
            ),
            "state_unchanged": after == before,
        } == {
            "status": 400,
            "body": INVALID_INPUT,
            "is_plaintext_500": False,
            "state_unchanged": True,
        }
    finally:
        store.close()
