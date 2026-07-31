"""Round 02D production Authoring engine 的公开行为合同。"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow.catalog import (
    CatalogAuthority,
    NodeTemplateImport,
    TemplateCatalog,
)
from unilabos.workflow.models import (
    CandidateChangeset,
    CandidateCompilation,
    CandidateDiagnostic,
    CandidateSourceMapEntry,
)
from unilabos.workflow.store import WorkflowStore

_AUTHORING_IMPORT_ERROR: ModuleNotFoundError | None = None
try:
    _authoring_api = import_module("unilabos.workflow.authoring_engine")
except ModuleNotFoundError as error:
    if error.name != "unilabos.workflow.authoring_engine":
        raise
    _AUTHORING_IMPORT_ERROR = error
    _authoring_api = None

WORKFLOW_UUID = "10000000-0000-4000-8000-000000000001"
OTHER_WORKFLOW_UUID = "10000000-0000-4000-8000-000000000002"
PREPARE_NODE_UUID = "20000000-0000-4000-8000-000000000001"
ANALYZE_NODE_UUID = "20000000-0000-4000-8000-000000000002"
GROUP_A_NODE_UUID = "20000000-0000-4000-8000-000000000003"
GROUP_B_NODE_UUID = "20000000-0000-4000-8000-000000000004"
FINAL_NODE_UUID = "20000000-0000-4000-8000-000000000005"

PREPARE_TEMPLATE_UUID = "30000000-0000-4000-8000-000000000001"
ANALYZE_TEMPLATE_UUID = "30000000-0000-4000-8000-000000000002"
GROUP_TEMPLATE_UUID = "30000000-0000-4000-8000-000000000003"
FINAL_TEMPLATE_UUID = "30000000-0000-4000-8000-000000000004"
RESOURCE_TEMPLATE_UUID = "31000000-0000-4000-8000-000000000001"

PREPARE_SAMPLE_TARGET = "40000000-0000-4000-8000-000000000001"
PREPARE_CYCLES_TARGET = "40000000-0000-4000-8000-000000000002"
PREPARE_NOTE_TARGET = "40000000-0000-4000-8000-000000000003"
PREPARE_READY_TARGET = "40000000-0000-4000-8000-000000000004"
PREPARE_SAMPLE_SOURCE = "40000000-0000-4000-8000-000000000005"
PREPARE_REPORT_SOURCE = "40000000-0000-4000-8000-000000000006"
PREPARE_READY_SOURCE = "40000000-0000-4000-8000-000000000007"

ANALYZE_SAMPLE_TARGET = "41000000-0000-4000-8000-000000000001"
ANALYZE_LABEL_TARGET = "41000000-0000-4000-8000-000000000002"
ANALYZE_READY_TARGET = "41000000-0000-4000-8000-000000000003"
ANALYZE_REPORT_SOURCE = "41000000-0000-4000-8000-000000000004"
ANALYZE_READY_SOURCE = "41000000-0000-4000-8000-000000000005"

FINAL_REPORT_TARGET = "42000000-0000-4000-8000-000000000001"
FINAL_READY_TARGET = "42000000-0000-4000-8000-000000000002"
FINAL_REPORT_SOURCE = "42000000-0000-4000-8000-000000000003"
FINAL_READY_SOURCE = "42000000-0000-4000-8000-000000000004"

AUTHORITY = CatalogAuthority(authority_id="backend-primary", kind="backend")
TIMESTAMP = "2026-08-01T00:00:00Z"


@dataclass
class EngineContext:
    store: WorkflowStore
    catalog: TemplateCatalog
    engine: Any
    fingerprint: str


def _engine_class() -> type[Any]:
    assert _AUTHORING_IMPORT_ERROR is None, (
        "缺少冻结 production seam: unilabos.workflow.authoring_engine"
    )
    assert _authoring_api is not None
    return _authoring_api.WorkflowAuthoringEngine


def _handle(
    handle_uuid: str,
    *,
    key: str,
    io_type: str,
    value_type: str,
    required: bool = False,
    data_source: str | None = None,
) -> dict[str, Any]:
    return {
        "uuid": handle_uuid,
        "description": f"{key} contract",
        "meta_data": {"contract": "declared"},
        "handle_key": key,
        "io_type": io_type,
        "display_name": key.replace("_", " ").title(),
        "type": value_type,
        "required": required,
        "data_source": data_source,
        "data_key": key,
    }


def _template(
    template_uuid: str,
    *,
    name: str,
    class_name: str = "lab.devices:Reactor",
    node_type: str = "compute",
    handles: list[dict[str, Any]] | None = None,
    resource_template_uuid: str = RESOURCE_TEMPLATE_UUID,
) -> NodeTemplateImport:
    return NodeTemplateImport(
        template={
            "uuid": template_uuid,
            "description": f"{name} action",
            "meta_data": {"catalog_owner": "backend"},
            "resource_template_uuid": resource_template_uuid,
            "name": name,
            "display_name": name.replace("_", " ").title(),
            "class": class_name,
            "goal": {},
            "goal_default": {},
            "feedback": {},
            "result": {},
            "schema": None,
            "type": "group" if node_type == "group" else "action",
            "icon": None,
            "header": None,
            "footer": None,
            "node_type": node_type,
        },
        handles=[] if handles is None else handles,
    )


def _catalog_imports() -> list[NodeTemplateImport]:
    ready_target = {
        "key": "ready",
        "io_type": "target",
        "value_type": "any",
        "required": False,
        "data_source": "dependency",
    }
    ready_source = {
        "key": "ready",
        "io_type": "source",
        "value_type": "any",
        "data_source": "dependency",
    }
    return [
        _template(
            PREPARE_TEMPLATE_UUID,
            name="prepare",
            handles=[
                _handle(
                    PREPARE_SAMPLE_TARGET,
                    key="sample",
                    io_type="target",
                    value_type="ResourceSlot",
                    required=True,
                    data_source="executor",
                ),
                _handle(
                    PREPARE_CYCLES_TARGET,
                    key="cycles",
                    io_type="target",
                    value_type="integer",
                    required=True,
                    data_source="executor",
                ),
                _handle(
                    PREPARE_NOTE_TARGET,
                    key="note",
                    io_type="target",
                    value_type="string",
                    data_source="executor",
                ),
                _handle(PREPARE_READY_TARGET, **ready_target),
                _handle(
                    PREPARE_SAMPLE_SOURCE,
                    key="prepared",
                    io_type="source",
                    value_type="ResourceSlot",
                    data_source="executor",
                ),
                _handle(
                    PREPARE_REPORT_SOURCE,
                    key="report",
                    io_type="source",
                    value_type="string",
                    data_source="executor",
                ),
                _handle(PREPARE_READY_SOURCE, **ready_source),
            ],
        ),
        _template(
            ANALYZE_TEMPLATE_UUID,
            name="analyze",
            handles=[
                _handle(
                    ANALYZE_SAMPLE_TARGET,
                    key="prepared",
                    io_type="target",
                    value_type="ResourceSlot",
                    required=True,
                    data_source="executor",
                ),
                _handle(
                    ANALYZE_LABEL_TARGET,
                    key="label",
                    io_type="target",
                    value_type="string",
                    required=True,
                    data_source="executor",
                ),
                _handle(ANALYZE_READY_TARGET, **ready_target),
                _handle(
                    ANALYZE_REPORT_SOURCE,
                    key="report",
                    io_type="source",
                    value_type="string",
                    data_source="executor",
                ),
                _handle(ANALYZE_READY_SOURCE, **ready_source),
            ],
        ),
        _template(
            GROUP_TEMPLATE_UUID,
            name="group",
            class_name="unilabos.workflow.authoring:group",
            node_type="group",
        ),
        _template(
            FINAL_TEMPLATE_UUID,
            name="finalize",
            handles=[
                _handle(
                    FINAL_REPORT_TARGET,
                    key="report",
                    io_type="target",
                    value_type="string",
                    data_source="executor",
                ),
                _handle(FINAL_READY_TARGET, **ready_target),
                _handle(
                    FINAL_REPORT_SOURCE,
                    key="report",
                    io_type="source",
                    value_type="string",
                    data_source="executor",
                ),
                _handle(FINAL_READY_SOURCE, **ready_source),
            ],
        ),
    ]


def _workflow(
    *,
    workflow_uuid: str = WORKFLOW_UUID,
    revision: int = 7,
    meta_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "uuid": workflow_uuid,
        "create_time": TIMESTAMP,
        "update_time": TIMESTAMP,
        "meta_data": (
            {"owner": "keep", "unilab": {"presentation": "keep"}}
            if meta_data is None
            else meta_data
        ),
        "name": "Persisted workflow",
        "tags": ["keep"],
        "revision": revision,
        "description": "Persisted description",
    }


def _empty_graph(
    *,
    workflow_uuid: str = WORKFLOW_UUID,
    revision: int = 7,
    meta_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "workflow": _workflow(
            workflow_uuid=workflow_uuid,
            revision=revision,
            meta_data=meta_data,
        ),
        "nodes": [],
        "edges": [],
        "node_templates": [],
        "handle_templates": [],
    }


def _source(
    *,
    workflow_uuid: str = WORKFLOW_UUID,
    fixed_device_id: str | None = None,
) -> str:
    selector = "device()" if fixed_device_id is None else f'device("{fixed_device_id}")'
    return f'''from typing import Annotated, Literal

from pydantic import Field
from lab.devices import Reactor
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import device, workflow_definition, workflow_output


reactor: Reactor = {selector}


@workflow_definition(
    workflow_uuid="{workflow_uuid}",
    displayname="Sample preparation",
    description="Prepare and analyze one sample.",
)
def prepare_sample(
    *,
    sample: ResourceSlot,
    cycles: Annotated[int, Field(ge=1, le=10)] = 3,
    mode: Literal["fast", "safe"] = "safe",
    note: Annotated[str | None, Field(max_length=200)] = None,
):
    # unilab:node_uuid={PREPARE_NODE_UUID}
    prepared = reactor.prepare(
        sample=sample,
        cycles=cycles,
        note=note,
    )
    # unilab:node_uuid={ANALYZE_NODE_UUID}
    analyzed = reactor.analyze(
        prepared=prepared.prepared,
        label=mode,
    )
    return workflow_output(
        sample=prepared.prepared,
        report=analyzed.report,
    )
'''


@contextmanager
def _opened_engine(
    database_path: Path,
    *,
    imports: list[NodeTemplateImport] | None = None,
) -> Iterator[EngineContext]:
    store = WorkflowStore(database_path)
    try:
        catalog = TemplateCatalog(store)
        snapshot = catalog.replace(
            AUTHORITY,
            _catalog_imports() if imports is None else imports,
        )
        engine = _engine_class()(catalog=catalog, authority=AUTHORITY)
        yield EngineContext(store, catalog, engine, snapshot.fingerprint)
    finally:
        store.close()


@pytest.fixture()
def engine_context(tmp_path: Path) -> Iterator[EngineContext]:
    with _opened_engine(tmp_path / "workflow.db") as context:
        yield context


def _compile(
    engine: Any,
    source: str | None = None,
    *,
    graph: dict[str, Any] | None = None,
) -> CandidateCompilation:
    return engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=_source() if source is None else source,
        source_uri="package://lab/workflows/sample.py",
        applied_graph=_empty_graph() if graph is None else graph,
    )


def _assert_error_result(
    result: CandidateCompilation,
    *,
    code: str | None = None,
) -> None:
    assert not result.valid
    assert result.graph is None
    assert result.normalized_python_source is None
    assert result.source_map == []
    assert result.changeset is None
    assert result.diagnostics
    validated = [
        CandidateDiagnostic.model_validate(item).model_dump(exclude_none=True)
        for item in result.diagnostics
    ]
    assert validated == result.diagnostics
    if code is not None:
        assert code in {item["code"] for item in result.diagnostics}


def _node_by_uuid(graph: Mapping[str, Any], node_uuid: str) -> dict[str, Any]:
    return next(node for node in graph["nodes"] if node["uuid"] == node_uuid)


def test_engine_implements_the_frozen_public_transform_seam(
    engine_context: EngineContext,
) -> None:
    engine = engine_context.engine

    assert isinstance(engine.compiler_version, str) and engine.compiler_version
    assert engine.template_catalog_fingerprint == engine_context.fingerprint
    with engine.catalog_snapshot() as fingerprint:
        assert fingerprint == engine_context.fingerprint
    for method_name in ("compile", "generate_python", "validate"):
        assert callable(getattr(engine, method_name))


def test_compile_returns_backend_identity_contracts_bindings_and_catalog(
    engine_context: EngineContext,
) -> None:
    result = _compile(engine_context.engine)

    assert result.valid
    assert result.compiler_version == engine_context.engine.compiler_version
    assert result.template_catalog_fingerprint == engine_context.fingerprint
    assert result.graph is not None
    graph = result.graph
    assert set(graph) == {
        "workflow",
        "nodes",
        "edges",
        "node_templates",
        "handle_templates",
    }
    assert graph["workflow"]["uuid"] == WORKFLOW_UUID
    assert graph["workflow"]["revision"] == 7
    assert graph["workflow"]["name"] == "Sample preparation"
    assert graph["workflow"]["description"] == "Prepare and analyze one sample."
    assert graph["workflow"]["tags"] == ["keep"]
    assert graph["workflow"]["meta_data"]["owner"] == "keep"
    assert graph["workflow"]["meta_data"]["unilab"]["presentation"] == "keep"

    unilab = graph["workflow"]["meta_data"]["unilab"]
    assert unilab["input_contract"] == {
        "version": 1,
        "parameters": [
            {
                "name": "sample",
                "schema": {"$slot": "ResourceSlot"},
                "required": True,
            },
            {
                "name": "cycles",
                "schema": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                },
                "required": False,
                "default": 3,
            },
            {
                "name": "mode",
                "schema": {
                    "type": "string",
                    "enum": ["fast", "safe"],
                },
                "required": False,
                "default": "safe",
            },
            {
                "name": "note",
                "schema": {
                    "anyOf": [{"type": "string", "maxLength": 200}, {"type": "null"}]
                },
                "required": False,
                "default": None,
            },
        ],
    }
    assert unilab["output_contract"] == {
        "version": 1,
        "outputs": [
            {
                "name": "sample",
                "schema": {"$slot": "ResourceSlot"},
                "implicit": False,
            },
            {
                "name": "report",
                "schema": {"type": "string"},
                "implicit": False,
            },
        ],
    }
    assert unilab["output_bindings"] == {
        "sample": {
            "kind": "node_output",
            "workflow_node_uuid": PREPARE_NODE_UUID,
            "source_handle_uuid": PREPARE_SAMPLE_SOURCE,
        },
        "report": {
            "kind": "node_output",
            "workflow_node_uuid": ANALYZE_NODE_UUID,
            "source_handle_uuid": ANALYZE_REPORT_SOURCE,
        },
    }

    prepare = _node_by_uuid(graph, PREPARE_NODE_UUID)
    analyze = _node_by_uuid(graph, ANALYZE_NODE_UUID)
    assert prepare["workflow_node_template_uuid"] == PREPARE_TEMPLATE_UUID
    assert analyze["workflow_node_template_uuid"] == ANALYZE_TEMPLATE_UUID
    assert prepare["meta_data"]["unilab"]["input_bindings"] == {
        PREPARE_SAMPLE_TARGET: {"parameter": "sample"},
        PREPARE_CYCLES_TARGET: {"parameter": "cycles"},
        PREPARE_NOTE_TARGET: {"parameter": "note"},
    }
    assert analyze["meta_data"]["unilab"]["input_bindings"] == {
        ANALYZE_LABEL_TARGET: {"parameter": "mode"}
    }
    assert len(graph["edges"]) == 1
    edge = graph["edges"][0]
    assert {
        "source_node_uuid": edge["source_node_uuid"],
        "target_node_uuid": edge["target_node_uuid"],
        "source_handle_uuid": edge["source_handle_uuid"],
        "target_handle_uuid": edge["target_handle_uuid"],
    } == {
        "source_node_uuid": PREPARE_NODE_UUID,
        "target_node_uuid": ANALYZE_NODE_UUID,
        "source_handle_uuid": PREPARE_SAMPLE_SOURCE,
        "target_handle_uuid": ANALYZE_SAMPLE_TARGET,
    }
    assert {item["uuid"] for item in graph["node_templates"]} == {
        PREPARE_TEMPLATE_UUID,
        ANALYZE_TEMPLATE_UUID,
    }
    assert {
        item["workflow_node_template_uuid"] for item in graph["handle_templates"]
    } == {
        PREPARE_TEMPLATE_UUID,
        ANALYZE_TEMPLATE_UUID,
    }
    json.dumps(graph, ensure_ascii=False, allow_nan=False)


def test_compile_is_deterministic_and_emits_valid_source_map_and_changeset(
    engine_context: EngineContext,
) -> None:
    first = _compile(engine_context.engine)
    second = _compile(engine_context.engine)

    assert first.model_dump() == second.model_dump()
    assert first.normalized_python_source is not None
    ast.parse(first.normalized_python_source)
    assert f"# unilab:node_uuid={PREPARE_NODE_UUID}" in first.normalized_python_source
    assert f"# unilab:node_uuid={ANALYZE_NODE_UUID}" in first.normalized_python_source
    assert "typing.Optional" not in first.normalized_python_source
    assert "Optional[" not in first.normalized_python_source
    assert "note: Annotated[str | None" in first.normalized_python_source

    source_map = [
        CandidateSourceMapEntry.model_validate(item) for item in first.source_map
    ]
    assert [entry.workflow_node_uuid for entry in source_map] == [
        PREPARE_NODE_UUID,
        ANALYZE_NODE_UUID,
    ]
    assert [(entry.start_line, entry.start_column) for entry in source_map] == sorted(
        (entry.start_line, entry.start_column) for entry in source_map
    )

    changeset = CandidateChangeset.model_validate(first.changeset)
    assert changeset.kind == "graph"
    assert changeset.created_node_uuids == sorted(
        [PREPARE_NODE_UUID, ANALYZE_NODE_UUID]
    )
    assert changeset.updated_node_uuids == []
    assert changeset.deleted_node_uuids == []
    assert changeset.created_edge_uuids == sorted(changeset.created_edge_uuids)
    assert changeset.reserved_metadata_changed is True


def test_compile_never_imports_or_executes_authoring_source(
    engine_context: EngineContext,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "authoring-source-was-executed"
    executable_statement = f'open({str(marker)!r}, "w").write("executed")'
    source = _source().replace(
        "reactor: Reactor = device()",
        f"{executable_statement}\nreactor: Reactor = device()",
    )

    result = _compile(engine_context.engine, source)

    _assert_error_result(result)
    assert not marker.exists()


def test_fixed_selector_only_projects_reserved_executor_binding(
    engine_context: EngineContext,
) -> None:
    result = _compile(engine_context.engine, _source(fixed_device_id="reactor-1"))

    assert result.valid and result.graph is not None
    for node_uuid in (PREPARE_NODE_UUID, ANALYZE_NODE_UUID):
        node = _node_by_uuid(result.graph, node_uuid)
        assert node["meta_data"]["unilab"]["executor_binding"] == {
            "mode": "fixed",
            "device_id": "reactor-1",
        }
        assert node.get("material_uuid") is None
        assert "device_id" not in node


@pytest.mark.parametrize(
    "selector",
    [
        "reactor = device()",
        'reactor: Reactor = device("")',
        'reactor: Reactor = device(device_id="reactor-1")',
        "reactor: Reactor = device(get_device_id())",
        'reactor: Reactor = device("a", "b")',
    ],
    ids=[
        "untyped",
        "blank-fixed-id",
        "keyword",
        "dynamic",
        "too-many-arguments",
    ],
)
def test_invalid_device_selectors_return_diagnostics(
    engine_context: EngineContext,
    selector: str,
) -> None:
    source = _source().replace("reactor: Reactor = device()", selector)

    _assert_error_result(_compile(engine_context.engine, source))


def test_missing_or_ambiguous_catalog_identity_fails_closed(tmp_path: Path) -> None:
    missing_action = [
        item for item in _catalog_imports() if item.template["name"] != "prepare"
    ]
    duplicate_prepare = _template(
        "30000000-0000-4000-8000-000000000009",
        name="prepare",
        resource_template_uuid="31000000-0000-4000-8000-000000000009",
        handles=[],
    )

    for index, imports in enumerate(
        (missing_action, [*_catalog_imports(), duplicate_prepare])
    ):
        with _opened_engine(
            tmp_path / f"catalog-{index}.db", imports=imports
        ) as context:
            result = _compile(context.engine)
        _assert_error_result(result, code="template_catalog_mismatch")


def test_catalog_result_is_detached_and_uses_exact_snapshot_identity(
    engine_context: EngineContext,
) -> None:
    first = _compile(engine_context.engine)
    assert first.graph is not None
    first.graph["node_templates"][0]["meta_data"]["catalog_owner"] = "mutated"

    second = _compile(engine_context.engine)

    assert second.graph is not None
    assert {
        template["meta_data"]["catalog_owner"]
        for template in second.graph["node_templates"]
    } == {"backend"}
    assert second.template_catalog_fingerprint == engine_context.fingerprint


def test_outer_catalog_snapshot_is_reused_for_the_complete_conversion(
    engine_context: EngineContext,
) -> None:
    engine = engine_context.engine
    with engine.catalog_snapshot() as held_fingerprint:
        engine_context.catalog.replace(AUTHORITY, [])
        held_result = _compile(engine)

    current_result = _compile(engine)

    assert held_result.valid
    assert held_result.template_catalog_fingerprint == held_fingerprint
    _assert_error_result(current_result, code="template_catalog_mismatch")
    assert current_result.template_catalog_fingerprint != held_fingerprint


def test_unavailable_catalog_is_a_stable_transform_diagnostic(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "unavailable.db")
    try:
        engine = _engine_class()(catalog=TemplateCatalog(store), authority=AUTHORITY)
        result = _compile(engine)
    finally:
        store.close()

    _assert_error_result(result, code="template_catalog_unavailable")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda source: source.replace(
            f'workflow_uuid="{WORKFLOW_UUID}"',
            f'workflow_uuid="{OTHER_WORKFLOW_UUID}"',
        ),
        lambda source: source.replace(
            "def prepare_sample(\n    *,",
            "def prepare_sample(sample: ResourceSlot,\n    *,",
        ),
        lambda source: source.replace(
            "sample=sample,",
            "sample,",
            1,
        ),
        lambda source: source.replace(
            "prepared = reactor.prepare(",
            "prepared, extra = reactor.prepare(",
        ),
        lambda source: source.replace(
            "label=mode,",
            "unknown=mode,",
        ),
    ],
    ids=[
        "decorator-identity",
        "workflow-positional-parameter",
        "action-positional-argument",
        "tuple-result",
        "unknown-handle",
    ],
)
def test_static_subset_violations_are_structured_diagnostics(
    engine_context: EngineContext,
    mutation: Any,
) -> None:
    result = _compile(engine_context.engine, mutation(_source()))

    _assert_error_result(result)


@pytest.mark.parametrize(
    "source",
    [
        _source().replace(
            f"    # unilab:node_uuid={ANALYZE_NODE_UUID}",
            f"    # unilab:node_uuid={PREPARE_NODE_UUID}",
        ),
        _source().replace(PREPARE_NODE_UUID, "00000000-0000-0000-0000-000000000000"),
        _source().replace(
            f"    # unilab:node_uuid={PREPARE_NODE_UUID}\n    prepared",
            (
                f"    # unilab:node_uuid={PREPARE_NODE_UUID}\n"
                "    note = note\n"
                "    prepared"
            ),
        ),
    ],
    ids=["duplicate", "nil", "not-adjacent"],
)
def test_invalid_uuid_anchors_block_the_candidate(
    engine_context: EngineContext,
    source: str,
) -> None:
    _assert_error_result(_compile(engine_context.engine, source))


def test_anchor_preserves_node_identity_and_non_authoring_metadata(
    engine_context: EngineContext,
) -> None:
    initial = _compile(engine_context.engine)
    assert initial.graph is not None
    applied = deepcopy(initial.graph)
    prepare = _node_by_uuid(applied, PREPARE_NODE_UUID)
    prepare["meta_data"]["operator_note"] = "preserve"
    prepare["meta_data"]["unilab"]["ui_color"] = "blue"
    changed_source = _source().replace("cycles=cycles,", "cycles=5,")

    changed = _compile(engine_context.engine, changed_source, graph=applied)

    assert changed.valid and changed.graph is not None
    changed_prepare = _node_by_uuid(changed.graph, PREPARE_NODE_UUID)
    assert changed_prepare["uuid"] == PREPARE_NODE_UUID
    assert changed_prepare["param"]["cycles"] == 5
    assert changed_prepare["meta_data"]["operator_note"] == "preserve"
    assert changed_prepare["meta_data"]["unilab"]["ui_color"] == "blue"
    changeset = CandidateChangeset.model_validate(changed.changeset)
    assert changeset.updated_node_uuids == [PREPARE_NODE_UUID]
    assert changeset.created_node_uuids == []


def test_missing_anchor_gets_a_stable_uuid_and_normalized_anchor(
    engine_context: EngineContext,
) -> None:
    source = _source().replace(
        f"    # unilab:node_uuid={ANALYZE_NODE_UUID}\n",
        "",
    )

    first = _compile(engine_context.engine, source)
    second = _compile(engine_context.engine, source)

    assert first.valid and first.graph is not None
    assert second.valid and second.graph is not None
    allocated = next(
        node["uuid"]
        for node in first.graph["nodes"]
        if node["workflow_node_template_uuid"] == ANALYZE_TEMPLATE_UUID
    )
    assert allocated != ANALYZE_NODE_UUID
    assert allocated == next(
        node["uuid"]
        for node in second.graph["nodes"]
        if node["workflow_node_template_uuid"] == ANALYZE_TEMPLATE_UUID
    )
    assert first.normalized_python_source is not None
    assert f"# unilab:node_uuid={allocated}" in first.normalized_python_source


def test_syntax_and_dynamic_python_fail_with_deterministic_diagnostics(
    engine_context: EngineContext,
) -> None:
    cases = [
        "def broken(:\n",
        _source().replace(
            (
                f"    # unilab:node_uuid={PREPARE_NODE_UUID}\n"
                "    prepared = reactor.prepare("
            ),
            (
                '    if mode == "fast":\n'
                f"        # unilab:node_uuid={PREPARE_NODE_UUID}\n"
                "        prepared = reactor.prepare("
            ),
        ),
        _source().replace("cycles=cycles,", "cycles=compute_cycles(),"),
        _source().replace(
            "from lab.devices import Reactor",
            "from .devices import Reactor",
        ),
    ]

    for source in cases:
        first = _compile(engine_context.engine, source)
        second = _compile(engine_context.engine, source)
        _assert_error_result(first)
        assert first.model_dump() == second.model_dump()


def test_generate_python_and_validate_are_pure_public_transforms(
    engine_context: EngineContext,
) -> None:
    compiled = _compile(engine_context.engine)
    assert compiled.valid and compiled.graph is not None

    generated = engine_context.engine.generate_python(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        graph=compiled.graph,
        source_uri="package://lab/workflows/generated.py",
    )
    validated = engine_context.engine.validate(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        graph=compiled.graph,
        python_source=compiled.normalized_python_source,
        source_uri="package://lab/workflows/generated.py",
    )

    assert generated.valid
    assert generated.graph == compiled.graph
    assert CandidateChangeset.model_validate(generated.changeset).kind == "source_only"
    assert validated.valid
    assert validated.graph == compiled.graph
    assert validated.normalized_python_source == compiled.normalized_python_source


def test_validate_rejects_a_source_graph_semantic_mismatch(
    engine_context: EngineContext,
) -> None:
    compiled = _compile(engine_context.engine)
    assert compiled.valid and compiled.graph is not None
    changed_source = _source().replace("cycles=cycles,", "cycles=5,")

    result = engine_context.engine.validate(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        graph=compiled.graph,
        python_source=changed_source,
        source_uri="package://lab/workflows/sample.py",
    )

    _assert_error_result(result)


def test_programming_contract_rejects_non_uuid_workflow_identity(
    engine_context: EngineContext,
) -> None:
    with pytest.raises(ValueError):
        engine_context.engine.compile(
            workflow_uuid="not-a-uuid",
            workflow_revision=7,
            python_source=_source(),
            source_uri="package://lab/workflows/sample.py",
            applied_graph=_empty_graph(),
        )
