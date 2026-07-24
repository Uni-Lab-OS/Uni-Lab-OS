"""Quick Debug Alpha Canonical schema and execution-hash contracts."""

from __future__ import annotations

import hashlib
import importlib
from types import ModuleType

import pytest
from hypothesis import given, strategies as st


def _canonical_api() -> ModuleType:
    try:
        return importlib.import_module("unilabos.workflow.canonical")
    except ModuleNotFoundError as exc:
        if exc.name != "unilabos.workflow.canonical":
            raise
        pytest.fail(
            "Canonical WorkflowRevision capability is missing: add "
            "unilabos.workflow.canonical",
            pytrace=False,
        )


def _bindings_api() -> ModuleType:
    try:
        return importlib.import_module("unilabos.workflow.bindings")
    except ModuleNotFoundError as exc:
        if exc.name != "unilabos.workflow.bindings":
            raise
        pytest.fail(
            "Canonical tagged binding capability is missing: add "
            "unilabos.workflow.bindings",
            pytrace=False,
        )


def _dag_compile_api() -> ModuleType:
    return importlib.import_module("unilabos.workflow.dag_compile")


def _git_blob_hash(text: str) -> str:
    payload = text.encode("utf-8")
    header = f"blob {len(payload)}\0".encode()
    return hashlib.sha1(header + payload, usedforsecurity=False).hexdigest()


def _revision_payload(
    *, literal: object, layout: dict[str, object]
) -> dict[str, object]:
    return {
        "schema_version": "2",
        "revision_id": "rev-001",
        "workflow_id": "wf-quick-debug",
        "invocations": [
            {
                "node_id": "measure",
                "action_ref": "balance.measure",
                "input_bindings": {"sample": {"kind": "literal", "value": literal}},
                "output_schema": {"mass": {"type": "number"}},
            },
            {
                "node_id": "dose",
                "action_ref": "pump.dose",
                "input_bindings": {
                    "amount": {
                        "kind": "node_output",
                        "node_id": "measure",
                        "output": "mass",
                    }
                },
                "output_schema": {},
            },
        ],
        "control_edges": [{"source": "measure", "target": "dose"}],
        "layout": layout,
    }


def _labeled_revision_payload(
    *,
    measure_id: str,
    dose_id: str,
    edge_id: str,
) -> dict[str, object]:
    payload = _revision_payload(literal="sample-1", layout={})
    invocations = payload["invocations"]
    assert isinstance(invocations, list)
    invocations[0]["node_id"] = measure_id
    invocations[1]["node_id"] = dose_id
    invocations[1]["input_bindings"]["amount"]["node_id"] = measure_id
    control_edges = payload["control_edges"]
    assert isinstance(control_edges, list)
    control_edges[0] = {
        "edge_id": edge_id,
        "source": measure_id,
        "target": dose_id,
    }
    return payload


@pytest.mark.parametrize(
    ("class_name", "kwargs", "tag"),
    [
        ("LiteralValue", {"value": {"amount": 3.5, "unit": "mL"}}, "literal"),
        (
            "RuntimeParameterRef",
            {"parameter": "target_volume", "default": 2.0},
            "runtime_parameter",
        ),
        (
            "NodeOutputRef",
            {"node_id": "measure", "output": "mass"},
            "node_output",
        ),
    ],
)
def test_tagged_binding_round_trip(
    class_name: str, kwargs: dict[str, object], tag: str
) -> None:
    api = _bindings_api()
    model_class = getattr(api, class_name)
    value = model_class(**kwargs)
    payload = value.model_dump(mode="json")
    restored = model_class.model_validate_json(value.model_dump_json())

    assert payload["kind"] == tag
    assert restored == value


def test_layout_only_change_does_not_change_execution_content_hash() -> None:
    api = _canonical_api()
    left = api.WorkflowRevision.model_validate(
        _revision_payload(literal="sample-1", layout={"measure": {"x": 0, "y": 0}})
    )
    right = api.WorkflowRevision.model_validate(
        _revision_payload(literal="sample-1", layout={"measure": {"x": 900, "y": 200}})
    )

    assert left.content_hash == right.content_hash


def test_edge_id_label_does_not_change_execution_content_hash() -> None:
    api = _canonical_api()
    left = api.WorkflowRevision.model_validate(
        _labeled_revision_payload(
            measure_id="measure",
            dose_id="dose",
            edge_id="authoring-edge-a",
        )
    )
    right = api.WorkflowRevision.model_validate(
        _labeled_revision_payload(
            measure_id="measure",
            dose_id="dose",
            edge_id="authoring-edge-b",
        )
    )

    assert left.content_hash == right.content_hash


def test_normalized_node_labels_share_task_dag_idempotency_identity() -> None:
    canonical = _canonical_api()
    dag_compile = _dag_compile_api()
    left = canonical.WorkflowRevision.model_validate(
        _labeled_revision_payload(
            measure_id="yaml-measure",
            dose_id="yaml-dose",
            edge_id="authoring-edge",
        )
    )
    right = canonical.WorkflowRevision.model_validate(
        _labeled_revision_payload(
            measure_id="python-measure",
            dose_id="python-dose",
            edge_id="authoring-edge",
        )
    )
    action_catalog = {
        "balance.measure": {
            "inputs": {"sample": {"type": "string", "required": True}},
            "outputs": {"mass": {"type": "number"}},
        },
        "pump.dose": {
            "inputs": {"amount": {"type": "number", "required": True}},
            "outputs": {},
        },
    }

    assert left.content_hash == right.content_hash
    left_dag = dag_compile.compile_workflow_revision(
        left,
        task_id="left-run",
        action_catalog=action_catalog,
    )
    right_dag = dag_compile.compile_workflow_revision(
        right,
        task_id="right-run",
        action_catalog=action_catalog,
    )
    left_nodes = sorted(left_dag.nodes.values(), key=lambda node: node.canonical_index)
    right_nodes = sorted(
        right_dag.nodes.values(), key=lambda node: node.canonical_index
    )

    assert [node.source_node_id for node in left_nodes] != [
        node.source_node_id for node in right_nodes
    ]
    assert [node.idempotency_key for node in left_nodes] == [
        node.idempotency_key for node in right_nodes
    ]


def test_source_artifact_round_trip_does_not_change_execution_content_hash() -> None:
    api = _canonical_api()
    payload = _revision_payload(literal="sample-1", layout={})
    source_text = "schema: example.workflow/v1\nname: quick-debug\n"
    source_hash = _git_blob_hash(source_text)
    edited_text = "schema: example.workflow/v2\nname: edited\n"
    without_artifact = api.WorkflowRevision.model_validate(payload)
    with_artifact = api.WorkflowRevision.model_validate(
        {
            **payload,
            "source_artifact": {
                "format": "example.workflow/v1",
                "text": source_text,
                "uri": "workflows/quick-debug.yaml",
                "content_hash": source_hash,
            },
        }
    )
    restored = api.WorkflowRevision.model_validate_json(with_artifact.model_dump_json())
    changed_artifact = with_artifact.model_copy(
        update={
            "source_artifact": api.WorkflowSourceArtifact(
                format="example.workflow/v2",
                text=edited_text,
                uri="workflows/edited.yaml",
                content_hash=_git_blob_hash(edited_text),
            )
        }
    )

    assert restored.source_artifact == with_artifact.source_artifact
    assert restored.source_artifact.model_dump(mode="json") == {
        "format": "example.workflow/v1",
        "text": source_text,
        "uri": "workflows/quick-debug.yaml",
        "content_hash": source_hash,
    }
    assert without_artifact.content_hash == with_artifact.content_hash
    assert changed_artifact.content_hash == with_artifact.content_hash


def test_execution_change_changes_content_hash() -> None:
    api = _canonical_api()
    left = api.WorkflowRevision.model_validate(
        _revision_payload(literal="sample-1", layout={})
    )
    right = api.WorkflowRevision.model_validate(
        _revision_payload(literal="sample-2", layout={})
    )

    assert left.content_hash != right.content_hash


@given(
    st.dictionaries(
        keys=st.text(min_size=1, max_size=8),
        values=st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=12)),
        max_size=6,
    )
)
def test_content_hash_is_deterministic_for_equivalent_mapping_order(
    literal: dict[str, object],
) -> None:
    api = _canonical_api()
    reversed_literal = dict(reversed(list(literal.items())))
    first = api.WorkflowRevision.model_validate(
        _revision_payload(literal=literal, layout={"measure": {"x": 1}})
    )
    second = api.WorkflowRevision.model_validate(
        _revision_payload(literal=reversed_literal, layout={"measure": {"x": 2}})
    )

    assert first.content_hash == second.content_hash
