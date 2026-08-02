"""I1 transport-independent Workflow I/O public validation tracer."""

from __future__ import annotations

from copy import deepcopy

import pytest

from unilabos.workflow.candidate_validation import (
    CandidateBundleError,
    validate_candidate_bundle,
)

WORKFLOW_UUID = "10000000-0000-4000-8000-000000000001"
NODE_UUID = "10000000-0000-4000-8000-000000000002"
FOREIGN_NODE_UUID = "10000000-0000-4000-8000-000000000003"
NODE_TEMPLATE_UUID = "10000000-0000-4000-8000-000000000004"
RESOURCE_TEMPLATE_UUID = "10000000-0000-4000-8000-000000000005"
SOURCE_HANDLE_UUID = "10000000-0000-4000-8000-000000000006"
TARGET_HANDLE_UUID = "10000000-0000-4000-8000-000000000007"
RESOURCE_TEMPLATE_A_UUID = "10000000-0000-4000-8000-000000000008"
RESOURCE_TEMPLATE_B_UUID = "10000000-0000-4000-8000-000000000009"
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


def _handle(
    *,
    handle_uuid: str,
    io_type: str,
    value_schema: dict[str, object],
    value_type: str,
) -> dict[str, object]:
    unilab: dict[str, object] = {"value_schema": deepcopy(value_schema)}
    base_schema = value_schema
    if "anyOf" in value_schema:
        base_schema = value_schema["anyOf"][0]
    if base_schema.get("$slot") == "ResourceSlot":
        unilab["allowed_resource_template_uuids"] = base_schema.get(
            "allowed_resource_template_uuids"
        )
    return {
        "uuid": handle_uuid,
        "create_time": TIMESTAMP,
        "update_time": TIMESTAMP,
        "meta_data": {"unilab": unilab},
        "workflow_node_template_uuid": NODE_TEMPLATE_UUID,
        "handle_key": "result",
        "io_type": io_type,
        "display_name": "Result",
        "type": value_type,
        "required": False,
        "description": "",
        "data_source": "result",
        "data_key": "result",
    }


def _producer_graph(
    source_schema: dict[str, object] | None = None,
    *,
    source_type: str = "number",
) -> dict[str, object]:
    graph = _empty_graph()
    graph["nodes"] = [
        {
            "uuid": NODE_UUID,
            "workflow_uuid": WORKFLOW_UUID,
            "create_time": TIMESTAMP,
            "update_time": TIMESTAMP,
            "workflow_node_template_uuid": NODE_TEMPLATE_UUID,
            "name": "producer",
            "status": "idle",
            "type": "compute",
            "pose": {},
            "param": {},
            "execution_policy": {},
            "disabled": False,
            "minimized": False,
            "meta_data": {"unilab": {"input_bindings": {}}},
        }
    ]
    graph["node_templates"] = [
        {
            "uuid": NODE_TEMPLATE_UUID,
            "create_time": TIMESTAMP,
            "update_time": TIMESTAMP,
            "meta_data": {},
            "resource_template_uuid": RESOURCE_TEMPLATE_UUID,
            "name": "produce",
            "display_name": "Produce",
            "goal": {},
            "goal_default": {},
            "feedback": {},
            "result": {},
            "type": "action",
            "node_type": "compute",
            "description": "",
        }
    ]
    graph["handle_templates"] = [
        _handle(
            handle_uuid=SOURCE_HANDLE_UUID,
            io_type="source",
            value_schema=source_schema or {"type": "number"},
            value_type=source_type,
        ),
        _handle(
            handle_uuid=TARGET_HANDLE_UUID,
            io_type="target",
            value_schema={"type": "number"},
            value_type="number",
        ),
    ]
    return graph


def _declare_output(
    graph: dict[str, object],
    *,
    schema: dict[str, object],
    binding: dict[str, object],
) -> None:
    unilab = graph["workflow"]["meta_data"]["unilab"]
    unilab["output_contract"] = {
        "version": 1,
        "outputs": [
            {
                "name": "result",
                "schema": deepcopy(schema),
                "implicit": False,
            }
        ],
    }
    unilab["output_bindings"] = {"result": deepcopy(binding)}


def _declare_input_binding(
    graph: dict[str, object],
    *,
    producer_schema: dict[str, object],
    consumer_schema: dict[str, object],
    consumer_type: str,
    producer_required: bool = True,
) -> None:
    parameter: dict[str, object] = {
        "name": "input",
        "schema": deepcopy(producer_schema),
        "required": producer_required,
    }
    if not producer_required:
        parameter["default"] = None
    graph["workflow"]["meta_data"]["unilab"]["input_contract"] = {
        "version": 1,
        "parameters": [parameter],
    }
    graph["nodes"][0]["meta_data"]["unilab"]["input_bindings"] = {
        TARGET_HANDLE_UUID: {"parameter": "input"}
    }
    graph["handle_templates"][1] = _handle(
        handle_uuid=TARGET_HANDLE_UUID,
        io_type="target",
        value_schema=consumer_schema,
        value_type=consumer_type,
    )


def _validate(graph: dict[str, object]) -> dict[str, object]:
    base_graph = deepcopy(graph)
    base_unilab = base_graph["workflow"]["meta_data"]["unilab"]
    base_unilab["input_contract"] = {"version": 1, "parameters": []}
    base_unilab["output_contract"] = {"version": 1, "outputs": []}
    base_unilab["output_bindings"] = {}
    return validate_candidate_bundle(
        graph=graph,
        base_graph=base_graph,
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


def _node_output_binding(
    *,
    workflow_node_uuid: str = NODE_UUID,
    source_handle_uuid: str = SOURCE_HANDLE_UUID,
) -> dict[str, object]:
    return {
        "kind": "node_output",
        "workflow_node_uuid": workflow_node_uuid,
        "source_handle_uuid": source_handle_uuid,
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


@pytest.mark.parametrize(
    "binding",
    [
        pytest.param(
            _node_output_binding(workflow_node_uuid=FOREIGN_NODE_UUID),
            id="foreign-node",
        ),
        pytest.param(
            {
                "kind": "node_output",
                "workflow_node_uuid": NODE_UUID,
            },
            id="missing-source-handle",
        ),
        pytest.param(
            _node_output_binding(source_handle_uuid=TARGET_HANDLE_UUID),
            id="target-direction-handle",
        ),
        pytest.param(
            {"kind": "workflow_input", "parameter": "unknown"},
            id="unknown-workflow-input",
        ),
    ],
)
def test_candidate_rejects_invalid_output_binding_identity(
    binding: dict[str, object],
) -> None:
    graph = _producer_graph()
    _declare_output(graph, schema={"type": "number"}, binding=binding)

    with pytest.raises(CandidateBundleError):
        _validate(graph)


@pytest.mark.parametrize(
    ("source_schema", "source_type", "output_schema"),
    [
        pytest.param(
            {"type": "number"},
            "number",
            {"type": "integer"},
            id="number-cannot-promise-integer",
        ),
        pytest.param(
            {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "string",
            {"type": "string"},
            id="nullable-cannot-promise-non-null",
        ),
        pytest.param(
            {"$slot": "ResourceSlot"},
            "ResourceSlot",
            {
                "$slot": "ResourceSlot",
                "allowed_resource_template_uuids": [RESOURCE_TEMPLATE_A_UUID],
            },
            id="unconstrained-slot-cannot-promise-restricted-slot",
        ),
    ],
)
def test_candidate_rejects_output_schema_not_guaranteed_by_producer(
    source_schema: dict[str, object],
    source_type: str,
    output_schema: dict[str, object],
) -> None:
    graph = _producer_graph(source_schema, source_type=source_type)
    _declare_output(graph, schema=output_schema, binding=_node_output_binding())

    with pytest.raises(CandidateBundleError):
        _validate(graph)


def test_candidate_accepts_integer_list_producer_for_number_list_output() -> None:
    graph = _producer_graph(
        {"type": "array", "items": {"type": "integer"}},
        source_type="array",
    )
    _declare_output(
        graph,
        schema={"type": "array", "items": {"type": "number"}},
        binding=_node_output_binding(),
    )

    assert _validate(graph) is graph


def test_candidate_accepts_resource_slot_producer_allowlist_subset() -> None:
    graph = _producer_graph(
        {
            "$slot": "ResourceSlot",
            "allowed_resource_template_uuids": [RESOURCE_TEMPLATE_A_UUID],
        },
        source_type="ResourceSlot",
    )
    _declare_output(
        graph,
        schema={
            "$slot": "ResourceSlot",
            "allowed_resource_template_uuids": [
                RESOURCE_TEMPLATE_A_UUID,
                RESOURCE_TEMPLATE_B_UUID,
            ],
        },
        binding=_node_output_binding(),
    )

    assert _validate(graph) is graph


@pytest.mark.parametrize(
    ("producer_schema", "consumer_schema"),
    [
        pytest.param(
            {"$slot": "ResourceSlot"},
            {
                "$slot": "ResourceSlot",
                "allowed_resource_template_uuids": [RESOURCE_TEMPLATE_A_UUID],
            },
            id="unconstrained-producer",
        ),
        pytest.param(
            {
                "$slot": "ResourceSlot",
                "allowed_resource_template_uuids": [RESOURCE_TEMPLATE_A_UUID],
            },
            {
                "$slot": "ResourceSlot",
                "allowed_resource_template_uuids": [RESOURCE_TEMPLATE_B_UUID],
            },
            id="disjoint-allowlists",
        ),
    ],
)
def test_candidate_rejects_workflow_input_slot_not_guaranteed_for_handle(
    producer_schema: dict[str, object],
    consumer_schema: dict[str, object],
) -> None:
    graph = _producer_graph()
    _declare_input_binding(
        graph,
        producer_schema=producer_schema,
        consumer_schema=consumer_schema,
        consumer_type="ResourceSlot",
    )

    with pytest.raises(CandidateBundleError):
        _validate(graph)


@pytest.mark.parametrize(
    "producer_allowlist",
    [
        pytest.param([RESOURCE_TEMPLATE_A_UUID], id="proper-subset"),
        pytest.param(
            [RESOURCE_TEMPLATE_A_UUID, RESOURCE_TEMPLATE_B_UUID],
            id="same-set",
        ),
    ],
)
def test_candidate_accepts_workflow_input_slot_guaranteed_for_handle(
    producer_allowlist: list[str],
) -> None:
    consumer_allowlist = [RESOURCE_TEMPLATE_A_UUID, RESOURCE_TEMPLATE_B_UUID]
    graph = _producer_graph()
    _declare_input_binding(
        graph,
        producer_schema={
            "$slot": "ResourceSlot",
            "allowed_resource_template_uuids": producer_allowlist,
        },
        consumer_schema={
            "$slot": "ResourceSlot",
            "allowed_resource_template_uuids": consumer_allowlist,
        },
        consumer_type="ResourceSlot",
    )

    assert _validate(graph) is graph


def test_candidate_rejects_nullable_workflow_input_for_optional_non_null_handle() -> (
    None
):
    graph = _producer_graph()
    _declare_input_binding(
        graph,
        producer_schema={
            "anyOf": [{"type": "string"}, {"type": "null"}],
        },
        consumer_schema={"type": "string"},
        consumer_type="string",
        producer_required=False,
    )

    with pytest.raises(CandidateBundleError):
        _validate(graph)
