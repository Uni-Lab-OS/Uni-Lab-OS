"""EdgeScheduler keeps one canonical WorkflowTask Material coordination seam."""

from __future__ import annotations

from pathlib import Path

import pytest

from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.models import WorkflowSpec, spec_from_dict
from unilabos.app.scheduler.service import EdgeScheduler


def _spec(*, with_retired_material_shape: bool) -> WorkflowSpec:
    node = {
        "id": "node-a",
        "device_id": "device-a",
        "action_name": "run",
        "action_args": {},
    }
    if with_retired_material_shape:
        node["material_requirements"] = [
            {
                "lot_id": "lot-a",
                "quantity": 1.0,
            }
        ]
    return spec_from_dict(
        {
            "workflow_id": "workflow-a",
            "nodes": [node],
            "edges": [],
        }
    )


def test_legacy_workflow_material_shape_fails_closed_before_dispatch() -> None:
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)

    with pytest.raises(ValueError, match="WorkflowTask Material admission"):
        scheduler.submit_workflow(_spec(with_retired_material_shape=True))

    assert dispatcher.dispatched == []
    assert scheduler.snapshot()["workflows"] == {}


def test_plain_legacy_dag_still_uses_the_single_scheduler_engine() -> None:
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)

    result = scheduler.submit_workflow(_spec(with_retired_material_shape=False))

    assert result["state"] == "running"
    assert len(result["dispatched"]) == 1
    assert len(dispatcher.dispatched) == 1


def test_workflow_task_dispatch_proof_fails_closed_without_both_authorities() -> None:
    scheduler = EdgeScheduler()

    assert (
        scheduler.can_dispatch_task_materials("70000000-0000-4000-8000-000000000901")
        is False
    )


def test_scheduler_composition_uses_only_workspace_inventory_database(
    tmp_path: Path,
) -> None:
    from unilabos.app.scheduler import integration

    try:
        integration.setup_edge_scheduler(
            working_dir=str(tmp_path),
            host_node_getter=lambda: None,
            device_state_db_path="off",
            workflow_history_db_path="off",
        )
        assert (tmp_path / "inventory.db").is_file()
        assert sorted(path.name for path in tmp_path.glob("*.db")) == ["inventory.db"]
    finally:
        integration.reset_for_test()
