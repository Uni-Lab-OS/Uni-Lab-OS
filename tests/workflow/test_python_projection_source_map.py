"""Control nodes remain visible and addressable in generated Python."""

from unilabos.workflow.to_python_script import generate_python_revision


def test_generated_join_has_explicit_python_source_span() -> None:
    canonical = {
        "schema_version": "2",
        "revision_id": "source-map-v1",
        "workflow_id": "source-map-demo",
        "invocations": [
            {
                "node_id": "branch",
                "action_ref": "os_control.branch",
                "node_type": "branch",
                "input_bindings": {
                    "condition": {"kind": "literal", "value": True}
                },
            },
            {"node_id": "yes", "action_ref": "station.yes"},
            {"node_id": "no", "action_ref": "station.no"},
            {
                "node_id": "join",
                "action_ref": "os_control.join",
                "node_type": "join",
            },
            {"node_id": "finish", "action_ref": "station.finish"},
        ],
        "control_edges": [
            {"source": "branch", "target": "yes", "branch": "true"},
            {"source": "branch", "target": "no", "branch": "false"},
            {"source": "yes", "target": "join"},
            {"source": "no", "target": "join"},
            {"source": "join", "target": "finish"},
        ],
    }
    result = generate_python_revision(
        {
            "base_revision_id": "base-v1",
            "canonical_ir": canonical,
            "source_uri": "workflows/source-map-demo.py",
        },
        action_catalog={
            "station.yes": {"inputs": {}, "outputs": {}},
            "station.no": {"inputs": {}, "outputs": {}},
            "station.finish": {"inputs": {}, "outputs": {}},
        },
    )

    candidate = result["candidate"]
    assert candidate is not None
    assert "# join: join" in candidate["python_source"]
    assert {
        span["node_id"] for span in candidate["source_map"]
    } == {"branch", "yes", "no", "join", "finish"}
