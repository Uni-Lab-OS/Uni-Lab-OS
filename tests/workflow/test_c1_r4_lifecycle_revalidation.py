"""C1 R4 旧 Canvas 投影的 child evolution 重验证 RED。"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from unilabos.workflow.composite import project_published_workflow_contract
from unilabos.workflow.models import WorkflowEdgeWrite, WorkflowNodeWrite

from .c1_r2_static_expansion_fixture import (
    CHILD_TEMPLATE_UUID,
    WorkflowContractFixture,
    _mark_applied,
    backend_contract_import,
)
from .test_c1_r3_authoring_fixed_point import (
    INVOCATION_UUID,
    PARENT_WORKFLOW_UUID,
    _compile,
    _opened_engine,
    _source,
)

ADDITIVE_HANDLE_UUID = "a5000000-0000-4000-8000-00000000000d"


def _boundary_only(graph: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(graph)
    result["nodes"] = [
        node for node in result["nodes"] if node["uuid"] == INVOCATION_UUID
    ]
    result["edges"] = []
    result["node_templates"] = [
        template
        for template in result["node_templates"]
        if template["uuid"] == CHILD_TEMPLATE_UUID
    ]
    result["handle_templates"] = [
        handle
        for handle in result["handle_templates"]
        if handle["workflow_node_template_uuid"] == CHILD_TEMPLATE_UUID
    ]
    return result


def _republish_child(
    world: Any,
    *,
    additive: bool,
    transparent: bool = False,
) -> WorkflowContractFixture:
    child = world.child
    graph = world.store.get_graph(child.source.workflow_uuid)
    meta_data = deepcopy(graph["workflow"]["meta_data"])
    unilab = meta_data["unilab"]
    if additive:
        unilab["input_contract"]["parameters"].append(
            {
                "name": "optional_gain",
                "schema": {"type": "number"},
                "required": False,
                "default": 1.0,
            }
        )
    unilab["composition_allow_transparent"] = transparent
    with world.store.transaction() as connection:
        connection.execute(
            "UPDATE workflow SET meta_data = ? WHERE uuid = ?",
            (json.dumps(meta_data, sort_keys=True), child.source.workflow_uuid),
        )
    saved = world.store.save_graph(
        child.source.workflow_uuid,
        revision=graph["workflow"]["revision"],
        nodes=[WorkflowNodeWrite.model_validate(item) for item in graph["nodes"]],
        edges=[WorkflowEdgeWrite.model_validate(item) for item in graph["edges"]],
    )
    _mark_applied(
        world.store, child.source.workflow_uuid, saved["workflow"]["revision"]
    )
    projected = project_published_workflow_contract(
        source=child.source,
        applied_snapshot=world.store.get_published_workflow_snapshot(
            child.source.workflow_uuid
        ),
        host_node_resource_template_uuid=projected_owner(child, world),
    )
    assert projected is not None
    handle_uuids = dict(child.handles)
    if additive:
        handle_uuids[("optional_gain", "target")] = ADDITIVE_HANDLE_UUID
    imported = backend_contract_import(
        projected,
        template_uuid=child.template_uuid,
        handle_uuids=handle_uuids,
    )
    world.imports[child.template_uuid] = imported
    world.publish()
    updated = WorkflowContractFixture(
        source=child.source,
        template_uuid=child.template_uuid,
        handles=handle_uuids,
        contract_pin=dict(imported.template["schema"]["x-unilabos-workflow-contract"]),
    )
    world.child = updated
    return updated


def projected_owner(child: WorkflowContractFixture, world: Any) -> str:
    template = world.imports[child.template_uuid].template
    return str(template["resource_template_uuid"])


def _generate(engine: Any, graph: dict[str, Any]) -> Any:
    return engine.generate_python(
        workflow_uuid=PARENT_WORKFLOW_UUID,
        workflow_revision=1,
        graph=graph,
        source_uri="package://tests/c1_r4/workflows/parent.py",
    )


def test_old_canvas_projection_revalidates_after_additive_child_republish(
    tmp_path: Path,
) -> None:
    world, engine = _opened_engine(tmp_path)
    try:
        initial = _compile(
            engine, _source(), world.store.get_graph(PARENT_WORKFLOW_UUID)
        )
        assert initial.valid and initial.graph is not None, initial.diagnostics
        old_canvas = _boundary_only(initial.graph)
        old_digest = world.child.contract_pin["contract_digest"]

        updated = _republish_child(world, additive=True)
        assert updated.contract_pin["contract_digest"] != old_digest

        generated = _generate(engine, old_canvas)

        assert generated.valid, generated.diagnostics
        assert generated.normalized_python_source is not None
        revalidated = _compile(engine, generated.normalized_python_source, old_canvas)
        assert revalidated.valid and revalidated.graph is not None, (
            revalidated.diagnostics
        )
        invocation = next(
            node
            for node in revalidated.graph["nodes"]
            if node["uuid"] == INVOCATION_UUID
        )
        assert (
            invocation["meta_data"]["unilab"]["composite"]["contract_digest"]
            == (updated.contract_pin["contract_digest"])
        )
        assert ADDITIVE_HANDLE_UUID in {
            item["uuid"] for item in revalidated.graph["handle_templates"]
        }
    finally:
        world.close()


def test_old_canvas_breaking_republish_is_stable_and_parent_store_is_unchanged(
    tmp_path: Path,
) -> None:
    world, engine = _opened_engine(tmp_path)
    try:
        initial = _compile(
            engine, _source(), world.store.get_graph(PARENT_WORKFLOW_UUID)
        )
        assert initial.valid and initial.graph is not None, initial.diagnostics
        old_canvas = _boundary_only(initial.graph)
        parent_before = deepcopy(world.store.get_graph(PARENT_WORKFLOW_UUID))

        _republish_child(world, additive=False, transparent=True)
        rejected = _generate(engine, old_canvas)

        assert not rejected.valid
        assert [item["code"] for item in rejected.diagnostics] == [
            "composite_contract_stale"
        ]
        assert world.store.get_graph(PARENT_WORKFLOW_UUID) == parent_before
    finally:
        world.close()
