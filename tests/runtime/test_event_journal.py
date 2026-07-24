"""Quick Debug Alpha SQLite event-journal atomicity tests."""

from __future__ import annotations

import importlib
from types import ModuleType

import pytest


def _api() -> ModuleType:
    try:
        return importlib.import_module("unilabos.runtime.event_store")
    except ModuleNotFoundError as exc:
        if not "unilabos.runtime.event_store".startswith(f"{exc.name}.") and exc.name != (
            "unilabos.runtime.event_store"
        ):
            raise
        pytest.fail("SQLite RunEvent journal capability is missing", pytrace=False)


def test_terminal_result_effect_cursor_and_outbox_commit_atomically(tmp_path) -> None:
    api = _api()
    journal = api.SQLiteEventJournal(tmp_path / "runtime.sqlite", runtime_epoch="epoch-1")

    journal.commit_node_terminal(
        run_id="run-1",
        node_id="measure",
        terminal="succeeded",
        result={"mass": 1.25},
        effects=[{"op": "move", "material_id": "plate-1", "to": "tank-1"}],
        cursor={"completed": ["measure"]},
        outbox=[{"topic": "run-events", "event": "node_succeeded"}],
    )

    projection = journal.load_node_projection("run-1", "measure")
    assert projection.terminal == "succeeded"
    assert projection.result == {"mass": 1.25}
    assert projection.effects[0]["material_id"] == "plate-1"
    assert journal.load_cursor("run-1") == {"completed": ["measure"]}
    assert [item.event for item in journal.list_outbox("run-1")] == ["node_succeeded"]


def test_atomic_terminal_commit_rolls_back_every_projection_on_failure(tmp_path) -> None:
    api = _api()
    journal = api.SQLiteEventJournal(tmp_path / "runtime.sqlite", runtime_epoch="epoch-1")
    journal.failpoint = "after_result_before_effect"

    with pytest.raises(api.JournalCommitError):
        journal.commit_node_terminal(
            run_id="run-1",
            node_id="develop",
            terminal="succeeded",
            result={"tank_id": 1},
            effects=[{"op": "move", "material_id": "plate-1", "to": "tank-1"}],
            cursor={"completed": ["develop"]},
            outbox=[{"topic": "run-events", "event": "node_succeeded"}],
        )

    assert journal.load_node_projection("run-1", "develop") is None
    assert journal.load_cursor("run-1") is None
    assert journal.list_outbox("run-1") == []
