"""Round R1A durable WorkflowTask command ingress contract tests."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore

_BODY_LIMIT = 8 * 1024 * 1024
_COMMAND_TABLE = "workflow_task_command"


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[WorkflowStore]:
    opened = WorkflowStore(tmp_path / "workflow.db")
    yield opened
    opened.close()


@pytest.fixture()
def service(store: WorkflowStore) -> WorkflowService:
    return WorkflowService(store)


@pytest.fixture()
def client(service: WorkflowService) -> Iterator[TestClient]:
    with TestClient(create_workflow_app(service)) as opened:
        yield opened


def _create_task(
    service: WorkflowService,
    *,
    run_mode: str = "normal",
) -> dict[str, Any]:
    workflow = service.create_workflow(
        name="R1A command ingress",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=str(uuid4()),
    )
    return service.create_workflow_task(
        workflow_uuid=workflow["uuid"],
        run_mode=run_mode,
        target_node_uuid=None,
        input_value={},
        description=None,
        meta_data={},
    )


def _create_command(
    service: WorkflowService,
    task_uuid: str,
    *,
    command_type: str,
    idempotency_key: str,
    target_node_uuid: str | None = None,
    description: str | None = None,
    meta_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return service.create_workflow_task_command(
        task_uuid,
        command_type=command_type,
        target_node_uuid=target_node_uuid,
        idempotency_key=idempotency_key,
        description=description,
        meta_data=meta_data,
    )


def _assert_error(response: Any, status: int, code: str) -> None:
    assert response.status_code == status
    payload = response.json()
    assert payload["code"] == status
    assert payload["error"]["code"] == code
    assert isinstance(payload["error"]["message"], str)
    assert payload["error"]["message"]
    assert set(payload) == {"code", "error"}


@pytest.mark.parametrize(
    ("command_type", "run_mode", "target_node_uuid"),
    [
        pytest.param(
            "step",
            "step",
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            id="step",
        ),
        pytest.param("pause", "normal", None, id="pause"),
        pytest.param("resume", "normal", None, id="resume"),
        pytest.param("cancel", "normal", None, id="cancel"),
    ],
)
def test_http_accepts_each_backend_command_and_returns_pending_read_dto(
    client: TestClient,
    service: WorkflowService,
    command_type: str,
    run_mode: str,
    target_node_uuid: str | None,
) -> None:
    task = _create_task(service, run_mode=run_mode)
    body: dict[str, Any] = {
        "type": command_type,
        "idempotency_key": f"toolbar-{command_type}-1",
        "description": "  operator request  ",
        "meta_data": {"source": "toolbar"},
    }
    if target_node_uuid is not None:
        body["target_node_uuid"] = target_node_uuid

    response = client.post(
        f"/api/v1/workflow-tasks/{task['uuid']}/commands",
        json=body,
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["code"] == 0
    assert set(payload) == {"code", "data"}
    command = payload["data"]
    expected_keys = {
        "uuid",
        "create_time",
        "update_time",
        "description",
        "meta_data",
        "workflow_task_uuid",
        "type",
        "idempotency_key",
        "status",
        "result",
        "trace_context",
    }
    if target_node_uuid is not None:
        expected_keys.add("target_node_uuid")
    assert set(command) == expected_keys
    assert UUID(command["uuid"]).version == 4
    datetime.fromisoformat(command["create_time"].replace("Z", "+00:00"))
    datetime.fromisoformat(command["update_time"].replace("Z", "+00:00"))
    assert command["workflow_task_uuid"] == task["uuid"]
    assert command["type"] == command_type
    assert command.get("target_node_uuid") == target_node_uuid
    assert command["idempotency_key"] == body["idempotency_key"]
    assert command["description"] == "operator request"
    assert command["meta_data"] == {"source": "toolbar"}
    assert command["status"] == "pending"
    assert command["result"] == {}
    assert command["trace_context"] == {}
    assert "consumed_at" not in command


def test_http_normalizes_null_object_and_ignores_unknown_request_field(
    client: TestClient,
    service: WorkflowService,
) -> None:
    task = _create_task(service)

    response = client.post(
        f"/api/v1/workflow-tasks/{task['uuid']}/commands",
        json={
            "type": "pause",
            "idempotency_key": "null-meta",
            "description": "   ",
            "meta_data": None,
            "unknown_backend_field": {"must": "be ignored"},
        },
    )

    assert response.status_code == 201
    command = response.json()["data"]
    assert command["meta_data"] == {}
    assert "description" not in command
    assert "unknown_backend_field" not in command


def test_same_key_replays_original_command_without_mutating_facts(
    service: WorkflowService,
    store: WorkflowStore,
) -> None:
    task = _create_task(service)
    created = _create_command(
        service,
        task["uuid"],
        command_type="pause",
        idempotency_key=" stable-key ",
        description="first",
        meta_data={"attempt": 1},
    )

    replayed = _create_command(
        service,
        task["uuid"],
        command_type="pause",
        idempotency_key="stable-key",
        description="must not replace first",
        meta_data={"attempt": 2},
    )

    assert replayed == created
    assert replayed["description"] == "first"
    assert replayed["meta_data"] == {"attempt": 1}
    assert store.count_rows(_COMMAND_TABLE) == 1
    assert store.get_task_command(created["uuid"]) == created
    assert store.get_task_command_by_key(task["uuid"], "stable-key") == created


@pytest.mark.parametrize(
    ("first_type", "second_type", "first_target", "second_target", "run_mode"),
    [
        pytest.param("pause", "resume", None, None, "normal", id="different-type"),
        pytest.param(
            "step",
            "step",
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "step",
            id="different-target",
        ),
    ],
)
def test_same_key_for_different_command_conflicts_without_partial_write(
    service: WorkflowService,
    store: WorkflowStore,
    first_type: str,
    second_type: str,
    first_target: str | None,
    second_target: str | None,
    run_mode: str,
) -> None:
    task = _create_task(service, run_mode=run_mode)
    _create_command(
        service,
        task["uuid"],
        command_type=first_type,
        target_node_uuid=first_target,
        idempotency_key="one-intent",
    )

    with pytest.raises(WorkflowError) as failure:
        _create_command(
            service,
            task["uuid"],
            command_type=second_type,
            target_node_uuid=second_target,
            idempotency_key="one-intent",
        )

    assert failure.value.code == "conflict"
    assert failure.value.status == 409
    assert store.count_rows(_COMMAND_TABLE) == 1


def test_command_and_idempotent_replay_survive_store_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "workflow.db"
    first_store = WorkflowStore(db_path)
    first_service = WorkflowService(first_store)
    task = _create_task(first_service)
    created = _create_command(
        first_service,
        task["uuid"],
        command_type="cancel",
        idempotency_key="restart-key",
    )
    first_store.close()

    reopened_store = WorkflowStore(db_path)
    try:
        reopened_service = WorkflowService(reopened_store)
        assert reopened_store.get_task_command(created["uuid"]) == created
        replayed = _create_command(
            reopened_service,
            task["uuid"],
            command_type="cancel",
            idempotency_key="restart-key",
        )
        assert replayed == created
        assert reopened_store.count_rows(_COMMAND_TABLE) == 1
    finally:
        reopened_store.close()


@pytest.mark.parametrize(
    "terminal_status", ["succeeded", "failed", "canceled", "timeout"]
)
def test_terminal_task_rejects_command_before_command_shape_validation(
    service: WorkflowService,
    store: WorkflowStore,
    terminal_status: str,
) -> None:
    task = _create_task(service)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE workflow_task SET status = ? WHERE uuid = ?",
            (terminal_status, task["uuid"]),
        )

    with pytest.raises(WorkflowError) as failure:
        _create_command(
            service,
            task["uuid"],
            command_type="unsupported",
            target_node_uuid="not-a-uuid",
            idempotency_key="terminal-first",
        )

    assert failure.value.code == "invalid_transition"
    assert failure.value.status == 409
    assert store.count_rows(_COMMAND_TABLE) == 0


def test_handler_rejects_invalid_target_uuid_before_task_lookup(
    client: TestClient,
    store: WorkflowStore,
) -> None:
    response = client.post(
        f"/api/v1/workflow-tasks/{uuid4()}/commands",
        json={
            "type": "unsupported",
            "target_node_uuid": "not-a-uuid",
            "idempotency_key": "handler-uuid-first",
        },
    )

    _assert_error(response, 400, "invalid_input")
    assert store.count_rows(_COMMAND_TABLE) == 0


def test_unknown_task_is_not_found_before_service_shape_validation(
    client: TestClient,
    store: WorkflowStore,
) -> None:
    response = client.post(
        f"/api/v1/workflow-tasks/{uuid4()}/commands",
        json={
            "type": "unsupported",
            "target_node_uuid": str(uuid4()),
            "idempotency_key": "unknown-task-first",
        },
    )

    _assert_error(response, 404, "not_found")
    assert store.count_rows(_COMMAND_TABLE) == 0


@pytest.mark.parametrize(
    ("run_mode", "command_type", "target_node_uuid", "key", "error_code", "status"),
    [
        pytest.param(
            "normal",
            "step",
            "not-a-uuid",
            "step-mode-first",
            "invalid_transition",
            409,
            id="step-requires-step-mode-before-uuid",
        ),
        pytest.param(
            "normal",
            "pause",
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "target-only-step",
            "invalid_input",
            400,
            id="target-only-for-step",
        ),
        pytest.param(
            "step",
            "step",
            "not-a-uuid",
            "bad-target",
            "invalid_input",
            400,
            id="invalid-target-uuid",
        ),
        pytest.param(
            "normal",
            "unsupported",
            None,
            "bad-type",
            "invalid_input",
            400,
            id="unknown-command-type",
        ),
        pytest.param(
            "normal",
            "pause",
            None,
            "   ",
            "invalid_input",
            400,
            id="blank-idempotency-key",
        ),
    ],
)
def test_service_rejects_invalid_command_without_partial_write(
    service: WorkflowService,
    store: WorkflowStore,
    run_mode: str,
    command_type: str,
    target_node_uuid: str | None,
    key: str,
    error_code: str,
    status: int,
) -> None:
    task = _create_task(service, run_mode=run_mode)

    with pytest.raises(WorkflowError) as failure:
        _create_command(
            service,
            task["uuid"],
            command_type=command_type,
            target_node_uuid=target_node_uuid,
            idempotency_key=key,
        )

    assert failure.value.code == error_code
    assert failure.value.status == status
    assert store.count_rows(_COMMAND_TABLE) == 0


def test_idempotency_key_limit_counts_utf8_bytes(
    service: WorkflowService,
    store: WorkflowStore,
) -> None:
    task = _create_task(service)
    accepted_key = "界" * 85
    rejected_key = "界" * 86
    assert len(accepted_key.encode()) == 255
    assert len(rejected_key.encode()) == 258

    accepted = _create_command(
        service,
        task["uuid"],
        command_type="pause",
        idempotency_key=accepted_key,
    )
    assert accepted["idempotency_key"] == accepted_key

    with pytest.raises(WorkflowError) as failure:
        _create_command(
            service,
            task["uuid"],
            command_type="resume",
            idempotency_key=rejected_key,
        )

    assert failure.value.code == "invalid_input"
    assert store.count_rows(_COMMAND_TABLE) == 1


def test_malformed_task_uuid_uses_backend_error_envelope(
    client: TestClient,
    store: WorkflowStore,
) -> None:
    response = client.post(
        "/api/v1/workflow-tasks/not-a-uuid/commands",
        json={"type": "pause", "idempotency_key": "bad-task-uuid"},
    )

    _assert_error(response, 400, "invalid_input")
    assert store.count_rows(_COMMAND_TABLE) == 0


def test_command_route_obeys_public_workflow_body_budget(
    client: TestClient,
    service: WorkflowService,
    store: WorkflowStore,
) -> None:
    task = _create_task(service)

    response = client.post(
        f"/api/v1/workflow-tasks/{task['uuid']}/commands",
        content=b"{}",
        headers={
            "content-type": "application/json",
            "content-length": str(_BODY_LIMIT + 1),
        },
    )

    _assert_error(response, 400, "invalid_input")
    assert store.count_rows(_COMMAND_TABLE) == 0


def test_store_schema_matches_frozen_backend_command_table(
    store: WorkflowStore,
) -> None:
    with store.transaction() as connection:
        columns = [
            row["name"]
            for row in connection.execute("PRAGMA table_info(workflow_task_command)")
        ]
        indexes = {
            row["name"]: row["partial"]
            for row in connection.execute("PRAGMA index_list(workflow_task_command)")
        }
        foreign_keys = list(
            connection.execute("PRAGMA foreign_key_list(workflow_task_command)")
        )

    assert columns == [
        "uuid",
        "create_time",
        "update_time",
        "deleted_at",
        "description",
        "meta_data",
        "workflow_task_uuid",
        "type",
        "target_node_uuid",
        "idempotency_key",
        "status",
        "result",
        "trace_context",
        "consumed_at",
    ]
    assert indexes["ux_workflow_task_command_idempotency_active"] == 1
    assert indexes["idx_workflow_task_command_pending"] == 1
    assert len(foreign_keys) == 1
    assert foreign_keys[0]["table"] == "workflow_task"
    assert foreign_keys[0]["from"] == "workflow_task_uuid"
    assert foreign_keys[0]["to"] == "uuid"
    assert foreign_keys[0]["on_delete"] == "CASCADE"


def _insert_raw_command(
    connection: sqlite3.Connection,
    *,
    command_uuid: str,
    task_uuid: str,
    command_type: str = "pause",
    key: str = "raw-key",
    status: str = "pending",
    meta_data: str = "{}",
    result: str = "{}",
    trace_context: str = "{}",
) -> None:
    connection.execute(
        """
        INSERT INTO workflow_task_command(
            uuid, create_time, update_time, meta_data, workflow_task_uuid,
            type, idempotency_key, status, result, trace_context
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            command_uuid,
            "2026-08-01T00:00:00Z",
            "2026-08-01T00:00:00Z",
            meta_data,
            task_uuid,
            command_type,
            key,
            status,
            result,
            trace_context,
        ),
    )


@pytest.mark.parametrize(
    ("overrides", "case"),
    [
        pytest.param({"command_type": "skip"}, "type-check", id="type-check"),
        pytest.param({"status": "running"}, "status-check", id="status-check"),
        pytest.param({"meta_data": "{"}, "meta-json", id="meta-json-check"),
        pytest.param({"result": "["}, "result-json", id="result-json-check"),
        pytest.param(
            {"trace_context": "not-json"},
            "trace-json",
            id="trace-json-check",
        ),
    ],
)
def test_store_schema_rejects_invalid_command_rows(
    service: WorkflowService,
    store: WorkflowStore,
    overrides: dict[str, str],
    case: str,
) -> None:
    task = _create_task(service)

    with pytest.raises(sqlite3.IntegrityError, match=r"CHECK constraint failed"):
        with store.transaction() as connection:
            _insert_raw_command(
                connection,
                command_uuid=str(uuid4()),
                task_uuid=task["uuid"],
                key=case,
                **overrides,
            )


def test_store_enforces_active_idempotency_unique_key(
    service: WorkflowService,
    store: WorkflowStore,
) -> None:
    task = _create_task(service)
    with store.transaction() as connection:
        _insert_raw_command(
            connection,
            command_uuid=str(uuid4()),
            task_uuid=task["uuid"],
            key="duplicate-key",
        )

    with pytest.raises(sqlite3.IntegrityError, match=r"UNIQUE constraint failed"):
        with store.transaction() as connection:
            _insert_raw_command(
                connection,
                command_uuid=str(uuid4()),
                task_uuid=task["uuid"],
                key="duplicate-key",
            )


def test_store_enforces_command_task_foreign_key(store: WorkflowStore) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=r"FOREIGN KEY constraint failed"):
        with store.transaction() as connection:
            _insert_raw_command(
                connection,
                command_uuid=str(uuid4()),
                task_uuid=str(uuid4()),
                key="orphan-command",
            )
