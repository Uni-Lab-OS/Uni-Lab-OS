"""Quick Debug Alpha EstimatedTimeline and ObservedGantt semantics."""

from __future__ import annotations

import importlib

import pytest

from unilabos.scheduler.dag_model import TaskDag


def _timeline_api():
    try:
        return importlib.import_module("unilabos.runtime.estimated_timeline")
    except ModuleNotFoundError as exc:
        if not "unilabos.runtime.estimated_timeline".startswith(f"{exc.name}.") and exc.name != (
            "unilabos.runtime.estimated_timeline"
        ):
            raise
        pytest.fail("EstimatedTimeline capability is missing", pytrace=False)


def _dag() -> TaskDag:
    return TaskDag.from_message(
        {
            "task_id": "timeline",
            "workflow_revision_hash": "sha256:revision",
            "nodes": [
                {"node_id": "a", "device_id": "same", "action": "a", "estimated_duration_s": 2},
                {"node_id": "b", "device_id": "same", "action": "b", "estimated_duration_s": 5},
                {"node_id": "c", "device_id": "same", "action": "c", "estimated_duration_s": 3},
                {"node_id": "d", "device_id": "same", "action": "d", "estimated_duration_s": 1},
            ],
            "edges": [
                {"source_node_uuid": "a", "target_node_uuid": "b"},
                {"source_node_uuid": "a", "target_node_uuid": "c"},
                {"source_node_uuid": "b", "target_node_uuid": "d"},
                {"source_node_uuid": "c", "target_node_uuid": "d"},
            ],
        }
    )


def test_estimated_timeline_uses_topology_only_and_never_claims_resource_guarantee() -> None:
    api = _timeline_api()
    timeline = api.EstimatedTimelineBuilder().build(_dag())
    items = {item.node_id: item for item in timeline.items}

    assert (items["a"].earliest_start_offset_s, items["a"].estimated_end_offset_s) == (0, 2)
    assert (items["b"].earliest_start_offset_s, items["b"].estimated_end_offset_s) == (2, 7)
    assert (items["c"].earliest_start_offset_s, items["c"].estimated_end_offset_s) == (2, 5)
    assert (items["d"].earliest_start_offset_s, items["d"].estimated_end_offset_s) == (7, 8)
    # b/c intentionally share a device: the estimate still overlaps because it is not a Plan.
    assert timeline.is_resource_constrained is False
    assert timeline.basis == "dag_topology+action_duration"
    assert timeline.workflow_revision_hash == "sha256:revision"


def test_observed_gantt_is_projected_only_from_real_run_events() -> None:
    api = _timeline_api()
    events = [
        {"type": "estimated_timeline_created", "node_id": "a", "timestamp": 1.0},
        {"type": "node_started", "node_id": "a", "timestamp": 10.0},
        {"type": "node_succeeded", "node_id": "a", "timestamp": 14.5},
    ]
    observed = api.ObservedGanttBuilder().build(events)
    assert len(observed.items) == 1
    assert observed.items[0].node_id == "a"
    assert observed.items[0].started_at == 10.0
    assert observed.items[0].ended_at == 14.5
    assert observed.items[0].source == "run_events"
