"""Round 02D 独立评审阻塞项的 public-seam 回归合同。"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import pytest
from pydantic import ValidationError

from unilabos.workflow.models import CandidateCompilation, CandidateDiagnostic

from .test_authoring_engine import (
    ANALYZE_NODE_UUID,
    PREPARE_CYCLES_TARGET,
    PREPARE_NODE_UUID,
    PREPARE_TEMPLATE_UUID,
    WORKFLOW_UUID,
    EngineContext,
    _assert_error_result,
    _catalog_imports,
    _compile,
    _node_by_uuid,
    _opened_engine,
    _source,
)
from .test_authoring_roundtrip import _group_source


@pytest.fixture()
def engine_context(tmp_path: Path) -> Iterator[EngineContext]:
    with _opened_engine(tmp_path / "workflow.db") as context:
        yield context


def _two_selector_source() -> str:
    return f'''from lab.devices import Reactor
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import device, workflow_definition, workflow_output


reactor: Reactor = device()
fixed_reactor: Reactor = device("reactor-1")


@workflow_definition(
    workflow_uuid="{WORKFLOW_UUID}",
    displayname="Two selectors",
)
def two_selectors(*, sample: ResourceSlot):
    # unilab:node_uuid={PREPARE_NODE_UUID}
    first = reactor.prepare(sample=sample, cycles=1, note=None)
    # unilab:node_uuid={ANALYZE_NODE_UUID}
    second = fixed_reactor.prepare(sample=sample, cycles=2, note=None)
    return workflow_output(report=second.report)
'''


def _generate(engine: Any, graph: dict[str, Any]) -> CandidateCompilation:
    return engine.generate_python(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        graph=graph,
        source_uri="package://lab/workflows/review.py",
    )


def _validate(
    engine: Any,
    graph: dict[str, Any],
    source: str,
) -> CandidateCompilation:
    return engine.validate(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        graph=graph,
        python_source=source,
        source_uri="package://lab/workflows/review.py",
    )


def _prepare_template(imports: list[Any]) -> Any:
    return next(
        item for item in imports if item.template["uuid"] == PREPARE_TEMPLATE_UUID
    )


def _without_prepare_keyword(source: str, keyword: str) -> str:
    return source.replace(f"        {keyword}={keyword},\n", "", 1)


def _range_key(source_range: Mapping[str, Any]) -> tuple[int, int, int, int]:
    return (
        source_range["start_line"],
        source_range["start_column"],
        source_range["end_line"],
        source_range["end_column"],
    )


def test_generate_python_is_invariant_to_graph_node_array_order(
    engine_context: EngineContext,
) -> None:
    compiled = _compile(engine_context.engine, _two_selector_source())
    assert compiled.valid and compiled.graph is not None
    original = deepcopy(compiled.graph)
    reversed_nodes = deepcopy(compiled.graph)
    reversed_nodes["nodes"].reverse()

    first = _generate(engine_context.engine, original)
    second = _generate(engine_context.engine, reversed_nodes)

    assert first.valid
    assert second.valid
    assert second.diagnostics == first.diagnostics == []
    assert second.normalized_python_source == first.normalized_python_source
    assert second.source_map == first.source_map
    assert second.changeset == first.changeset


@pytest.mark.parametrize(
    ("fixed_device_id", "expected_executor_binding"),
    [
        (None, None),
        (
            "reactor-1",
            {"mode": "fixed", "device_id": "reactor-1"},
        ),
    ],
    ids=["unbound", "fixed"],
)
def test_device_action_selector_does_not_require_preadmitted_material(
    tmp_path: Path,
    fixed_device_id: str | None,
    expected_executor_binding: dict[str, str] | None,
) -> None:
    imports = _catalog_imports()
    for item in imports:
        if item.template["type"] == "action":
            item.template["node_type"] = "device_action"

    with _opened_engine(tmp_path / "device-action.db", imports=imports) as context:
        result = _compile(
            context.engine,
            _source(fixed_device_id=fixed_device_id),
        )

    assert result.valid, result.diagnostics
    assert result.graph is not None
    for node_uuid in (PREPARE_NODE_UUID, ANALYZE_NODE_UUID):
        node = _node_by_uuid(result.graph, node_uuid)
        assert node.get("material_uuid") is None
        assert "device_id" not in node
        unilab = node["meta_data"]["unilab"]
        if expected_executor_binding is None:
            assert "executor_binding" not in unilab
        else:
            assert unilab["executor_binding"] == expected_executor_binding


@pytest.mark.parametrize(
    "malformed_anchor",
    [
        "# unilab:node_uuid=",
        f"# unilab:node_uuid = {PREPARE_NODE_UUID}",
        f"# unilab:node_uuid={PREPARE_NODE_UUID} trailing",
        f"# unilab:node_uuid:{PREPARE_NODE_UUID}",
        "# unilab:node_uuid",
        f"# unilab:node_uuid=={PREPARE_NODE_UUID}",
        "# unilab:node_uuid=not-a-uuid",
    ],
    ids=[
        "empty",
        "space-before-equals",
        "trailing-content",
        "colon-separator",
        "missing-equals",
        "double-equals",
        "invalid-uuid",
    ],
)
def test_every_anchor_like_malformed_comment_fails_closed(
    engine_context: EngineContext,
    malformed_anchor: str,
) -> None:
    source = _source().replace(
        f"# unilab:node_uuid={PREPARE_NODE_UUID}",
        malformed_anchor,
        1,
    )

    result = _compile(engine_context.engine, source)

    _assert_error_result(result, code="invalid_node_anchor")


GraphMutation = Callable[[dict[str, Any]], None]


def _drop_workflow_name(graph: dict[str, Any]) -> None:
    del graph["workflow"]["name"]


def _replace_workflow_metadata_with_array(graph: dict[str, Any]) -> None:
    graph["workflow"]["meta_data"] = []


@pytest.mark.parametrize(
    "operation",
    ["compile", "generate_python", "validate"],
)
@pytest.mark.parametrize(
    "mutation",
    [_drop_workflow_name, _replace_workflow_metadata_with_array],
    ids=["missing-workflow-name", "workflow-metadata-array"],
)
def test_bad_graph_public_transforms_return_diagnostic_instead_of_python_error(
    engine_context: EngineContext,
    operation: Literal["compile", "generate_python", "validate"],
    mutation: GraphMutation,
) -> None:
    compiled = _compile(engine_context.engine)
    assert compiled.valid and compiled.graph is not None
    graph = deepcopy(compiled.graph)
    mutation(graph)

    if operation == "compile":
        result = _compile(engine_context.engine, graph=graph)
    elif operation == "generate_python":
        result = _generate(engine_context.engine, graph)
    else:
        result = _validate(engine_context.engine, graph, _source())

    _assert_error_result(result, code="candidate_invalid")


@pytest.mark.parametrize(
    ("goal_default", "goal", "cycles_required", "expected_param"),
    [
        ({"cycles": 2}, {"cycles": 3}, True, {"cycles": 2}),
        ({}, {"cycles": 3}, True, {"cycles": 3}),
        ({}, {}, False, {}),
    ],
    ids=["goal-default", "goal-fallback", "empty-fallback"],
)
def test_action_param_fallback_is_goal_default_then_goal_then_empty_object(
    tmp_path: Path,
    goal_default: dict[str, Any],
    goal: dict[str, Any],
    cycles_required: bool,
    expected_param: dict[str, Any],
) -> None:
    imports = _catalog_imports()
    prepare = _prepare_template(imports)
    prepare.template["goal_default"] = goal_default
    prepare.template["goal"] = goal
    for handle in prepare.handles:
        if handle["uuid"] == PREPARE_CYCLES_TARGET:
            handle["required"] = cycles_required
    source = _without_prepare_keyword(_source(), "cycles")

    with _opened_engine(tmp_path / "fallback.db", imports=imports) as context:
        result = _compile(context.engine, source)

    assert result.valid and result.graph is not None
    assert _node_by_uuid(result.graph, PREPARE_NODE_UUID)["param"] == expected_param


def test_explicit_value_and_workflow_binding_override_template_fallback(
    tmp_path: Path,
) -> None:
    imports = _catalog_imports()
    prepare = _prepare_template(imports)
    prepare.template["goal_default"] = {
        "cycles": 2,
        "note": "catalog default",
    }
    prepare.template["goal"] = {"cycles": 3, "note": "goal fallback"}
    explicit_source = (
        _source()
        .replace("cycles=cycles,", "cycles=9,", 1)
        .replace(
            "note=note,",
            'note="explicit",',
            1,
        )
    )

    with _opened_engine(tmp_path / "override.db", imports=imports) as context:
        explicit = _compile(context.engine, explicit_source)
        bound = _compile(context.engine, _source())

    assert explicit.valid and explicit.graph is not None
    assert _node_by_uuid(explicit.graph, PREPARE_NODE_UUID)["param"] == {
        "cycles": 9,
        "note": "explicit",
    }
    assert bound.valid and bound.graph is not None
    bound_node = _node_by_uuid(bound.graph, PREPARE_NODE_UUID)
    assert bound_node["param"] == {}
    assert set(bound_node["meta_data"]["unilab"]["input_bindings"]) >= {
        PREPARE_CYCLES_TARGET
    }


@pytest.mark.parametrize(
    ("source", "expected_code"),
    [
        (
            _source().replace(
                f'    workflow_uuid="{WORKFLOW_UUID}",',
                (
                    '    workflow_uuid="10000000-0000-4000-8000-000000000002",\n'
                    f'    workflow_uuid="{WORKFLOW_UUID}",'
                ),
                1,
            ),
            "invalid_workflow_declaration",
        ),
        (
            _group_source().replace(
                'with group(name="Preparation"):',
                'with group(name="Ignored", name="Preparation"):',
                1,
            ),
            "invalid_group",
        ),
    ],
    ids=["workflow-definition", "group"],
)
def test_authoring_markers_reject_duplicate_keywords(
    engine_context: EngineContext,
    source: str,
    expected_code: str,
) -> None:
    result = _compile(engine_context.engine, source)

    _assert_error_result(result, code=expected_code)


def test_duplicate_anchor_diagnostic_has_machine_applicable_uuid4_alternatives(
    engine_context: EngineContext,
) -> None:
    source = _source().replace(ANALYZE_NODE_UUID, PREPARE_NODE_UUID)

    result = _compile(engine_context.engine, source)

    _assert_error_result(result)
    duplicate_diagnostics = [
        item for item in result.diagnostics if item["code"] == "DUPLICATE_NODE_UUID"
    ]
    assert len(duplicate_diagnostics) == 1, result.diagnostics
    duplicate = duplicate_diagnostics[0]
    assert duplicate["duplicate_uuid"] == PREPARE_NODE_UUID
    occurrences = duplicate["occurrence_ranges"]
    alternatives = duplicate["repair_alternatives"]
    assert len(occurrences) == 2
    assert len({_range_key(item) for item in occurrences}) == 2
    assert {
        source.splitlines()[item["start_line"] - 1].strip() for item in occurrences
    } == {f"# unilab:node_uuid={PREPARE_NODE_UUID}"}
    assert len(alternatives) == 2
    assert {_range_key(item["retained_range"]) for item in alternatives} == {
        _range_key(item) for item in occurrences
    }

    replacement_uuids: list[str] = []
    occurrence_keys = {_range_key(item) for item in occurrences}
    for alternative in alternatives:
        retained = _range_key(alternative["retained_range"])
        replacements = alternative["replacements"]
        assert len(replacements) == 1
        assert {
            _range_key(item["source_range"]) for item in replacements
        } == occurrence_keys - {retained}
        replacement_uuid = replacements[0]["replacement_uuid"]
        parsed = UUID(replacement_uuid)
        assert parsed.version == 4
        assert replacement_uuid != PREPARE_NODE_UUID
        replacement_uuids.append(replacement_uuid)
    assert len(set(replacement_uuids)) == len(replacement_uuids)

    validated = CandidateDiagnostic.model_validate(duplicate)
    assert validated.model_dump(exclude_none=True) == duplicate
    with pytest.raises(ValidationError):
        CandidateDiagnostic.model_validate(
            {**duplicate, "details": {"arbitrary": True}}
        )
