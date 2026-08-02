"""D1A-S1 设备页单 Action Task 的公开 HTTP 与持久事实 tracer。"""

from __future__ import annotations

import copy
import importlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.catalog import (
    CatalogAuthority,
    NodeTemplateImport,
    TemplateCatalog,
)
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore

AUTHORITY = CatalogAuthority(authority_id="os-local", kind="local")
RESOURCE_TEMPLATE_UUID = "10000000-0000-4000-8000-000000000001"
OTHER_RESOURCE_TEMPLATE_UUID = "10000000-0000-4000-8000-000000000002"
MISSING_TEMPLATE_UUID = "20000000-0000-4000-8000-000000000099"


def _closed_action_schema(
    *,
    material: bool = False,
) -> dict[str, Any]:
    value_schema: dict[str, Any]
    if material:
        value_schema = {"$slot": "ResourceSlot"}
        input_name = "sample"
        result_schema = {
            "type": "object",
            "properties": {"sample": value_schema},
            "required": ["sample"],
            "additionalProperties": False,
        }
        output_order = ["sample"]
        symbols = {
            "goal": {"sample": ["test.resources:sample"]},
            "result": {"sample": ["test.resources:sample"]},
        }
    else:
        input_name = "duration_seconds"
        value_schema = {"type": "integer", "minimum": 1}
        goal_properties = {
            input_name: value_schema,
            "note": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "default": None,
            },
            "axes": {
                "type": "array",
                "items": {"type": "integer"},
                "default": [],
            },
            "options": {
                "type": "object",
                "additionalProperties": True,
                "default": {},
            },
        }
        result_schema = {
            "type": "object",
            "properties": {"completed": {"type": "boolean"}},
            "required": ["completed"],
            "additionalProperties": False,
        }
        output_order = ["completed"]
        input_order = [input_name, "note", "axes", "options"]
        symbols = {"goal": {}, "result": {}}
    if material:
        goal_properties = {input_name: value_schema}
        input_order = [input_name]
    return {
        "type": "object",
        "properties": {
            "goal": {
                "type": "object",
                "properties": goal_properties,
                "required": [input_name],
                "additionalProperties": False,
            },
            "feedback": {
                "type": "object",
                "properties": {"progress": {"type": "number"}},
                "required": ["progress"],
                "additionalProperties": False,
            },
            "result": result_schema,
        },
        "required": ["goal", "feedback", "result"],
        "additionalProperties": False,
        "x-unilabos-action-contract": {
            "version": 1,
            "input_order": input_order,
            "output_order": output_order,
            "resource_template_symbols": symbols,
        },
    }


SIMPLE_SCHEMA = _closed_action_schema()
MATERIAL_SCHEMA = _closed_action_schema(material=True)


def _template_import(
    *,
    name: str,
    display_name: str,
    resource_template_uuid: str,
    schema: dict[str, Any],
) -> NodeTemplateImport:
    contract = schema["x-unilabos-action-contract"]
    goal_properties = schema["properties"]["goal"]["properties"]
    result_properties = schema["properties"]["result"]["properties"]
    required_inputs = set(schema["properties"]["goal"]["required"])
    goal_default = {
        name: copy.deepcopy(value_schema["default"])
        for name, value_schema in goal_properties.items()
        if "default" in value_schema
    }

    def value_type(value_schema: dict[str, Any]) -> str:
        if value_schema.get("$slot") == "ResourceSlot":
            return "ResourceSlot"
        schema_type = value_schema.get("type")
        if schema_type == "integer":
            return "integer"
        if schema_type == "array":
            return "array"
        if schema_type == "object":
            return "object"
        if "anyOf" in value_schema:
            return str(value_schema["anyOf"][0]["type"])
        return str(schema_type)

    handles: list[dict[str, Any]] = []
    for input_name in contract["input_order"]:
        input_schema = goal_properties[input_name]
        input_type = value_type(input_schema)
        handles.append(
            {
                "description": None,
                "meta_data": {
                    "unilab": {
                        "value_schema": copy.deepcopy(input_schema),
                        "editor_control": (
                            "material_port"
                            if input_type == "ResourceSlot"
                            else "variable_selector"
                        ),
                        "allowed_resource_template_uuids": (
                            [OTHER_RESOURCE_TEMPLATE_UUID]
                            if input_type == "ResourceSlot"
                            else None
                        ),
                        "implicit_passthrough": False,
                    }
                },
                "handle_key": input_name,
                "io_type": "target",
                "display_name": input_name,
                "type": input_type,
                "required": input_name in required_inputs,
                "data_source": "goal",
                "data_key": input_name,
            }
        )
    for output_name in contract["output_order"]:
        output_schema = result_properties[output_name]
        output_type = value_type(output_schema)
        handles.append(
            {
                "description": None,
                "meta_data": {
                    "unilab": {
                        "value_schema": copy.deepcopy(output_schema),
                        "editor_control": "variable_selector",
                        "allowed_resource_template_uuids": (
                            [OTHER_RESOURCE_TEMPLATE_UUID]
                            if output_type == "ResourceSlot"
                            else None
                        ),
                        "implicit_passthrough": output_type == "ResourceSlot",
                    }
                },
                "handle_key": output_name,
                "io_type": "source",
                "display_name": output_name,
                "type": output_type,
                "required": False,
                "data_source": "result",
                "data_key": output_name,
            }
        )
    return NodeTemplateImport(
        template={
            "description": "D1A public tracer action",
            "meta_data": {},
            "resource_template_uuid": resource_template_uuid,
            "name": name,
            "display_name": display_name,
            "class": "test.device",
            "goal": {name: name for name in contract["input_order"]},
            "goal_default": goal_default,
            "feedback": {"progress": "progress"},
            "result": {name: name for name in contract["output_order"]},
            "schema": schema,
            "type": "action",
            "icon": None,
            "header": None,
            "footer": None,
            "node_type": "device",
        },
        handles=handles,
    )


class MutableLiveCatalog:
    """HostNode Action registry 的 detached 测试 port。"""

    def __init__(self) -> None:
        self.devices: dict[str, dict[str, Any]] = {
            "robot": {
                "online": True,
                "resource_template_uuid": RESOURCE_TEMPLATE_UUID,
                "actions": {
                    "move": {
                        "type": "action",
                        "schema": copy.deepcopy(SIMPLE_SCHEMA),
                    }
                },
            },
            "material-robot": {
                "online": True,
                "resource_template_uuid": OTHER_RESOURCE_TEMPLATE_UUID,
                "actions": {
                    "consume": {
                        "type": "action",
                        "schema": copy.deepcopy(MATERIAL_SCHEMA),
                    }
                },
            },
        }

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return copy.deepcopy(self.devices)


class RecordingAdmission:
    """只验证 materialization 已提交后才唤醒正式 scheduler。"""

    def __init__(self, database_path: Path, *, available: bool = True) -> None:
        self.database_path = database_path
        self.available = available
        self.wakes: list[tuple[str, str]] = []

    def is_available(self) -> bool:
        return self.available

    def wake(self, task_uuid: str, job_uuid: str) -> None:
        # 独立 connection 看不到未提交写入；因此这不是对 WorkflowStore 的 mock。
        with sqlite3.connect(self.database_path) as connection:
            task = connection.execute(
                "SELECT uuid FROM workflow_task WHERE uuid = ?",
                (task_uuid,),
            ).fetchone()
            job = connection.execute(
                "SELECT uuid FROM workflow_node_job WHERE uuid = ?",
                (job_uuid,),
            ).fetchone()
        assert task == (task_uuid,)
        assert job == (job_uuid,)
        self.wakes.append((task_uuid, job_uuid))


@dataclass
class Harness:
    database_path: Path
    store: WorkflowStore
    client: TestClient
    catalog: TemplateCatalog
    fingerprint: str
    simple_template_uuid: str
    material_template_uuid: str
    live_catalog: MutableLiveCatalog
    admission: RecordingAdmission


def _public_member(module_name: str, name: str) -> Any:
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        if error.name != module_name:
            raise
        pytest.fail(f"D1A 缺少公开模块 {module_name}", pytrace=False)
    if not hasattr(module, name):
        pytest.fail(f"D1A 缺少公开 Interface {module_name}.{name}", pytrace=False)
    return getattr(module, name)


@pytest.fixture()
def harness(tmp_path: Path) -> Any:
    database_path = tmp_path / "workflow.db"
    store = WorkflowStore(database_path)
    catalog = TemplateCatalog(store)
    snapshot = catalog.replace(
        AUTHORITY,
        [
            _template_import(
                name="move",
                display_name="移动",
                resource_template_uuid=RESOURCE_TEMPLATE_UUID,
                schema=SIMPLE_SCHEMA,
            ),
            _template_import(
                name="consume",
                display_name="消耗样品",
                resource_template_uuid=OTHER_RESOURCE_TEMPLATE_UUID,
                schema=MATERIAL_SCHEMA,
            ),
        ],
    )
    simple_template = next(
        item for item in snapshot.node_templates if item["name"] == "move"
    )
    material_template = next(
        item for item in snapshot.node_templates if item["name"] == "consume"
    )
    live_catalog = MutableLiveCatalog()
    admission = RecordingAdmission(database_path)
    device_action_service = _public_member(
        "unilabos.workflow.device_action_task",
        "DeviceActionTaskService",
    )(
        store=store,
        template_catalog=catalog,
        authority=AUTHORITY,
        live_catalog=live_catalog,
        admission=admission,
    )
    workflow_service = WorkflowService(store)
    client = TestClient(
        create_workflow_app(
            workflow_service,
            device_action_tasks=device_action_service,
        ),
        raise_server_exceptions=False,
    )
    try:
        yield Harness(
            database_path=database_path,
            store=store,
            client=client,
            catalog=catalog,
            fingerprint=snapshot.fingerprint,
            simple_template_uuid=str(simple_template["uuid"]),
            material_template_uuid=str(material_template["uuid"]),
            live_catalog=live_catalog,
            admission=admission,
        )
    finally:
        client.close()
        store.close()


def _request(
    harness: Harness,
    *,
    idempotency_key: str | None = None,
    input_value: dict[str, Any] | None = None,
    template_uuid: str | None = None,
    device_id: str = "robot",
    fingerprint: str | None = None,
) -> dict[str, Any]:
    return {
        "authority_id": AUTHORITY.authority_id,
        "template_catalog_fingerprint": fingerprint or harness.fingerprint,
        "workflow_node_template_uuid": template_uuid or harness.simple_template_uuid,
        "device_id": device_id,
        "input": input_value if input_value is not None else {"duration_seconds": 5},
        "idempotency_key": idempotency_key or str(uuid4()),
        "description": "设备页单动作运行",
    }


def _assert_error(response: Any, *, status: int, code: str) -> None:
    assert response.status_code == status
    payload = response.json()
    assert payload["code"] == status
    assert payload["error"]["code"] == code
    assert "message" in payload["error"]
    assert "detail" not in payload


def _assert_no_internal_source(value: Any) -> None:
    forbidden = {
        "workflow_uuid",
        "workflow_node_uuid",
        "node_uuid",
        "target_node_uuid",
        "source_revision",
        "source_content",
        "python_source",
        "source_path",
        "source_uri",
        "workflow_snapshot",
        "execution_plan",
        "executor_assignment",
        "lease_token",
    }
    if isinstance(value, dict):
        assert forbidden.isdisjoint(value), value
        for item in value.values():
            _assert_no_internal_source(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_internal_source(item)


def _assert_no_internal_identity(value: Any, identities: set[str]) -> None:
    if isinstance(value, dict):
        for item in value.values():
            _assert_no_internal_identity(item, identities)
    elif isinstance(value, list):
        for item in value:
            _assert_no_internal_identity(item, identities)
    elif isinstance(value, str):
        assert value not in identities


def _table_count(store: WorkflowStore, table: str) -> int:
    assert table in {
        "device_action_system_source",
        "device_action_task",
        "workflow",
        "workflow_node",
        "workflow_task",
        "workflow_node_job",
    }
    with store.transaction() as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_post_get_materializes_one_formal_task_job_and_sanitized_view(
    harness: Harness,
) -> None:
    response = harness.client.post(
        "/api/v1/device-action-tasks",
        json=_request(harness),
    )

    assert response.status_code == 201
    assert response.json()["code"] == 0
    view = response.json()["data"]
    assert {
        "authority_id": view["authority_id"],
        "template_catalog_fingerprint": view["template_catalog_fingerprint"],
        "workflow_node_template_uuid": view["workflow_node_template_uuid"],
        "name": view["name"],
        "display_name": view["display_name"],
        "device_id": view["device_id"],
        "status": view["status"],
        "control_status": view["control_status"],
        "cleanup_status": view["cleanup_status"],
        "input": view["input"],
        "output": view["output"],
        "error_info": view["error_info"],
        "job_status": view["job_status"],
        "feedback_cursor": view["feedback_cursor"],
    } == {
        "authority_id": "os-local",
        "template_catalog_fingerprint": harness.fingerprint,
        "workflow_node_template_uuid": harness.simple_template_uuid,
        "name": "move",
        "display_name": "移动",
        "device_id": "robot",
        "status": "pending",
        "control_status": "active",
        "cleanup_status": "none",
        "input": {
            "duration_seconds": 5,
            "note": None,
            "axes": [],
            "options": {},
        },
        "output": {},
        "error_info": [],
        "job_status": "pending",
        "feedback_cursor": 0,
    }
    assert isinstance(view["task_uuid"], str)
    assert isinstance(view["job_uuid"], str)
    assert set(view) >= {
        "create_time",
        "update_time",
        "started_at",
        "finished_at",
    }
    _assert_no_internal_source(view)
    assert harness.admission.wakes == [(view["task_uuid"], view["job_uuid"])]

    get_response = harness.client.get(
        f"/api/v1/device-action-tasks/{view['task_uuid']}"
    )
    assert get_response.status_code == 200
    assert get_response.json() == {"code": 0, "data": view}
    _assert_no_internal_source(get_response.json())

    with harness.store.transaction() as connection:
        task = dict(
            connection.execute(
                "SELECT * FROM workflow_task WHERE uuid = ?",
                (view["task_uuid"],),
            ).fetchone()
        )
        job = dict(
            connection.execute(
                "SELECT * FROM workflow_node_job WHERE uuid = ?",
                (view["job_uuid"],),
            ).fetchone()
        )
        link = dict(
            connection.execute(
                "SELECT * FROM device_action_task WHERE workflow_task_uuid = ?",
                (view["task_uuid"],),
            ).fetchone()
        )
        event = dict(
            connection.execute(
                "SELECT event, data FROM frontend_event ORDER BY id DESC LIMIT 1"
            ).fetchone()
        )
    assert task["run_mode"] == "single_node"
    assert task["target_node_uuid"] == job["workflow_node_uuid"]
    assert task["status"] == job["status"] == "pending"
    assert json.loads(task["input"]) == {
        "duration_seconds": 5,
        "note": None,
        "axes": [],
        "options": {},
    }
    assert link["workflow_node_job_uuid"] == view["job_uuid"]
    assert link["device_id"] == "robot"
    assert event["event"] == "device_action_task.changed"
    assert json.loads(event["data"]) == {"task_uuid": view["task_uuid"]}


def test_production_action_transport_type_is_not_mistaken_for_node_kind(
    harness: Harness,
) -> None:
    canonical = _template_import(
        name="move",
        display_name="移动",
        resource_template_uuid=RESOURCE_TEMPLATE_UUID,
        schema=SIMPLE_SCHEMA,
    )
    canonical.template["type"] = "UniLabJsonCommand"
    snapshot = harness.catalog.replace(AUTHORITY, [canonical])
    template = snapshot.node_templates[0]
    harness.live_catalog.devices["robot"]["actions"]["move"][
        "type"
    ] = "UniLabJsonCommand"

    response = harness.client.post(
        "/api/v1/device-action-tasks",
        json=_request(
            harness,
            fingerprint=snapshot.fingerprint,
            template_uuid=str(template["uuid"]),
        ),
    )

    assert response.status_code == 201
    view = response.json()["data"]
    assert view["name"] == "move"
    assert view["workflow_node_template_uuid"] == str(template["uuid"])


def test_idempotency_replay_is_one_task_and_conflicting_payload_is_409(
    harness: Harness,
) -> None:
    key = str(uuid4())
    request = _request(harness, idempotency_key=key)
    first = harness.client.post("/api/v1/device-action-tasks", json=request)
    replay = harness.client.post("/api/v1/device-action-tasks", json=request)

    assert first.status_code == 201
    assert 200 <= replay.status_code < 300
    assert replay.json()["data"] == first.json()["data"]
    assert _table_count(harness.store, "workflow_task") == 1
    assert _table_count(harness.store, "workflow_node_job") == 1


def test_idempotency_replay_survives_mutable_admission_and_catalog_changes(
    harness: Harness,
) -> None:
    key = str(uuid4())
    request = _request(harness, idempotency_key=key)
    first = harness.client.post("/api/v1/device-action-tasks", json=request)
    assert first.status_code == 201

    harness.admission.available = False
    harness.live_catalog.devices["robot"]["online"] = False
    changed = _template_import(
        name="move",
        display_name="移动（新合同）",
        resource_template_uuid=RESOURCE_TEMPLATE_UUID,
        schema=SIMPLE_SCHEMA,
    )
    harness.catalog.replace(AUTHORITY, [changed])

    replay = harness.client.post("/api/v1/device-action-tasks", json=request)

    assert 200 <= replay.status_code < 300
    assert replay.json()["data"] == first.json()["data"]
    assert _table_count(harness.store, "workflow_task") == 1
    assert _table_count(harness.store, "workflow_node_job") == 1
    assert len(harness.admission.wakes) == 1
    assert len(harness.admission.wakes) == 1

    conflict = harness.client.post(
        "/api/v1/device-action-tasks",
        json={**request, "input": {"duration_seconds": 6}},
    )
    _assert_error(conflict, status=409, code="idempotency_conflict")
    assert _table_count(harness.store, "workflow_task") == 1
    assert _table_count(harness.store, "workflow_node_job") == 1


def test_request_validation_is_strict_and_rolls_back(harness: Harness) -> None:
    invalid_requests = [
        {**_request(harness), "action_name": "move"},
        {**_request(harness), "idempotency_key": "not-a-uuid"},
        _request(harness, input_value={"duration_seconds": "5"}),
        _request(harness, input_value={"duration_seconds": True}),
        _request(harness, input_value={"duration_seconds": 5, "note": 1}),
        _request(
            harness,
            input_value={"duration_seconds": 5, "axes": [1, "2"]},
        ),
        _request(
            harness,
            input_value={"duration_seconds": 5, "unexpected": "drop-me"},
        ),
        _request(harness, input_value={}),
    ]

    for request in invalid_requests:
        response = harness.client.post("/api/v1/device-action-tasks", json=request)
        _assert_error(response, status=400, code="invalid_input")

    assert _table_count(harness.store, "device_action_system_source") == 0
    assert _table_count(harness.store, "device_action_task") == 0
    assert _table_count(harness.store, "workflow_task") == 0
    assert _table_count(harness.store, "workflow_node_job") == 0
    assert harness.admission.wakes == []


def test_catalog_stale_and_missing_template_fail_before_persistence(
    harness: Harness,
) -> None:
    stale = harness.client.post(
        "/api/v1/device-action-tasks",
        json=_request(harness, fingerprint=f"sha256:{'f' * 64}"),
    )
    _assert_error(stale, status=409, code="template_catalog_conflict")

    missing = harness.client.post(
        "/api/v1/device-action-tasks",
        json=_request(harness, template_uuid=MISSING_TEMPLATE_UUID),
    )
    _assert_error(missing, status=404, code="not_found")
    assert _table_count(harness.store, "workflow_task") == 0
    assert _table_count(harness.store, "workflow_node_job") == 0
    assert harness.admission.wakes == []


def test_device_absence_and_action_contract_mismatch_are_distinct(
    harness: Harness,
) -> None:
    missing = harness.client.post(
        "/api/v1/device-action-tasks",
        json=_request(harness, device_id="unknown-device"),
    )
    _assert_error(missing, status=404, code="not_found")

    del harness.live_catalog.devices["robot"]["actions"]["move"]
    action_removed = harness.client.post(
        "/api/v1/device-action-tasks",
        json=_request(harness),
    )
    _assert_error(
        action_removed,
        status=409,
        code="device_action_mismatch",
    )

    harness.live_catalog.devices["robot"]["actions"]["move"] = {
        "type": "action",
        "schema": _closed_action_schema(material=True),
    }
    mismatch = harness.client.post(
        "/api/v1/device-action-tasks",
        json=_request(harness),
    )
    _assert_error(mismatch, status=409, code="device_action_mismatch")
    assert _table_count(harness.store, "workflow_task") == 0
    assert _table_count(harness.store, "workflow_node_job") == 0


def test_resource_slot_contract_is_rejected_without_partial_materialization(
    harness: Harness,
) -> None:
    response = harness.client.post(
        "/api/v1/device-action-tasks",
        json=_request(
            harness,
            template_uuid=harness.material_template_uuid,
            device_id="material-robot",
            input_value={"sample": {"uuid": str(uuid4())}},
        ),
    )

    _assert_error(response, status=422, code="unsupported_contract")
    assert _table_count(harness.store, "device_action_system_source") == 0
    assert _table_count(harness.store, "workflow_task") == 0
    assert _table_count(harness.store, "workflow_node_job") == 0
    assert harness.admission.wakes == []


def test_system_source_and_task_job_are_opaque_to_ordinary_routes(
    harness: Harness,
) -> None:
    created = harness.client.post(
        "/api/v1/device-action-tasks",
        json=_request(harness),
    ).json()["data"]
    with harness.store.transaction() as connection:
        source = dict(
            connection.execute("SELECT * FROM device_action_system_source").fetchone()
        )

    workflows = harness.client.get("/api/v1/workflows")
    assert workflows.status_code == 200
    assert workflows.json()["data"]["items"] == []
    tasks = harness.client.get("/api/v1/workflow-tasks")
    assert tasks.status_code == 200
    assert tasks.json()["data"]["items"] == []

    for path in (
        f"/api/v1/workflows/{source['workflow_uuid']}",
        f"/api/v1/workflows/{source['workflow_uuid']}/graph",
        f"/api/v1/workflows/{source['workflow_uuid']}/authoring",
        f"/api/v1/workflow-tasks/{created['task_uuid']}",
        f"/api/v1/workflow-tasks/{created['task_uuid']}/jobs",
        f"/api/v1/workflow-node-jobs/{created['job_uuid']}",
    ):
        _assert_error(
            harness.client.get(path),
            status=404,
            code="not_found",
        )

    feedback = harness.client.get(
        f"/api/v1/workflow-node-jobs/{created['job_uuid']}/feedback"
    )
    assert feedback.status_code == 200
    assert feedback.json()["data"]["items"] == []
    _assert_no_internal_source(feedback.json())

    command = harness.client.post(
        f"/api/v1/workflow-tasks/{created['task_uuid']}/commands",
        json={
            "type": "cancel",
            "target_node_uuid": None,
            "idempotency_key": str(uuid4()),
            "description": "operator cancel",
            "meta_data": {},
        },
    )
    assert command.status_code == 201
    _assert_no_internal_identity(
        command.json(),
        {source["workflow_uuid"], source["workflow_node_uuid"]},
    )


def test_system_source_identity_is_stable_and_device_is_not_graph_state(
    harness: Harness,
) -> None:
    first = harness.client.post(
        "/api/v1/device-action-tasks",
        json=_request(harness),
    )
    second = harness.client.post(
        "/api/v1/device-action-tasks",
        json=_request(harness),
    )
    assert first.status_code == second.status_code == 201

    with harness.store.transaction() as connection:
        sources = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM device_action_system_source"
            ).fetchall()
        ]
        workflows = [
            dict(row) for row in connection.execute("SELECT * FROM workflow").fetchall()
        ]
        nodes = [
            dict(row)
            for row in connection.execute("SELECT * FROM workflow_node").fetchall()
        ]
        tasks = [
            dict(row)
            for row in connection.execute("SELECT * FROM workflow_task").fetchall()
        ]
        jobs = [
            dict(row)
            for row in connection.execute("SELECT * FROM workflow_node_job").fetchall()
        ]

    assert len(sources) == len(workflows) == len(nodes) == 1
    assert len(tasks) == len(jobs) == 2
    source = sources[0]
    assert source["authority_id"] == "os-local"
    assert source["workflow_node_template_uuid"] == harness.simple_template_uuid
    assert source["origin_kind"] == "system/device-console"
    assert source["workflow_uuid"] == workflows[0]["uuid"]
    assert source["workflow_node_uuid"] == nodes[0]["uuid"]
    assert workflows[0]["revision"] == source["source_revision"]
    assert "system/device-console" not in workflows[0]["meta_data"]
    assert json.loads(nodes[0]["param"]) == {}
    assert "robot" not in nodes[0]["meta_data"]
    assert {task["workflow_uuid"] for task in tasks} == {source["workflow_uuid"]}
    assert {task["target_node_uuid"] for task in tasks} == {
        source["workflow_node_uuid"]
    }
    for task in tasks:
        snapshot = json.loads(task["workflow_snapshot"])
        contract = snapshot["workflow"]["meta_data"]["unilab"]["input_contract"]
        assert contract == {
            "version": 1,
            "parameters": [
                {
                    "name": "duration_seconds",
                    "schema": {"type": "integer", "minimum": 1},
                    "required": True,
                },
                {
                    "name": "note",
                    "schema": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "required": False,
                    "default": None,
                },
                {
                    "name": "axes",
                    "schema": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                    "required": False,
                    "default": [],
                },
                {
                    "name": "options",
                    "schema": {"type": "object"},
                    "required": False,
                    "default": {},
                },
            ],
        }
        bindings = snapshot["nodes"][0]["meta_data"]["unilab"]["input_bindings"]
        assert {item["parameter"] for item in bindings.values()} == {
            "duration_seconds",
            "note",
            "axes",
            "options",
        }
        assert snapshot["nodes"][0]["param"] == {}
        plan = json.loads(task["execution_plan"])
        assert plan["nodes"][0]["param"] == {
            "duration_seconds": 5,
            "note": None,
            "axes": [],
            "options": {},
        }
    assert all(
        json.loads(job["param"])
        == {
            "duration_seconds": 5,
            "note": None,
            "axes": [],
            "options": {},
        }
        for job in jobs
    )


def test_catalog_contract_change_revises_stable_system_source_and_freezes_old_task(
    harness: Harness,
) -> None:
    first = harness.client.post(
        "/api/v1/device-action-tasks",
        json=_request(harness),
    )
    assert first.status_code == 201

    revised_schema = copy.deepcopy(SIMPLE_SCHEMA)
    revised_schema["properties"]["result"] = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    revised_schema["x-unilabos-action-contract"]["output_order"] = ["ok"]
    revised_import = _template_import(
        name="move",
        display_name="移动（修订）",
        resource_template_uuid=RESOURCE_TEMPLATE_UUID,
        schema=revised_schema,
    )
    revised_snapshot = harness.catalog.replace(AUTHORITY, [revised_import])
    revised_template = revised_snapshot.node_templates[0]
    assert str(revised_template["uuid"]) == harness.simple_template_uuid
    harness.live_catalog.devices["robot"]["actions"]["move"] = {
        "type": "action",
        "schema": copy.deepcopy(revised_schema),
    }

    second = harness.client.post(
        "/api/v1/device-action-tasks",
        json=_request(
            harness,
            fingerprint=revised_snapshot.fingerprint,
            template_uuid=str(revised_template["uuid"]),
        ),
    )
    assert second.status_code == 201, second.text

    with harness.store.transaction() as connection:
        source = dict(
            connection.execute("SELECT * FROM device_action_system_source").fetchone()
        )
        workflow = dict(
            connection.execute(
                "SELECT * FROM workflow WHERE uuid = ?",
                (source["workflow_uuid"],),
            ).fetchone()
        )
        snapshots = {
            row["uuid"]: json.loads(row["workflow_snapshot"])
            for row in connection.execute(
                "SELECT uuid, workflow_snapshot FROM workflow_task"
            )
        }

    assert source["source_revision"] == workflow["revision"] == 2
    assert json.loads(source["contract_snapshot"])["output_contract"][
        "outputs"
    ][0]["name"] == "ok"
    first_task_uuid = first.json()["data"]["task_uuid"]
    second_task_uuid = second.json()["data"]["task_uuid"]
    assert snapshots[first_task_uuid]["workflow"]["revision"] == 1
    assert snapshots[first_task_uuid]["workflow"]["meta_data"]["unilab"][
        "output_contract"
    ]["outputs"][0]["name"] == "completed"
    assert snapshots[second_task_uuid]["workflow"]["revision"] == 2
    assert snapshots[second_task_uuid]["workflow"]["meta_data"]["unilab"][
        "output_contract"
    ]["outputs"][0]["name"] == "ok"


def test_unavailable_admission_returns_503_without_creating_facts(
    harness: Harness,
) -> None:
    harness.admission.available = False
    response = harness.client.post(
        "/api/v1/device-action-tasks",
        json=_request(harness),
    )

    _assert_error(response, status=503, code="admission_unavailable")
    assert _table_count(harness.store, "device_action_system_source") == 0
    assert _table_count(harness.store, "workflow_task") == 0
    assert _table_count(harness.store, "workflow_node_job") == 0
    assert harness.admission.wakes == []
