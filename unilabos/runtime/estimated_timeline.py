"""Topology-only estimate and event-only observed timeline projections."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

from unilabos.scheduler.dag_model import TaskDag


@dataclass(frozen=True)
class EstimatedTimelineItem:
    node_id: str
    earliest_start_offset_s: float
    estimated_end_offset_s: float


@dataclass(frozen=True)
class EstimatedTimeline:
    items: tuple[EstimatedTimelineItem, ...]
    workflow_revision_hash: str
    is_resource_constrained: bool = False
    basis: str = "dag_topology+action_duration"


class EstimatedTimelineBuilder:
    def build(self, dag: TaskDag) -> EstimatedTimeline:
        predecessors: dict[str, list[str]] = {node_id: [] for node_id in dag.nodes}
        successors = dag.adjacency()
        indegree = dag.build_indegree()
        for edge in dag.edges:
            predecessors[edge.target_node_uuid].append(edge.source_node_uuid)
        ready = [node_id for node_id in dag.nodes if indegree[node_id] == 0]
        ends: dict[str, float] = {}
        items: list[EstimatedTimelineItem] = []
        while ready:
            node_id = ready.pop(0)
            start = max((ends[pred] for pred in predecessors[node_id]), default=0.0)
            end = start + dag.nodes[node_id].estimated_duration_s
            ends[node_id] = end
            items.append(
                EstimatedTimelineItem(
                    node_id=node_id,
                    earliest_start_offset_s=start,
                    estimated_end_offset_s=end,
                )
            )
            for successor in successors[node_id]:
                indegree[successor] -= 1
                if indegree[successor] == 0:
                    ready.append(successor)
        return EstimatedTimeline(
            items=tuple(items),
            workflow_revision_hash=dag.workflow_revision_hash,
        )


@dataclass(frozen=True)
class ObservedGanttItem:
    node_id: str
    started_at: float
    ended_at: Optional[float]
    terminal: Optional[str]
    source: str = "run_events"


@dataclass(frozen=True)
class ObservedGantt:
    items: tuple[ObservedGanttItem, ...]


def _field(event: Any, name: str) -> Any:
    return event.get(name) if isinstance(event, dict) else getattr(event, name)


class ObservedGanttBuilder:
    def build(self, events: Iterable[Any]) -> ObservedGantt:
        started: dict[str, float] = {}
        completed: dict[str, tuple[float, str]] = {}
        order: list[str] = []
        for event in events:
            event_type = _field(event, "type")
            node_id = _field(event, "node_id")
            timestamp = _field(event, "timestamp")
            if event_type == "node_started" and node_id is not None:
                started[node_id] = timestamp
                if node_id not in order:
                    order.append(node_id)
            elif event_type in {
                "node_succeeded",
                "node_failed",
                "node_cancelled",
                "node_skipped",
            }:
                if node_id is not None and node_id in started:
                    completed[node_id] = (timestamp, event_type.removeprefix("node_"))
        return ObservedGantt(
            items=tuple(
                ObservedGanttItem(
                    node_id=node_id,
                    started_at=started[node_id],
                    ended_at=completed.get(node_id, (None, None))[0],
                    terminal=completed.get(node_id, (None, None))[1],
                )
                for node_id in order
            )
        )
