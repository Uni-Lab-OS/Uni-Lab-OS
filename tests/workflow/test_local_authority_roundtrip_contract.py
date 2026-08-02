"""Round 02G1 local CatalogAuthority 自身 Candidate round-trip 合同。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.catalog import (
    CatalogAuthority,
    TemplateCatalog,
    TemplateCatalogImportError,
)
from unilabos.workflow.models import CandidateChangeset
from unilabos.workflow.store import WorkflowStore

from .test_authoring_engine import (
    ANALYZE_NODE_UUID,
    PREPARE_NODE_UUID,
    WORKFLOW_UUID,
    EngineContext,
    _assert_error_result,
    _catalog_imports,
    _compile,
    _source,
)

LOCAL_AUTHORITY = CatalogAuthority(authority_id="round-02g1-local", kind="local")


def _local_imports() -> list[Any]:
    """Registry importer 只声明业务键；local Catalog 分配全部模板 UUID。"""

    imports = deepcopy(_catalog_imports())
    for item in imports:
        item.template.pop("uuid")
        for handle in item.handles:
            handle.pop("uuid")
    return imports


@contextmanager
def _opened_local_engine(database_path: Path) -> Iterator[EngineContext]:
    store = WorkflowStore(database_path)
    try:
        catalog = TemplateCatalog(store)
        snapshot = catalog.replace(LOCAL_AUTHORITY, _local_imports())
        engine = WorkflowAuthoringEngine(catalog=catalog, authority=LOCAL_AUTHORITY)
        yield EngineContext(store, catalog, engine, snapshot.fingerprint)
    finally:
        store.close()


@pytest.fixture()
def local_engine_context(tmp_path: Path) -> Iterator[EngineContext]:
    with _opened_local_engine(tmp_path / "workflow.db") as context:
        yield context


def _template_by_name(graph: dict[str, Any], name: str) -> dict[str, Any]:
    return next(item for item in graph["node_templates"] if item["name"] == name)


def _handle_by_key(
    graph: dict[str, Any],
    *,
    template_uuid: str,
    handle_key: str,
    io_type: str,
) -> dict[str, Any]:
    return next(
        item
        for item in graph["handle_templates"]
        if item["workflow_node_template_uuid"] == template_uuid
        and item["handle_key"] == handle_key
        and item["io_type"] == io_type
    )


def test_local_catalog_rejects_caller_uuid_and_allocates_server_owned_identity(
    tmp_path: Path,
) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    try:
        catalog = TemplateCatalog(store)
        with pytest.raises(TemplateCatalogImportError) as caught:
            catalog.replace(LOCAL_AUTHORITY, deepcopy(_catalog_imports()))
        assert caught.value.code == "template_catalog_mismatch"

        snapshot = catalog.replace(LOCAL_AUTHORITY, _local_imports())
    finally:
        store.close()

    allocated_node_uuids = {item["uuid"] for item in snapshot.node_templates}
    allocated_handle_uuids = {item["uuid"] for item in snapshot.handle_templates}
    caller_node_uuids = {item.template["uuid"] for item in _catalog_imports()}
    caller_handle_uuids = {
        handle["uuid"] for item in _catalog_imports() for handle in item.handles
    }
    assert allocated_node_uuids.isdisjoint(caller_node_uuids)
    assert allocated_handle_uuids.isdisjoint(caller_handle_uuids)
    assert all(UUID(value).version == 4 for value in allocated_node_uuids)
    assert all(UUID(value).version == 4 for value in allocated_handle_uuids)


def test_local_compile_candidate_generates_itself_without_identity_drift(
    local_engine_context: EngineContext,
) -> None:
    source = _source().replace("= 3,", "= 4,")
    compiled = _compile(local_engine_context.engine, source)
    assert compiled.valid and compiled.graph is not None

    candidate = compiled.graph
    prepare_template = _template_by_name(candidate, "prepare")
    analyze_template = _template_by_name(candidate, "analyze")
    prepare_sample_target = _handle_by_key(
        candidate,
        template_uuid=prepare_template["uuid"],
        handle_key="sample",
        io_type="target",
    )
    prepare_sample_source = _handle_by_key(
        candidate,
        template_uuid=prepare_template["uuid"],
        handle_key="prepared",
        io_type="source",
    )
    analyze_sample_target = _handle_by_key(
        candidate,
        template_uuid=analyze_template["uuid"],
        handle_key="prepared",
        io_type="target",
    )
    analyze_report_source = _handle_by_key(
        candidate,
        template_uuid=analyze_template["uuid"],
        handle_key="report",
        io_type="source",
    )

    unilab = candidate["workflow"]["meta_data"]["unilab"]
    cycles = next(
        item
        for item in unilab["input_contract"]["parameters"]
        if item["name"] == "cycles"
    )
    assert cycles["default"] == 4
    prepare = next(
        item for item in candidate["nodes"] if item["uuid"] == PREPARE_NODE_UUID
    )
    assert prepare["meta_data"]["unilab"]["input_bindings"][
        prepare_sample_target["uuid"]
    ] == {"parameter": "sample"}
    edge = next(
        item
        for item in candidate["edges"]
        if item["source_node_uuid"] == PREPARE_NODE_UUID
        and item["target_node_uuid"] == ANALYZE_NODE_UUID
    )
    assert edge["source_handle_uuid"] == prepare_sample_source["uuid"]
    assert edge["target_handle_uuid"] == analyze_sample_target["uuid"]
    assert unilab["output_bindings"] == {
        "sample": {
            "kind": "node_output",
            "workflow_node_uuid": PREPARE_NODE_UUID,
            "source_handle_uuid": prepare_sample_source["uuid"],
        },
        "report": {
            "kind": "node_output",
            "workflow_node_uuid": ANALYZE_NODE_UUID,
            "source_handle_uuid": analyze_report_source["uuid"],
        },
    }

    generated = local_engine_context.engine.generate_python(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        graph=candidate,
        source_uri="package://lab/workflows/local-round-trip.py",
    )

    assert generated.valid, generated.diagnostics
    assert generated.graph == candidate
    assert generated.normalized_python_source is not None
    assert "= 4" in generated.normalized_python_source
    assert CandidateChangeset.model_validate(generated.changeset).kind == "source_only"


def test_local_generate_still_rejects_foreign_catalog_identity(
    local_engine_context: EngineContext,
) -> None:
    compiled = _compile(local_engine_context.engine)
    assert compiled.valid and compiled.graph is not None
    foreign = deepcopy(compiled.graph)
    foreign["nodes"][0]["workflow_node_template_uuid"] = (
        "30000000-0000-4000-8000-000000000099"
    )

    result = local_engine_context.engine.generate_python(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        graph=foreign,
        source_uri="package://lab/workflows/foreign.py",
    )

    _assert_error_result(result, code="template_catalog_mismatch")


def test_local_generate_still_rejects_semantically_unrepresentable_graph(
    local_engine_context: EngineContext,
) -> None:
    compiled = _compile(local_engine_context.engine)
    assert compiled.valid and compiled.graph is not None
    unrepresentable = deepcopy(compiled.graph)
    unrepresentable["workflow"]["meta_data"]["unilab"]["output_bindings"]["report"][
        "workflow_node_uuid"
    ] = PREPARE_NODE_UUID

    result = local_engine_context.engine.generate_python(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        graph=unrepresentable,
        source_uri="package://lab/workflows/unrepresentable.py",
    )

    _assert_error_result(result, code="round_trip_mismatch")
