"""I1 canonical Workflow result-record 的 public authoring tracer。"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow.models import CandidateChangeset

from .test_authoring_engine import (
    ANALYZE_NODE_UUID,
    ANALYZE_REPORT_SOURCE,
    PREPARE_NODE_UUID,
    PREPARE_SAMPLE_SOURCE,
    WORKFLOW_UUID,
    EngineContext,
    _empty_graph,
    _opened_engine,
)


def _typed_result_source() -> str:
    return f'''from typing import TypedDict

from lab.devices import Reactor
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import device, workflow_definition


class SamplePreparationResult(TypedDict):
    sample: ResourceSlot
    report: str


reactor: Reactor = device()


@workflow_definition(
    workflow_uuid="{WORKFLOW_UUID}",
    displayname="Sample preparation",
)
def prepare_sample(*, sample: ResourceSlot) -> SamplePreparationResult:
    # unilab:node_uuid={PREPARE_NODE_UUID}
    prepared = reactor.prepare(sample=sample, cycles=1, note=None)
    # unilab:node_uuid={ANALYZE_NODE_UUID}
    analyzed = reactor.analyze(prepared=prepared.prepared, label="typed")
    return {{"sample": prepared.prepared, "report": analyzed.report}}
'''


@pytest.fixture()
def engine_context(tmp_path: Path) -> Iterator[EngineContext]:
    with _opened_engine(tmp_path / "workflow.db") as context:
        yield context


def _workflow_io(graph: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    unilab = graph["workflow"]["meta_data"]["unilab"]
    return unilab["output_contract"], unilab["output_bindings"]


def _assert_canonical_typed_dict_source(source: str) -> None:
    assert "workflow_output" not in source
    module = ast.parse(source)
    result_records = [
        statement
        for statement in module.body
        if isinstance(statement, ast.ClassDef)
        and len(statement.bases) == 1
        and isinstance(statement.bases[0], ast.Name)
        and statement.bases[0].id == "TypedDict"
    ]
    assert len(result_records) == 1
    result_record = result_records[0]
    fields = [
        statement
        for statement in result_record.body
        if isinstance(statement, ast.AnnAssign)
    ]
    assert [
        field.target.id for field in fields if isinstance(field.target, ast.Name)
    ] == [
        "sample",
        "report",
    ]
    assert [ast.unparse(field.annotation) for field in fields] == [
        "ResourceSlot",
        "str",
    ]

    workflows = [
        statement for statement in module.body if isinstance(statement, ast.FunctionDef)
    ]
    assert len(workflows) == 1
    workflow = workflows[0]
    assert isinstance(workflow.returns, ast.Name)
    assert workflow.returns.id == result_record.name
    final = workflow.body[-1]
    assert isinstance(final, ast.Return)
    assert isinstance(final.value, ast.Dict)
    assert [key.value for key in final.value.keys if isinstance(key, ast.Constant)] == [
        "sample",
        "report",
    ]
    assert [ast.unparse(value) for value in final.value.values] == [
        "prepared.prepared",
        "analyzed.report",
    ]


def test_typed_result_record_compile_generate_compile_is_a_fixed_point(
    engine_context: EngineContext,
) -> None:
    compiled = engine_context.engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=_typed_result_source(),
        source_uri="package://lab/workflows/typed-result.py",
        applied_graph=_empty_graph(),
    )

    assert compiled.valid and compiled.graph is not None, compiled.diagnostics
    expected_contract = {
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
    expected_bindings = {
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
    assert _workflow_io(compiled.graph) == (expected_contract, expected_bindings)

    generated = engine_context.engine.generate_python(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        graph=compiled.graph,
        source_uri="package://lab/workflows/typed-result.py",
    )

    assert generated.valid, generated.diagnostics
    assert generated.normalized_python_source is not None
    _assert_canonical_typed_dict_source(generated.normalized_python_source)

    recompiled = engine_context.engine.compile(
        workflow_uuid=WORKFLOW_UUID,
        workflow_revision=7,
        python_source=generated.normalized_python_source,
        source_uri="package://lab/workflows/typed-result.py",
        applied_graph=compiled.graph,
    )

    assert recompiled.valid and recompiled.graph is not None, recompiled.diagnostics
    assert _workflow_io(recompiled.graph) == (expected_contract, expected_bindings)
    assert recompiled.normalized_python_source == generated.normalized_python_source
    assert CandidateChangeset.model_validate(recompiled.changeset).kind == "source_only"
