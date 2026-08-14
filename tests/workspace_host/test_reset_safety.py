from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from unilabos.workspace_host.discovery import ensure_local_token
from unilabos.workspace_host.host import WorkspaceHost
from unilabos.workspace_host.model import WorkspaceHostError, WorkspacePaths
from unilabos.workspace_host.reset_safety import inspect_local_reset_blockers
from unilabos.workspace_host.reset_safety import LocalResetBlocker


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "deployment" / "graphs").mkdir(parents=True)
    (root / "deployment" / "graphs" / "graph.json").write_text("{}\n")
    (root / "deployment" / "local_config.py").write_text("# fixture\n")
    return root


def _database(path: Path, schema: str) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(schema)
    return connection


def test_active_workflow_task_blocks_reset_before_processes_stop(
    workspace: Path,
) -> None:
    paths = WorkspacePaths.resolve(workspace)
    connection = _database(
        paths.runtime / "backend" / "local-domain" / "workflow_history.db",
        """
        CREATE TABLE workflow_task(
            uuid TEXT PRIMARY KEY, status TEXT NOT NULL, deleted_at TEXT,
            create_time TEXT NOT NULL
        );
        """,
    )
    connection.execute(
        "INSERT INTO workflow_task VALUES (?, ?, NULL, ?)",
        ("task-active", "running", "2026-08-14T00:00:00Z"),
    )
    connection.commit()
    connection.close()
    host = WorkspaceHost(paths, ensure_local_token(paths), readiness_timeout=0.1)
    stopped: list[str] = []
    host._stop_component = lambda name: stopped.append(name) or {}  # type: ignore[method-assign]

    with pytest.raises(WorkspaceHostError) as raised:
        host._dispatch("local.reset-state", {})

    assert raised.value.code == "local_reset_state_blocked"
    assert raised.value.details == {
        "stage": "before-stop",
        "blockers": [
            {
                "source": "local-domain",
                "kind": "workflow-task",
                "identity": "task-active",
                "status": "running",
            }
        ],
    }
    assert stopped == []
    assert '"event":"local.reset-state.blocked"' in paths.audit.read_text()
    host.close()


def test_unknown_edge_job_blocks_reset_with_command_identity(workspace: Path) -> None:
    paths = WorkspacePaths.resolve(workspace)
    connection = _database(
        paths.runtime / "backend" / "local-domain" / "edge_authority.db",
        """
        CREATE TABLE local_edge_job(
            job_uuid TEXT PRIMARY KEY, status TEXT NOT NULL,
            unknown_command_ids_json TEXT NOT NULL, updated_at REAL NOT NULL
        );
        """,
    )
    connection.execute(
        "INSERT INTO local_edge_job VALUES (?, ?, ?, ?)",
        ("job-unknown", "unknown", json.dumps(["plc-command-7"]), 1.0),
    )
    connection.commit()
    connection.close()

    blockers = inspect_local_reset_blockers(paths)

    assert [blocker.as_dict() for blocker in blockers] == [
        {
            "source": "local-edge-authority",
            "kind": "edge-job",
            "identity": "job-unknown",
            "status": "unknown",
            "unknownCommandIds": ["plc-command-7"],
        }
    ]


def test_pending_edge_outcome_and_unacknowledged_event_block_reset(
    workspace: Path,
) -> None:
    paths = WorkspacePaths.resolve(workspace)
    connection = _database(
        paths.runtime / "edge" / "edge_control.db",
        """
        CREATE TABLE edge_job_runtime(
            job_uuid TEXT PRIMARY KEY, status TEXT NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE edge_job_outcome_pending(
            job_uuid TEXT PRIMARY KEY, unknown_command_ids_json TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE edge_event_outbox(
            event_uuid TEXT PRIMARY KEY, type TEXT NOT NULL, created_at TEXT NOT NULL,
            acked_at REAL
        );
        """,
    )
    connection.execute(
        "INSERT INTO edge_job_runtime VALUES ('job-1', 'outcome_pending', 1.0)"
    )
    connection.execute(
        "INSERT INTO edge_job_outcome_pending VALUES ('job-1', '[]', 1.0)"
    )
    connection.execute(
        "INSERT INTO edge_event_outbox VALUES ('event-1', 'job.outcome_committed', 'now', NULL)"
    )
    connection.commit()
    connection.close()

    blockers = [blocker.as_dict() for blocker in inspect_local_reset_blockers(paths)]

    assert blockers == [
        {
            "source": "edge-runtime",
            "kind": "edge-event",
            "identity": "event-1",
            "status": "unacknowledged:job.outcome_committed",
        },
        {
            "source": "edge-runtime",
            "kind": "edge-job",
            "identity": "job-1",
            "status": "outcome_pending",
        },
        {
            "source": "edge-runtime",
            "kind": "edge-outcome",
            "identity": "job-1",
            "status": "outcome-pending",
        },
    ]


def test_second_check_restores_previously_ready_components_on_race(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = WorkspacePaths.resolve(workspace)
    host = WorkspaceHost(paths, ensure_local_token(paths), readiness_timeout=0.1)
    host._components["backend"]["phase"] = "ready"
    host._components["edge"]["phase"] = "ready"
    inspections = iter(
        [
            [],
            [
                LocalResetBlocker(
                    source="edge-runtime",
                    kind="edge-job",
                    identity="raced-job",
                    status="running",
                )
            ],
        ]
    )
    monkeypatch.setattr(
        "unilabos.workspace_host.host.inspect_local_reset_blockers",
        lambda _paths: next(inspections),
    )
    calls: list[str] = []
    host._stop_component = lambda name: calls.append(f"stop:{name}") or {}  # type: ignore[method-assign]
    host._start_backend = lambda parameters: calls.append("start:backend") or {}  # type: ignore[method-assign]
    host._start_edge = lambda: calls.append("start:edge") or {}  # type: ignore[method-assign]

    with pytest.raises(WorkspaceHostError) as raised:
        host._dispatch("local.reset-state", {})

    assert raised.value.code == "local_reset_state_blocked"
    assert raised.value.details["stage"] == "after-stop"
    assert calls == [
        "stop:edge",
        "stop:backend",
        "start:backend",
        "start:edge",
    ]
    host.close()


def test_unreadable_existing_state_fails_closed_without_stopping_components(
    workspace: Path,
) -> None:
    paths = WorkspacePaths.resolve(workspace)
    state_path = paths.runtime / "backend" / "local-domain" / "workflow_history.db"
    state_path.parent.mkdir(parents=True)
    state_path.write_bytes(b"not-a-sqlite-database")
    host = WorkspaceHost(paths, ensure_local_token(paths), readiness_timeout=0.1)
    stopped: list[str] = []
    host._stop_component = lambda name: stopped.append(name) or {}  # type: ignore[method-assign]

    with pytest.raises(WorkspaceHostError) as raised:
        host._dispatch("local.reset-state", {})

    assert raised.value.code == "local_reset_state_preflight_failed"
    assert raised.value.details["stage"] == "before-stop"
    assert stopped == []
    assert state_path.read_bytes() == b"not-a-sqlite-database"
    host.close()
