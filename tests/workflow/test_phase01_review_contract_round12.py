"""Phase 01 第十二轮冻结 Backend 公共合同测试。"""

from __future__ import annotations

import json
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
SECOND_NODE_UUID = "20000000-0000-4000-8000-000000000002"
EDGE_UUID = "30000000-0000-4000-8000-000000000001"
NODE_TEMPLATE_UUID = "40000000-0000-4000-8000-000000000001"
RESOURCE_TEMPLATE_UUID = "50000000-0000-4000-8000-000000000001"
SOURCE_HANDLE_UUID = "60000000-0000-4000-8000-000000000001"
TARGET_HANDLE_UUID = "60000000-0000-4000-8000-000000000002"
CATALOG_FINGERPRINT = f"sha256:{'9' * 64}"

INVALID_INPUT = {
    "code": 400,
    "error": {
        "code": "invalid_input",
        "message": "提交内容格式不正确",
    },
}
INTERNAL_ERROR = {
    "code": 500,
    "error": {
        "code": "internal_error",
        "message": "本地工作流服务出现错误，请重试或查看日志",
    },
}
CANDIDATE_INVALID_DIAGNOSTIC = {
    "severity": "error",
    "code": "candidate_invalid",
    "message": "工作流校验失败，请检查节点、连线和输入输出",
}

SOURCE_MAP_FIELDS = {
    "workflow_node_uuid",
    "start_line",
    "start_column",
    "end_line",
    "end_column",
}
CHANGESET_UUID_FIELDS = (
    "created_node_uuids",
    "updated_node_uuids",
    "deleted_node_uuids",
    "created_edge_uuids",
    "updated_edge_uuids",
    "deleted_edge_uuids",
)
CHANGESET_FIELDS = {
    "kind",
    *CHANGESET_UUID_FIELDS,
    "reserved_metadata_changed",
}
DEEP_JSON_OBJECT = {
    "level_1": [
        None,
        True,
        False,
        0,
        1.25,
        -2,
        {
            "text": "深层 JSON",
            "level_3": [1e308, -0.0, {"leaf": "ok"}],
        },
    ]
}


class DamagedAppliedGraphStore(WorkflowStore):
    """Inject one non-JSON leaf through the public authority graph seam."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.invalid_leaf: Any = None
        self.damage_enabled = False

    def get_graph(self, workflow_uuid: str) -> dict[str, Any]:
        graph = deepcopy(super().get_graph(workflow_uuid))
        if self.damage_enabled:
            graph["workflow"]["meta_data"] = {"nested": [{"leaf": self.invalid_leaf}]}
        return graph


class Round12Compiler:
    compiler_version = "phase-01-review-round-12"

    def __init__(
        self,
        *,
        graph_json_field: str | None = None,
        invalid_leaf: Any = None,
        bundle_case: str | None = None,
        changeset_kind: str = "graph",
        legal_deep_json: bool = False,
    ) -> None:
        self.graph_json_field = graph_json_field
        self.invalid_leaf = invalid_leaf
        self.bundle_case = bundle_case
        self.changeset_kind = changeset_kind
        self.legal_deep_json = legal_deep_json
        self.template_catalog_fingerprint = (
            "sha256:short"
            if bundle_case == "template-catalog-fingerprint-malformed"
            else CATALOG_FINGERPRINT
        )

    def compile(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        python_source: str,
        source_uri: str,
        applied_graph: dict[str, Any],
    ) -> dict[str, Any]:
        del source_uri
        graph = (
            deepcopy(applied_graph)
            if self.changeset_kind == "source_only"
            else self._graph(
                workflow_uuid=workflow_uuid,
                workflow_revision=workflow_revision,
                applied_graph=applied_graph,
            )
        )
        if self.graph_json_field is not None:
            self._inject_nested_leaf(
                graph,
                self.graph_json_field,
                self.invalid_leaf,
            )
        if self.legal_deep_json:
            self._inject_legal_deep_json(graph)

        source_map: Any = [
            {
                "workflow_node_uuid": NODE_UUID,
                "start_line": 1,
                "start_column": 1,
                "end_line": 1,
                "end_column": 1,
            }
        ]
        changeset: Any = self._changeset(self.changeset_kind)
        compiler_version: Any = self.compiler_version
        fingerprint: Any = self.template_catalog_fingerprint

        if self.bundle_case == "source-map-required-field-missing":
            source_map[0].pop("end_column")
        elif self.bundle_case == "source-map-extra-field":
            source_map[0]["node_name"] = "must not be accepted"
        elif self.bundle_case == "source-map-uuid-invalid":
            source_map[0]["workflow_node_uuid"] = "not-a-uuid"
        elif self.bundle_case == "source-map-coordinate-bool":
            source_map[0]["start_line"] = True
        elif self.bundle_case == "source-map-entry-nonobject":
            source_map = [1]
        elif self.bundle_case == "changeset-nonobject":
            changeset = []
        elif self.bundle_case == "changeset-kind-missing":
            changeset.pop("kind")
        elif self.bundle_case == "changeset-kind-invalid":
            changeset["kind"] = "partial"
        elif self.bundle_case is not None and self.bundle_case.startswith(
            "changeset-missing-"
        ):
            changeset.pop(self.bundle_case.removeprefix("changeset-missing-"))
        elif self.bundle_case == "changeset-uuid-list-object":
            changeset["updated_node_uuids"] = {}
        elif self.bundle_case == "changeset-uuid-member-invalid":
            changeset["updated_node_uuids"] = ["not-a-uuid"]
        elif self.bundle_case == "changeset-reserved-bool-integer":
            changeset["reserved_metadata_changed"] = 0
        elif self.bundle_case == "changeset-extra-field":
            changeset["frontend_only"] = True
        elif self.bundle_case == "compiler-version-empty":
            compiler_version = ""

        return {
            "diagnostics": [],
            "graph": graph,
            "normalized_python_source": python_source,
            "source_map": source_map,
            "changeset": changeset,
            "compiler_version": compiler_version,
            "template_catalog_fingerprint": fingerprint,
        }

    @staticmethod
    def _graph(
        *,
        workflow_uuid: str,
        workflow_revision: int,
        applied_graph: dict[str, Any],
    ) -> dict[str, Any]:
        del workflow_uuid, workflow_revision
        return {
            "workflow": deepcopy(applied_graph["workflow"]),
            "nodes": [
                {
                    "uuid": NODE_UUID,
                    "workflow_node_template_uuid": NODE_TEMPLATE_UUID,
                    "name": "candidate node",
                    "status": "idle",
                    "type": "compute",
                    "pose": {"x": 1.25},
                    "param": {"input": 1},
                    "execution_policy": {"retry": {"maximum": 0}},
                    "disabled": False,
                    "minimized": False,
                    "meta_data": {"node": {"round": 12}},
                },
                {
                    "uuid": SECOND_NODE_UUID,
                    "workflow_node_template_uuid": NODE_TEMPLATE_UUID,
                    "name": "candidate target node",
                    "status": "idle",
                    "type": "compute",
                    "pose": {"x": 2.5},
                    "param": {},
                    "execution_policy": {"retry": {"maximum": 0}},
                    "disabled": False,
                    "minimized": False,
                    "meta_data": {"node": {"round": 12}},
                },
            ],
            "edges": [
                {
                    "uuid": EDGE_UUID,
                    "source_node_uuid": NODE_UUID,
                    "target_node_uuid": SECOND_NODE_UUID,
                    "source_handle_uuid": SOURCE_HANDLE_UUID,
                    "target_handle_uuid": TARGET_HANDLE_UUID,
                    "meta_data": {"edge": {"round": 12}},
                }
            ],
            "node_templates": deepcopy(applied_graph["node_templates"]),
            "handle_templates": deepcopy(applied_graph["handle_templates"]),
        }

    @staticmethod
    def _changeset(kind: str) -> dict[str, Any]:
        if kind == "source_only":
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
        return {
            "kind": "graph",
            "created_node_uuids": [SECOND_NODE_UUID],
            "updated_node_uuids": [NODE_UUID],
            "deleted_node_uuids": [],
            "created_edge_uuids": [EDGE_UUID],
            "updated_edge_uuids": [],
            "deleted_edge_uuids": [],
            "reserved_metadata_changed": False,
        }

    @staticmethod
    def _json_container(
        graph: dict[str, Any],
        field: str,
    ) -> tuple[dict[str, Any], str]:
        entity_name, child = field.split(".", 1)
        if entity_name == "workflow":
            return graph["workflow"], child
        return graph[entity_name][0], child

    @classmethod
    def _inject_nested_leaf(
        cls,
        graph: dict[str, Any],
        field: str,
        leaf: Any,
    ) -> None:
        entity, child = cls._json_container(graph, field)
        entity[child] = (
            ["valid", {"nested": [{"leaf": leaf}]}]
            if field == "workflow.tags"
            else {"nested": [{"leaf": leaf}]}
        )

    @classmethod
    def _inject_legal_deep_json(cls, graph: dict[str, Any]) -> None:
        for field in CANDIDATE_JSON_FIELDS:
            entity, child = cls._json_container(graph, field)
            entity[child] = (
                [deepcopy(DEEP_JSON_OBJECT)]
                if field == "workflow.tags"
                else deepcopy(DEEP_JSON_OBJECT)
            )


@pytest.fixture()
def store(tmp_path: Path):
    opened = WorkflowStore(tmp_path / "workflow.db")
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture()
def damaged_store(tmp_path: Path):
    opened = DamagedAppliedGraphStore(tmp_path / "damaged-workflow.db")
    try:
        yield opened
    finally:
        opened.close()


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


def _seed_template_catalog(
    store: WorkflowStore,
    *,
    legal_deep_json: bool = False,
) -> None:
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
                      '{"from_goal":12}', '{"from_default":12}', '{}', '{}',
                      NULL, 'action', NULL, NULL, NULL, 'compute')
            """,
            (
                NODE_TEMPLATE_UUID,
                timestamp,
                timestamp,
                RESOURCE_TEMPLATE_UUID,
            ),
        )
        for handle_uuid, handle_key, io_type, display_name, required in (
            (SOURCE_HANDLE_UUID, "result", "source", "Result", 0),
            (TARGET_HANDLE_UUID, "input", "target", "Input", 0),
        ):
            connection.execute(
                """
                INSERT INTO workflow_handle_template(
                    uuid, create_time, update_time, meta_data, authority_id,
                    workflow_node_template_uuid, handle_key, io_type,
                    display_name, type, required, data_source, data_key
                ) VALUES (?, ?, ?, '{}', 'os-local', ?, ?, ?, ?, 'number', ?,
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
                    required,
                ),
            )
        if legal_deep_json:
            deep_json = json.dumps(DEEP_JSON_OBJECT)
            connection.execute(
                """
                UPDATE workflow_node_template
                SET meta_data = ?, goal = ?, goal_default = ?, feedback = ?,
                    result = ?
                WHERE uuid = ?
                """,
                (
                    deep_json,
                    deep_json,
                    deep_json,
                    deep_json,
                    deep_json,
                    NODE_TEMPLATE_UUID,
                ),
            )
            connection.execute(
                """
                UPDATE workflow_handle_template
                SET meta_data = ?, type = 'any'
                WHERE workflow_node_template_uuid = ?
                """,
                (deep_json, NODE_TEMPLATE_UUID),
            )


def _create_workflow(
    service: WorkflowService,
    *,
    name: str = "phase 01 review round 12",
    tags: list[Any] | None = None,
    meta_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return service.create_workflow(
        name=name,
        tags=[] if tags is None else tags,
        description=None,
        meta_data={} if meta_data is None else meta_data,
        workflow_uuid=WORKFLOW_UUID,
    )


def _authoring_service(
    store: WorkflowStore,
    tmp_path: Path,
    *,
    compiler: Any,
) -> tuple[WorkflowService, int]:
    service = WorkflowService(store, compiler=compiler)
    legal_deep_json = isinstance(compiler, Round12Compiler) and compiler.legal_deep_json
    _create_workflow(
        service,
        tags=[deepcopy(DEEP_JSON_OBJECT)] if legal_deep_json else [],
        meta_data=deepcopy(DEEP_JSON_OBJECT) if legal_deep_json else {},
    )
    _seed_template_catalog(store, legal_deep_json=legal_deep_json)
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
        package_id="phase_01_review_round_12",
        package_root=package_root,
        relative_path="workflows/review.py",
    )
    return service, 2


def _save_draft(
    client: TestClient,
    *,
    revision: int,
) -> Any:
    return client.put(
        f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/draft",
        json={
            "python_source": "build()\n",
            "expected_draft_hash": None,
            "expected_workflow_revision": revision,
        },
    )


def _raw_json_request(
    client: TestClient,
    method: str,
    path: str,
    body: str,
) -> Any:
    """Send exact non-standard bytes into the focused ASGI app."""

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


def _assert_candidate_invalid(response: Any) -> None:
    payload = _json_payload(response) or {}
    aggregate = payload.get("data", {})
    draft = aggregate.get("draft", {})
    assert {
        "status": response.status_code,
        "envelope_code": payload.get("code"),
        "state": aggregate.get("state"),
        "candidate": aggregate.get("candidate"),
        "saved_source": draft.get("python_source"),
        "has_draft_hash": bool(draft.get("draft_hash")),
        "diagnostics": draft.get("diagnostics"),
    } == {
        "status": 200,
        "envelope_code": 0,
        "state": "draft_invalid",
        "candidate": None,
        "saved_source": "build()\n",
        "has_draft_hash": True,
        "diagnostics": [CANDIDATE_INVALID_DIAGNOSTIC],
    }


CANDIDATE_JSON_FIELDS = (
    "workflow.tags",
    "workflow.meta_data",
    "nodes.pose",
    "nodes.param",
    "nodes.execution_policy",
    "nodes.meta_data",
    "edges.meta_data",
    "node_templates.goal",
    "node_templates.goal_default",
    "node_templates.feedback",
    "node_templates.result",
    "node_templates.meta_data",
    "handle_templates.meta_data",
)


@pytest.mark.parametrize(
    "graph_json_field",
    CANDIDATE_JSON_FIELDS,
    ids=[field.replace(".", "-") for field in CANDIDATE_JSON_FIELDS],
)
def test_candidate_rejects_non_json_object_leaf_in_every_json_container(
    store: WorkflowStore,
    tmp_path: Path,
    graph_json_field: str,
) -> None:
    service, revision = _authoring_service(
        store,
        tmp_path,
        compiler=Round12Compiler(
            graph_json_field=graph_json_field,
            invalid_leaf=object(),
        ),
    )

    with TestClient(
        create_workflow_app(service),
        raise_server_exceptions=False,
    ) as client:
        response = _save_draft(client, revision=revision)

    _assert_candidate_invalid(response)


@pytest.mark.parametrize(
    "invalid_leaf",
    [
        pytest.param({1}, id="set"),
        pytest.param(b"\x00\xff", id="bytes"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="positive-infinity"),
        pytest.param(float("-inf"), id="negative-infinity"),
    ],
)
def test_candidate_rejects_recursive_non_json_and_non_finite_leaves(
    store: WorkflowStore,
    tmp_path: Path,
    invalid_leaf: Any,
) -> None:
    service, revision = _authoring_service(
        store,
        tmp_path,
        compiler=Round12Compiler(
            graph_json_field="workflow.meta_data",
            invalid_leaf=invalid_leaf,
        ),
    )

    with TestClient(
        create_workflow_app(service),
        raise_server_exceptions=False,
    ) as client:
        response = _save_draft(client, revision=revision)

    _assert_candidate_invalid(response)


@pytest.mark.parametrize(
    "invalid_leaf",
    [
        pytest.param(object(), id="python-object"),
        pytest.param({1}, id="set"),
        pytest.param(b"\x00\xff", id="bytes"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="positive-infinity"),
        pytest.param(float("-inf"), id="negative-infinity"),
    ],
)
def test_get_authoring_wraps_recursive_applied_json_damage_as_internal_error(
    damaged_store: DamagedAppliedGraphStore,
    tmp_path: Path,
    invalid_leaf: Any,
) -> None:
    service, _ = _authoring_service(
        damaged_store,
        tmp_path,
        compiler=Round12Compiler(),
    )
    damaged_store.invalid_leaf = invalid_leaf
    damaged_store.damage_enabled = True

    with TestClient(
        create_workflow_app(service),
        raise_server_exceptions=False,
    ) as client:
        response = client.get(f"/api/v1/workflows/{WORKFLOW_UUID}/authoring")

    assert {
        "status": response.status_code,
        "body": _json_payload(response),
    } == {
        "status": 500,
        "body": INTERNAL_ERROR,
    }


def test_candidate_accepts_legal_deep_recursive_json(
    store: WorkflowStore,
    tmp_path: Path,
) -> None:
    service, revision = _authoring_service(
        store,
        tmp_path,
        compiler=Round12Compiler(legal_deep_json=True),
    )

    with TestClient(create_workflow_app(service)) as client:
        response = _save_draft(client, revision=revision)

    payload = response.json()
    candidate = payload["data"]["candidate"]
    graph = candidate["graph"]
    assert {
        "status": response.status_code,
        "state": payload["data"]["state"],
        "has_candidate_hash": bool(candidate["candidate_hash"]),
        "workflow_tags": graph["workflow"]["tags"],
        "workflow_meta_data": graph["workflow"]["meta_data"],
        "node_param": graph["nodes"][0]["param"],
        "template_result": graph["node_templates"][0]["result"],
    } == {
        "status": 200,
        "state": "unapplied_graph",
        "has_candidate_hash": True,
        "workflow_tags": [DEEP_JSON_OBJECT],
        "workflow_meta_data": DEEP_JSON_OBJECT,
        "node_param": DEEP_JSON_OBJECT,
        "template_result": DEEP_JSON_OBJECT,
    }


INVALID_BUNDLE_CASES = [
    pytest.param(
        "source-map-required-field-missing",
        id="source-map-required-field-missing",
    ),
    pytest.param("source-map-extra-field", id="source-map-extra-field"),
    pytest.param("source-map-uuid-invalid", id="source-map-uuid-invalid"),
    pytest.param("source-map-coordinate-bool", id="source-map-coordinate-bool"),
    pytest.param("source-map-entry-nonobject", id="source-map-entry-nonobject"),
    pytest.param("changeset-nonobject", id="changeset-nonobject"),
    pytest.param("changeset-kind-missing", id="changeset-kind-missing"),
    pytest.param("changeset-kind-invalid", id="changeset-kind-invalid"),
    *[
        pytest.param(
            f"changeset-missing-{field}",
            id=f"changeset-missing-{field.replace('_', '-')}",
        )
        for field in CHANGESET_UUID_FIELDS
    ],
    pytest.param(
        "changeset-uuid-list-object",
        id="changeset-uuid-list-object",
    ),
    pytest.param(
        "changeset-uuid-member-invalid",
        id="changeset-uuid-member-invalid",
    ),
    pytest.param(
        "changeset-reserved-bool-integer",
        id="changeset-reserved-bool-integer",
    ),
    pytest.param("changeset-extra-field", id="changeset-extra-field"),
    pytest.param("compiler-version-empty", id="compiler-version-empty"),
    pytest.param(
        "template-catalog-fingerprint-malformed",
        id="template-catalog-fingerprint-malformed",
    ),
]


@pytest.mark.parametrize("bundle_case", INVALID_BUNDLE_CASES)
def test_incomplete_candidate_bundle_is_saved_as_draft_diagnostic(
    store: WorkflowStore,
    tmp_path: Path,
    bundle_case: str,
) -> None:
    service, revision = _authoring_service(
        store,
        tmp_path,
        compiler=Round12Compiler(bundle_case=bundle_case),
    )

    with TestClient(
        create_workflow_app(service),
        raise_server_exceptions=False,
    ) as client:
        response = _save_draft(client, revision=revision)

    _assert_candidate_invalid(response)


@pytest.mark.parametrize(
    ("changeset_kind", "expected_state"),
    [
        ("graph", "unapplied_graph"),
        ("source_only", "unapplied_source_only"),
    ],
    ids=["graph", "source-only"],
)
def test_complete_candidate_bundle_accepts_both_changeset_kinds(
    store: WorkflowStore,
    tmp_path: Path,
    changeset_kind: str,
    expected_state: str,
) -> None:
    service, revision = _authoring_service(
        store,
        tmp_path,
        compiler=Round12Compiler(changeset_kind=changeset_kind),
    )

    with TestClient(create_workflow_app(service)) as client:
        response = _save_draft(client, revision=revision)

    payload = response.json()
    candidate = payload["data"]["candidate"]
    assert {
        "status": response.status_code,
        "state": payload["data"]["state"],
        "has_candidate_hash": bool(candidate["candidate_hash"]),
        "source_map_fields": set(candidate["source_map"][0]),
        "changeset_fields": set(candidate["changeset"]),
        "changeset_kind": candidate["changeset"]["kind"],
        "compiler_version": candidate["compiler_version"],
        "catalog_fingerprint": candidate["template_catalog_fingerprint"],
    } == {
        "status": 200,
        "state": expected_state,
        "has_candidate_hash": True,
        "source_map_fields": SOURCE_MAP_FIELDS,
        "changeset_fields": CHANGESET_FIELDS,
        "changeset_kind": changeset_kind,
        "compiler_version": "phase-01-review-round-12",
        "catalog_fingerprint": CATALOG_FINGERPRINT,
    }


def test_create_rejects_raw_nan_before_persisting_workflow(
    store: WorkflowStore,
) -> None:
    service = WorkflowService(store)

    with TestClient(
        create_workflow_app(service),
        raise_server_exceptions=False,
    ) as client:
        response = _raw_json_request(
            client,
            "POST",
            "/api/v1/workflows",
            """
            {
              "name": "nonfinite create",
              "tags": [],
              "description": null,
              "meta_data": {"nested": [NaN]}
            }
            """,
        )
        lookup = client.get(
            "/api/v1/workflows",
            params={"name": "nonfinite create"},
        )

    lookup_payload = _json_payload(lookup) or {}
    assert {
        "status": response.status_code,
        "body": _json_payload(response),
        "lookup_status": lookup.status_code,
        "lookup_items": (lookup_payload.get("data") or {}).get("items"),
    } == {
        "status": 400,
        "body": INVALID_INPUT,
        "lookup_status": 200,
        "lookup_items": [],
    }


def test_update_rejects_raw_infinity_without_mutating_workflow(
    store: WorkflowStore,
) -> None:
    service = WorkflowService(store)
    _create_workflow(service)

    with TestClient(
        create_workflow_app(service),
        raise_server_exceptions=False,
    ) as client:
        before = client.get(f"/api/v1/workflows/{WORKFLOW_UUID}").json()["data"]
        response = _raw_json_request(
            client,
            "PUT",
            f"/api/v1/workflows/{WORKFLOW_UUID}",
            """
            {
              "name": "must not persist",
              "tags": [{"nested": Infinity}],
              "description": "must not persist",
              "meta_data": {}
            }
            """,
        )
        after = client.get(f"/api/v1/workflows/{WORKFLOW_UUID}")

    after_payload = _json_payload(after) or {}
    after_data = after_payload.get("data")
    fields = ("name", "tags", "description", "meta_data", "revision")
    assert {
        "status": response.status_code,
        "body": _json_payload(response),
        "after_status": after.status_code,
        "after_workflow": (
            {field: after_data.get(field) for field in fields}
            if isinstance(after_data, dict)
            else None
        ),
    } == {
        "status": 400,
        "body": INVALID_INPUT,
        "after_status": 200,
        "after_workflow": {field: before.get(field) for field in fields},
    }


def test_graph_put_rejects_raw_negative_infinity_without_mutating_graph(
    store: WorkflowStore,
) -> None:
    service = WorkflowService(store)
    _create_workflow(service)

    with TestClient(
        create_workflow_app(service),
        raise_server_exceptions=False,
    ) as client:
        response = _raw_json_request(
            client,
            "PUT",
            f"/api/v1/workflows/{WORKFLOW_UUID}/graph",
            f"""
            {{
              "revision": 1,
              "nodes": [
                {{
                  "uuid": "{NODE_UUID}",
                  "name": "must not persist",
                  "status": "idle",
                  "type": "compute",
                  "pose": {{"nested": -Infinity}},
                  "param": {{}},
                  "execution_policy": {{}},
                  "disabled": false,
                  "minimized": false,
                  "meta_data": {{}}
                }}
              ],
              "edges": []
            }}
            """,
        )
        after = client.get(f"/api/v1/workflows/{WORKFLOW_UUID}/graph")

    after_payload = _json_payload(after) or {}
    after_graph = after_payload.get("data")
    assert {
        "status": response.status_code,
        "body": _json_payload(response),
        "after_status": after.status_code,
        "after_revision": (
            after_graph["workflow"]["revision"]
            if isinstance(after_graph, dict)
            else None
        ),
        "after_nodes": (
            after_graph.get("nodes") if isinstance(after_graph, dict) else None
        ),
    } == {
        "status": 400,
        "body": INVALID_INPUT,
        "after_status": 200,
        "after_revision": 1,
        "after_nodes": [],
    }


def test_create_normalizes_explicit_null_json_array_and_object(
    store: WorkflowStore,
) -> None:
    service = WorkflowService(store)

    with TestClient(create_workflow_app(service)) as client:
        response = _raw_json_request(
            client,
            "POST",
            "/api/v1/workflows",
            """
            {
              "name": "explicit null create",
              "tags": null,
              "description": null,
              "meta_data": null
            }
            """,
        )

    payload = _json_payload(response) or {}
    data = payload.get("data") or {}
    assert {
        "status": response.status_code,
        "envelope_code": payload.get("code"),
        "tags": data.get("tags"),
        "meta_data": data.get("meta_data"),
        "description_present": (
            "description" in data if isinstance(data, dict) else None
        ),
    } == {
        "status": 201,
        "envelope_code": 0,
        "tags": [],
        "meta_data": {},
        "description_present": False,
    }


def test_update_normalizes_explicit_null_json_array_and_object(
    store: WorkflowStore,
) -> None:
    service = WorkflowService(store)
    _create_workflow(service)

    with TestClient(create_workflow_app(service)) as client:
        response = _raw_json_request(
            client,
            "PUT",
            f"/api/v1/workflows/{WORKFLOW_UUID}",
            """
            {
              "name": "explicit null update",
              "tags": null,
              "description": null,
              "meta_data": null
            }
            """,
        )

    payload = _json_payload(response) or {}
    data = payload.get("data") or {}
    assert {
        "status": response.status_code,
        "envelope_code": payload.get("code"),
        "name": data.get("name"),
        "tags": data.get("tags"),
        "meta_data": data.get("meta_data"),
        "description_present": (
            "description" in data if isinstance(data, dict) else None
        ),
    } == {
        "status": 200,
        "envelope_code": 0,
        "name": "explicit null update",
        "tags": [],
        "meta_data": {},
        "description_present": False,
    }


def test_graph_put_normalizes_explicit_null_objects_but_preserves_param_default(
    store: WorkflowStore,
) -> None:
    service = WorkflowService(store)
    _create_workflow(service)
    _seed_template_catalog(store)

    with TestClient(create_workflow_app(service)) as client:
        response = _raw_json_request(
            client,
            "PUT",
            f"/api/v1/workflows/{WORKFLOW_UUID}/graph",
            f"""
            {{
              "revision": 1,
              "nodes": [
                {{
                  "uuid": "{NODE_UUID}",
                  "workflow_node_template_uuid": "{NODE_TEMPLATE_UUID}",
                  "name": "explicit null node",
                  "status": "idle",
                  "type": "compute",
                  "pose": null,
                  "param": null,
                  "execution_policy": null,
                  "disabled": false,
                  "minimized": false,
                  "meta_data": null
                }}
              ],
              "edges": []
            }}
            """,
        )

    payload = _json_payload(response) or {}
    data = payload.get("data") or {}
    nodes = data.get("nodes") or []
    node = nodes[0] if nodes else {}
    assert {
        "status": response.status_code,
        "envelope_code": payload.get("code"),
        "workflow_revision": (data.get("workflow") or {}).get("revision"),
        "pose": node.get("pose"),
        "execution_policy": node.get("execution_policy"),
        "meta_data": node.get("meta_data"),
        "param": node.get("param"),
    } == {
        "status": 200,
        "envelope_code": 0,
        "workflow_revision": 2,
        "pose": {},
        "execution_policy": {},
        "meta_data": {},
        "param": {"from_default": 12},
    }
