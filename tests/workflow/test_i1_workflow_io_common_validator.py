"""I1 transport-independent Workflow I/O public validation tracer."""

from __future__ import annotations

from copy import deepcopy

import pytest

from unilabos.workflow.candidate_validation import (
    CandidateBundleError,
    validate_candidate_bundle,
)

WORKFLOW_UUID = "10000000-0000-4000-8000-000000000001"
TIMESTAMP = "2026-08-01T00:00:00Z"

EMPTY_IO = {
    "input_contract": {"version": 1, "parameters": []},
    "output_contract": {"version": 1, "outputs": []},
    "output_bindings": {},
}


def _empty_graph() -> dict[str, object]:
    return {
        "workflow": {
            "uuid": WORKFLOW_UUID,
            "create_time": TIMESTAMP,
            "update_time": TIMESTAMP,
            "meta_data": {"unilab": deepcopy(EMPTY_IO)},
            "name": "I1 output contract tracer",
            "tags": [],
            "revision": 1,
            "description": None,
        },
        "nodes": [],
        "edges": [],
        "node_templates": [],
        "handle_templates": [],
    }


def test_candidate_rejects_declared_output_without_its_root_binding() -> None:
    """Core #154 requires every explicit output name to have one root binding."""

    applied_graph = _empty_graph()
    candidate_graph = deepcopy(applied_graph)
    candidate_graph["workflow"]["meta_data"]["unilab"]["output_contract"] = {
        "version": 1,
        "outputs": [
            {
                "name": "final_sample",
                "schema": {"$slot": "ResourceSlot"},
                "title": "Final sample",
                "description": "",
                "implicit": False,
            }
        ],
    }

    with pytest.raises(CandidateBundleError):
        validate_candidate_bundle(
            graph=candidate_graph,
            base_graph=applied_graph,
            workflow_uuid=WORKFLOW_UUID,
            revision=1,
            source_map=[],
            changeset={
                "kind": "graph",
                "created_node_uuids": [],
                "updated_node_uuids": [],
                "deleted_node_uuids": [],
                "created_edge_uuids": [],
                "updated_edge_uuids": [],
                "deleted_edge_uuids": [],
                "reserved_metadata_changed": True,
            },
            require_unchanged_graph=False,
        )
