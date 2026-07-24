"""Quick Debug Alpha restart/reconcile safety contracts."""

from __future__ import annotations

import importlib

import pytest


def _event_store():
    try:
        return importlib.import_module("unilabos.runtime.event_store")
    except ModuleNotFoundError as exc:
        if not "unilabos.runtime.event_store".startswith(f"{exc.name}.") and exc.name != (
            "unilabos.runtime.event_store"
        ):
            raise
        pytest.fail("SQLite RunEvent journal capability is missing", pytrace=False)


def test_restart_moves_running_node_to_reconciling_without_dispatch(tmp_path) -> None:
    api = _event_store()
    path = tmp_path / "runtime.sqlite"
    before = api.SQLiteEventJournal(path, runtime_epoch="epoch-old")
    before.record_node_started(run_id="run-1", node_id="develop", attempt=1)
    before.close()

    dispatched: list[str] = []
    after = api.SQLiteEventJournal(path, runtime_epoch="epoch-new")
    recovery = after.reconcile_restart("run-1", dispatch=dispatched.append)

    assert recovery.nodes["develop"].state == "reconciling"
    assert recovery.nodes["develop"].attempt == 1
    assert dispatched == []
    assert after.runtime_epoch == "epoch-new"


def test_stale_epoch_callback_cannot_change_new_runtime_state(tmp_path) -> None:
    api = _event_store()
    journal = api.SQLiteEventJournal(tmp_path / "runtime.sqlite", runtime_epoch="epoch-new")
    accepted = journal.record_callback(
        run_id="run-1",
        node_id="develop",
        runtime_epoch="epoch-old",
        terminal="succeeded",
        result={"tank_id": 1},
    )
    assert accepted is False
    assert journal.load_node_projection("run-1", "develop") is None
