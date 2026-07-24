"""RED contracts for deterministic Canonical-to-Python generation."""

from __future__ import annotations

import ast
import copy
from typing import Any, Mapping

from unilabos.workflow.canonical import WorkflowRevision

from .authoring_test_support import (
    BASE_REVISION_ID,
    CONTROL_ACTION_CATALOG,
    CONTROL_FLOW_SOURCE,
    GOLDEN_SOURCE_URI,
    as_mapping,
    authoring_request,
    compile_result,
    error_diagnostics,
    golden_action_catalog,
    require_authoring_functions,
)


def _candidate(result: Mapping[str, Any]) -> dict[str, Any]:
    candidate = result.get("candidate")
    assert isinstance(candidate, dict), result
    return candidate


def _generation_request(canonical_ir: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "base_revision_id": BASE_REVISION_ID,
        "canonical_ir": copy.deepcopy(dict(canonical_ir)),
        "source_uri": GOLDEN_SOURCE_URI,
    }


def _action_calls(source: str) -> list[ast.Call]:
    tree = ast.parse(source)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]


def _assigned_action_names(source: str, action_ref: str) -> list[str]:
    tree = ast.parse(source)
    assigned_names: list[str] = []
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.Assign)
            or len(node.targets) != 1
            or not isinstance(node.targets[0], ast.Name)
            or not isinstance(node.value, ast.Call)
            or not isinstance(node.value.func, ast.Attribute)
            or not isinstance(node.value.func.value, ast.Name)
        ):
            continue
        call_ref = f"{node.value.func.value.id}.{node.value.func.attr}"
        if call_ref == action_ref:
            assigned_names.append(node.targets[0].id)
    return assigned_names


def _pure_canonical_ir(candidate: Mapping[str, Any]) -> dict[str, Any]:
    canonical_ir = copy.deepcopy(candidate["canonical_ir"])
    canonical_ir.pop("source_artifact", None)
    return canonical_ir


def test_generation_is_deterministic_readable_and_keyword_only() -> None:
    _, _, generate_revision = require_authoring_functions()
    compiled = _candidate(compile_result())
    request = _generation_request(compiled["canonical_ir"])

    first = as_mapping(
        generate_revision(request, action_catalog=golden_action_catalog())
    )
    second = as_mapping(
        generate_revision(request, action_catalog=golden_action_catalog())
    )
    first_candidate = _candidate(first)
    second_candidate = _candidate(second)

    assert first_candidate["python_source"] == second_candidate["python_source"]
    assert first_candidate["source_map"] == second_candidate["source_map"]
    assert first_candidate["python_source"].strip()
    assert "#" in first_candidate["python_source"]
    assert "result_1" not in first_candidate["python_source"]
    assert all(
        not call.args for call in _action_calls(first_candidate["python_source"])
    )
    assert all(
        span["start_line"] >= 1
        and span["start_column"] >= 1
        and span["end_line"] >= span["start_line"]
        for span in first_candidate["source_map"]
    )


def test_python_ir_python_ir_roundtrip_preserves_execution_hash() -> None:
    compile_revision, _, generate_revision = require_authoring_functions()
    compiled = _candidate(compile_result())
    canonical_with_different_layout = copy.deepcopy(compiled["canonical_ir"])
    canonical_with_different_layout["layout"] = {
        invocation["node_id"]: {"x": index * 100, "y": index * 20}
        for index, invocation in enumerate(
            canonical_with_different_layout["invocations"]
        )
    }

    generated = as_mapping(
        generate_revision(
            _generation_request(canonical_with_different_layout),
            action_catalog=golden_action_catalog(),
        )
    )
    generated_candidate = _candidate(generated)
    recompiled = as_mapping(
        compile_revision(
            {
                "base_revision_id": BASE_REVISION_ID,
                "python_source": generated_candidate["python_source"],
                "source_uri": GOLDEN_SOURCE_URI,
            },
            action_catalog=golden_action_catalog(),
        )
    )
    recompiled_candidate = _candidate(recompiled)

    assert error_diagnostics(generated) == []
    assert error_diagnostics(recompiled) == []
    assert generated_candidate["content_hash"] == compiled["content_hash"]
    assert recompiled_candidate["content_hash"] == compiled["content_hash"]
    assert (
        WorkflowRevision.model_validate(canonical_with_different_layout).content_hash
        == compiled["content_hash"]
    )


def test_control_flow_graph_python_ir_roundtrip_preserves_execution_hash() -> None:
    compile_revision, _, generate_revision = require_authoring_functions()
    action_catalog = {
        **CONTROL_ACTION_CATALOG,
        "host_node.manual_confirm": {
            "inputs": {
                "prompt": {"type": "string"},
                "on_cancel": {"type": "string", "default": "raise"},
            },
            "outputs": {},
        },
    }
    compiled = _candidate(
        compile_result(
            request=authoring_request(
                source=CONTROL_FLOW_SOURCE,
                source_uri=(
                    "packages/generic_station/generic_station/workflows/control_flow.py"
                ),
            ),
            action_catalog=action_catalog,
        )
    )
    compiled_types = {
        invocation.get("node_type", "action")
        for invocation in compiled["canonical_ir"]["invocations"]
    }
    assert {"group", "fork", "join", "branch"}.issubset(compiled_types)
    assert {
        edge.get("branch") for edge in compiled["canonical_ir"]["control_edges"]
    }.issuperset({"true"})

    pure_canonical_ir = _pure_canonical_ir(compiled)
    generated = as_mapping(
        generate_revision(
            _generation_request(pure_canonical_ir),
            action_catalog=action_catalog,
        )
    )
    generated_candidate = _candidate(generated)

    assert error_diagnostics(generated) == []
    assert "unsupported_node(" not in generated_candidate["python_source"]
    ast.parse(generated_candidate["python_source"])
    assert "with group(" in generated_candidate["python_source"]
    assert "with parallel(" in generated_candidate["python_source"]
    assert "if " in generated_candidate["python_source"]

    recompiled = as_mapping(
        compile_revision(
            {
                "base_revision_id": BASE_REVISION_ID,
                "python_source": generated_candidate["python_source"],
                "source_uri": GOLDEN_SOURCE_URI,
            },
            action_catalog=action_catalog,
        )
    )
    recompiled_candidate = _candidate(recompiled)

    assert error_diagnostics(recompiled) == []
    assert recompiled_candidate["content_hash"] == compiled["content_hash"]
    assert {
        invocation.get("node_type", "action")
        for invocation in recompiled_candidate["canonical_ir"]["invocations"]
    } == compiled_types


REPEATED_ACTION_SOURCE = """\
from unilabos.workflow.authoring import workflow_definition

@workflow_definition(workflow_id="repeated_actions", revision="authoring-v1")
def repeated_actions(sample_id: str) -> None:
    first = station.inspect(sample=sample_id)
    second = station.inspect(sample=sample_id)
    station.finish(sample=first)
    station.finish(sample=second)
"""

NESTED_PARALLEL_SOURCE = """\
from unilabos.workflow.authoring import parallel, workflow_definition

@workflow_definition(workflow_id="nested_parallel", revision="authoring-v1")
def nested_parallel(sample_id: str) -> None:
    prepared = station.prepare(sample_id=sample_id)
    with parallel():
        station.inspect(sample=prepared.sample)
        with parallel():
            station.inspect(sample=prepared.sample)
            station.inspect(sample=prepared.sample)
        station.finish(sample=prepared.sample)
"""


def _repeated_action_generation() -> tuple[
    Any,
    dict[str, dict[str, Any]],
    dict[str, Any],
    dict[str, Any],
]:
    compile_revision, _, generate_revision = require_authoring_functions()
    action_catalog = copy.deepcopy(CONTROL_ACTION_CATALOG)
    compiled_result = as_mapping(
        compile_revision(
            authoring_request(
                source=REPEATED_ACTION_SOURCE,
                source_uri=(
                    "packages/generic_station/generic_station/workflows/"
                    "repeated_actions.py"
                ),
            ),
            action_catalog=action_catalog,
        )
    )
    compiled = _candidate(compiled_result)
    generated = as_mapping(
        generate_revision(
            _generation_request(_pure_canonical_ir(compiled)),
            action_catalog=action_catalog,
        )
    )
    return compile_revision, action_catalog, compiled, generated


def test_pure_ir_repeated_action_refs_allocate_unique_result_variables() -> None:
    _, _, _, generated = _repeated_action_generation()
    generated_candidate = _candidate(generated)

    assert error_diagnostics(generated) == []
    assigned_names = _assigned_action_names(
        generated_candidate["python_source"],
        "station.inspect",
    )
    assert len(assigned_names) == 2
    assert len(set(assigned_names)) == len(assigned_names)


def test_pure_ir_repeated_action_refs_roundtrip_without_silent_hash_drift() -> None:
    compile_revision, action_catalog, compiled, generated = (
        _repeated_action_generation()
    )
    generated_candidate = _candidate(generated)
    recompiled = as_mapping(
        compile_revision(
            {
                "base_revision_id": BASE_REVISION_ID,
                "python_source": generated_candidate["python_source"],
                "source_uri": GOLDEN_SOURCE_URI,
            },
            action_catalog=action_catalog,
        )
    )
    recompiled_candidate = _candidate(recompiled)

    assert error_diagnostics(generated) == []
    assert error_diagnostics(recompiled) == []
    assert recompiled_candidate["content_hash"] == compiled["content_hash"]


def test_nested_parallel_pure_ir_without_source_hints_roundtrips_hash() -> None:
    compile_revision, _, generate_revision = require_authoring_functions()
    action_catalog = copy.deepcopy(CONTROL_ACTION_CATALOG)
    compiled_result = as_mapping(
        compile_revision(
            authoring_request(
                source=NESTED_PARALLEL_SOURCE,
                source_uri=(
                    "packages/generic_station/generic_station/workflows/"
                    "nested_parallel.py"
                ),
            ),
            action_catalog=action_catalog,
        )
    )
    compiled = _candidate(compiled_result)
    pure_canonical_ir = _pure_canonical_ir(compiled)
    pure_canonical_ir["source_map"] = {"entries": []}
    node_types = [
        invocation.get("node_type", "action")
        for invocation in pure_canonical_ir["invocations"]
    ]

    assert pure_canonical_ir.get("source_artifact") is None
    assert pure_canonical_ir["source_map"]["entries"] == []
    assert node_types.count("fork") == 2
    assert node_types.count("join") == 2
    assert node_types.index("fork") < node_types.index("join")

    generated = as_mapping(
        generate_revision(
            _generation_request(pure_canonical_ir),
            action_catalog=action_catalog,
        )
    )
    generated_candidate = _candidate(generated)
    generated_error_codes = {
        diagnostic["code"] for diagnostic in error_diagnostics(generated)
    }

    assert not {
        code
        for code in generated_error_codes
        if code.startswith("UNSUPPORTED") or code.startswith("UNMATCHED")
    }
    assert generated_candidate["python_source"].count("with parallel():") == 2
    ast.parse(generated_candidate["python_source"])

    recompiled = as_mapping(
        compile_revision(
            {
                "base_revision_id": BASE_REVISION_ID,
                "python_source": generated_candidate["python_source"],
                "source_uri": GOLDEN_SOURCE_URI,
            },
            action_catalog=action_catalog,
        )
    )
    recompiled_candidate = _candidate(recompiled)

    assert error_diagnostics(recompiled) == []
    assert generated_candidate["content_hash"] == compiled["content_hash"]
    assert recompiled_candidate["content_hash"] == compiled["content_hash"]


def test_unsupported_canonical_node_is_never_silently_dropped() -> None:
    _, _, generate_revision = require_authoring_functions()
    compiled = _candidate(compile_result())
    unsupported = copy.deepcopy(compiled["canonical_ir"])
    unsupported["invocations"][0]["node_type"] = "vendor_extension"

    result = as_mapping(
        generate_revision(
            _generation_request(unsupported),
            action_catalog=golden_action_catalog(),
        )
    )
    candidate = _candidate(result)

    assert "unsupported_node(" in candidate["python_source"]
    errors = error_diagnostics(result)
    assert any(
        diagnostic["code"] == "UNSUPPORTED_NODE"
        and diagnostic.get("node_id") == unsupported["invocations"][0]["node_id"]
        for diagnostic in errors
    )
