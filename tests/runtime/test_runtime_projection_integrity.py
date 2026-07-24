"""Runtime projection integrity contracts.

The UI projection is read-only, but its identity and dependency view must still
describe the exact Canonical content that RuntimeService compiled and dispatched.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from unilabos.runtime.service import RuntimeService
from unilabos.scheduler.dag_model import TaskDag
from unilabos.workflow.canonical import WorkflowRevision


class RecordingSchedule:
    def __init__(self) -> None:
        self.submitted: list[TaskDag] = []

    def on_job_status(self, _callback: Any) -> None:
        return None

    async def submit_dag(self, dag: TaskDag) -> Any:
        self.submitted.append(dag)
        return type("RunHandle", (), {"dag": dag})()

    def get_run(self, _task_id: str) -> None:
        return None


def _git_blob_hash(text: str) -> str:
    payload = text.encode("utf-8")
    header = f"blob {len(payload)}\0".encode()
    return hashlib.sha1(header + payload, usedforsecurity=False).hexdigest()


def test_projection_content_hash_matches_authoritatively_materialized_task_dag() -> None:
    """Catalog materialization must not make display and execution hashes diverge."""

    schedule = RecordingSchedule()
    service = RuntimeService(
        schedule,
        action_catalog={
            "station.measure": {
                "inputs": {},
                "outputs": {"reading": {"type": "number"}},
                "contract": {
                    "resource_claims": [
                        {"resource_ref": "station/device", "mode": "exclusive"}
                    ],
                    "effects": [{"kind": "measurement_recorded"}],
                    "timing": {"estimated_duration_s": 4.5},
                },
            }
        },
    )
    source_payload = {
        "schema_version": "2",
        "revision_id": "author-revision-1",
        "workflow_id": "authoritative-contract-workflow",
        "invocations": [
            {
                "node_id": "measure",
                "action_ref": "station.measure",
                # These author-side fields are intentionally stale.  The
                # catalog is authoritative at compile time.
                "output_schema": {},
                "resource_claims": [],
                "effects": [],
                "estimated_duration_s": 0,
            }
        ],
    }

    asyncio.run(
        service.start_run(
            {
                "source": {
                    "format": "canonical_workflow_v2",
                    "payload": source_payload,
                }
            }
        )
    )

    projected_hash = service.get_workflow()["revision"]["contentHash"]
    dispatched_hash = schedule.submitted[0].workflow_revision_hash
    assert projected_hash == dispatched_hash


def test_projection_carries_generic_source_artifact_metadata() -> None:
    source_text = (
        "schema: example.workflow/v1\nname: generic-source-workflow\n"
    )
    source_hash = _git_blob_hash(source_text)
    revision = WorkflowRevision.model_validate(
        {
            "schema_version": "2",
            "revision_id": "source-artifact-1",
            "workflow_id": "generic-source-workflow",
            "invocations": [
                {
                    "node_id": "measure",
                    "action_ref": "station.measure",
                }
            ],
            "source_artifact": {
                "format": "example.workflow/v1",
                "text": source_text,
                "uri": "workflows/generic-source-workflow.yaml",
                "content_hash": source_hash,
            },
        }
    )
    service = RuntimeService(RecordingSchedule())

    service.set_workflow_revision(revision)

    assert service.get_workflow()["revision"]["sourceArtifact"] == {
        "format": "example.workflow/v1",
        "text": source_text,
        "uri": "workflows/generic-source-workflow.yaml",
        "contentHash": source_hash,
    }


def _all_dependency_kinds_revision() -> WorkflowRevision:
    return WorkflowRevision.model_validate(
        {
            "schema_version": "2",
            "revision_id": "all-dependencies-1",
            "workflow_id": "all-dependencies",
            "invocations": [
                {
                    "node_id": "produce",
                    "action_ref": "sensor.produce",
                    "node_type": "branch",
                },
                {
                    "node_id": "route",
                    "action_ref": "router.route",
                    "input_bindings": {
                        "reading": {
                            "kind": "node_output",
                            "node_id": "produce",
                            "output": "reading",
                        }
                    },
                },
                {"node_id": "move", "action_ref": "transport.move"},
                {
                    "node_id": "finish",
                    "action_ref": "station.finish",
                    "input_bindings": {
                        # Binding-only dependency: no matching DataEdge.
                        "audit_value": {
                            "kind": "node_output",
                            "node_id": "produce",
                            "output": "audit_value",
                        }
                    },
                },
            ],
            "control_edges": [
                {
                    "edge_id": "control-produce-route",
                    "source": "produce",
                    "target": "route",
                    "branch": "ready",
                }
            ],
            "data_edges": [
                {
                    "edge_id": "data-produce-route",
                    "source": "produce",
                    "source_output": "reading",
                    "target": "route",
                    "target_input": "reading",
                }
            ],
            "material_edges": [
                {
                    "edge_id": "material-route-move",
                    "source": "route",
                    "source_port": "plate_out",
                    "target": "move",
                    "target_port": "plate_in",
                    "material_ref": "plate-A",
                }
            ],
            "constraint_edges": [
                {
                    "edge_id": "constraint-move-finish",
                    "source": "move",
                    "target": "finish",
                    "constraint_type": "finish_after",
                }
            ],
        }
    )


def test_visual_projection_contains_every_canonical_dependency_kind() -> None:
    service = RuntimeService(RecordingSchedule())
    service.set_workflow_revision(_all_dependency_kinds_revision())

    edges = service.get_workflow()["revision"]["edges"]
    edge_pairs = {(edge["source"], edge["target"]) for edge in edges}

    assert edge_pairs == {
        ("produce", "route"),  # control + data + matching NodeOutputRef
        ("route", "move"),  # material
        ("move", "finish"),  # constraint
        ("produce", "finish"),  # binding-only NodeOutputRef
    }


def test_visual_projection_deduplicates_pairs_and_retains_control_branch() -> None:
    service = RuntimeService(RecordingSchedule())
    service.set_workflow_revision(_all_dependency_kinds_revision())

    edges = service.get_workflow()["revision"]["edges"]
    matching = [
        edge
        for edge in edges
        if edge["source"] == "produce" and edge["target"] == "route"
    ]

    assert matching == [
        {"source": "produce", "target": "route", "branch": "ready"}
    ]
