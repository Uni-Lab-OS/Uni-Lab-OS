"""RED contracts for pure Code/Canonical authoring services."""

from __future__ import annotations

import ast
import copy
import sys
from typing import Any

import pytest

from unilabos.workflow.bindings import binding_node_dependencies
from unilabos.workflow.canonical import WorkflowRevision
from unilabos.workflow.contracts import validate_workflow_revision

from .authoring_test_support import (
    BASE_REVISION_ID,
    CONTROL_ACTION_CATALOG,
    CONTROL_FLOW_SOURCE,
    GOLDEN_SOURCE,
    GOLDEN_SOURCE_URI,
    as_mapping,
    authoring_request,
    compile_result,
    error_diagnostics,
    golden_action_catalog,
    require_authoring_functions,
)


def _candidate(result: dict[str, Any]) -> dict[str, Any]:
    candidate = result.get("candidate")
    assert isinstance(candidate, dict), result
    return candidate


def test_ten_step_package_python_compiles_to_valid_authoring_revision() -> None:
    result = compile_result()
    candidate = _candidate(result)

    assert result["base_revision_id"] == BASE_REVISION_ID
    assert error_diagnostics(result) == []
    assert validate_workflow_revision(candidate) == candidate
    assert candidate["parent_revision_id"] == BASE_REVISION_ID
    assert candidate["authoring_surface"] == "code"
    assert candidate["python_source"] == GOLDEN_SOURCE
    assert candidate["canonical_ir"]["source_artifact"]["uri"] == GOLDEN_SOURCE_URI
    assert len(candidate["canonical_ir"]["invocations"]) >= 10
    assert (
        candidate["content_hash"]
        == WorkflowRevision.model_validate(candidate["canonical_ir"]).content_hash
    )
    assert candidate["source_map"]
    for span in candidate["source_map"]:
        assert span["node_id"]
        assert span["start_line"] >= 1
        assert span["start_column"] >= 1
        assert span["end_line"] >= span["start_line"]
        assert span["end_column"] >= 1
    assert not {
        "dispatch",
        "execution_request",
        "run",
        "transport_destination",
    }.intersection(candidate)


def test_valid_python_is_parsed_without_importing_or_executing_author_source() -> None:
    sentinel_module = "unilab_authoring_source_must_never_be_imported"
    source = f"""\
import {sentinel_module}
from unilabos.workflow.authoring import workflow_definition

@workflow_definition(workflow_id="ast_only", revision="authoring-v1")
def ast_only() -> None:
    station.cleanup()
"""
    assert sentinel_module not in sys.modules

    result = compile_result(
        request=authoring_request(
            source=source,
            source_uri="packages/generic_station/generic_station/workflows/ast_only.py",
        ),
        action_catalog=CONTROL_ACTION_CATALOG,
    )

    assert error_diagnostics(result) == []
    assert (
        _candidate(result)["canonical_ir"]["invocations"][0]["action_ref"]
        == "station.cleanup"
    )
    assert sentinel_module not in sys.modules


@pytest.mark.parametrize(
    ("case_name", "source"),
    [
        (
            "syntax",
            '@workflow_definition(workflow_id="bad", revision="v1")\n'
            "def bad(:\n"
            "    pass\n",
        ),
        (
            "while",
            '@workflow_definition(workflow_id="bad", revision="v1")\n'
            "def bad():\n"
            "    while True:\n"
            "        station.cleanup()\n",
        ),
        (
            "try-except",
            '@workflow_definition(workflow_id="bad", revision="v1")\n'
            "def bad():\n"
            "    try:\n"
            "        station.cleanup()\n"
            "    except Exception:\n"
            "        station.cleanup()\n",
        ),
        (
            "dynamic-import",
            '@workflow_definition(workflow_id="bad", revision="v1")\n'
            "def bad():\n"
            '    __import__("unsafe.module")\n',
        ),
        (
            "eval",
            '@workflow_definition(workflow_id="bad", revision="v1")\n'
            "def bad():\n"
            '    eval("station.cleanup()")\n',
        ),
        (
            "exec",
            '@workflow_definition(workflow_id="bad", revision="v1")\n'
            "def bad():\n"
            '    exec("station.cleanup()")\n',
        ),
    ],
)
def test_unsupported_python_returns_location_bearing_diagnostics(
    case_name: str,
    source: str,
) -> None:
    result = compile_result(
        request=authoring_request(
            source=source,
            source_uri=f"packages/generic/workflows/{case_name}.py",
        ),
        action_catalog=CONTROL_ACTION_CATALOG,
    )

    assert result["candidate"] is None
    errors = error_diagnostics(result)
    assert errors, result
    diagnostic = errors[0]
    assert diagnostic["code"]
    assert diagnostic["message"]
    assert diagnostic["start_line"] >= 1
    assert diagnostic["start_column"] >= 1
    assert diagnostic["end_line"] >= diagnostic["start_line"]
    assert diagnostic["end_column"] >= 1


@pytest.mark.parametrize(
    ("case_name", "invalid_statement"),
    [
        ("unknown-action", "station.not_installed(sample_id=sample_id)"),
        ("positional-action-argument", "station.prepare(sample_id)"),
    ],
)
def test_compile_semantic_errors_point_to_the_actual_ast_statement(
    case_name: str,
    invalid_statement: str,
) -> None:
    source = f'''\
from unilabos.workflow.authoring import workflow_definition

@workflow_definition(workflow_id="located_error", revision="authoring-v1")
def located_error(sample_id: str) -> None:
    """The invalid action is intentionally on line six."""
    {invalid_statement}
'''
    result = compile_result(
        request=authoring_request(
            source=source,
            source_uri=f"packages/generic/workflows/{case_name}.py",
        ),
        action_catalog=CONTROL_ACTION_CATALOG,
    )

    assert result["candidate"] is None
    errors = error_diagnostics(result)
    assert errors, result
    diagnostic = errors[0]
    assert diagnostic["code"] == "PYTHON_COMPILE_ERROR"
    assert diagnostic["start_line"] == 6
    assert diagnostic["start_column"] == 5
    assert diagnostic["end_line"] == 6
    assert diagnostic["end_column"] > diagnostic["start_column"]


def test_compile_rejects_unknown_action_parameter_at_the_action_span() -> None:
    source = '''\
from unilabos.workflow.authoring import workflow_definition

@workflow_definition(workflow_id="located_parameter_error", revision="authoring-v1")
def located_parameter_error() -> None:
    """The misspelled parameter is intentionally on line six."""
    station.cleanup(typo_parameter=1)
'''
    result = compile_result(
        request=authoring_request(
            source=source,
            source_uri="packages/generic/workflows/unknown-parameter.py",
        ),
        action_catalog=CONTROL_ACTION_CATALOG,
    )

    assert result["candidate"] is None
    errors = error_diagnostics(result)
    assert errors, result
    diagnostic = errors[0]
    assert "typo_parameter" in diagnostic["message"]
    assert diagnostic["start_line"] == 6
    assert diagnostic["start_column"] == 5
    assert diagnostic["end_line"] == 6
    assert diagnostic["end_column"] > diagnostic["start_column"]


def test_validate_rejects_unknown_action_parameter_at_the_source_span() -> None:
    _, validate_revision, _ = require_authoring_functions()
    source = '''\
from unilabos.workflow.authoring import workflow_definition

@workflow_definition(workflow_id="validate_parameter_error", revision="authoring-v1")
def validate_parameter_error() -> None:
    """The action is intentionally on line six."""
    station.cleanup()
'''
    compiled = _candidate(
        compile_result(
            request=authoring_request(
                source=source,
                source_uri="packages/generic/workflows/validate-parameter.py",
            ),
            action_catalog=CONTROL_ACTION_CATALOG,
        )
    )
    cleanup = next(
        invocation
        for invocation in compiled["canonical_ir"]["invocations"]
        if invocation["action_ref"] == "station.cleanup"
    )
    cleanup["input_bindings"]["typo_parameter"] = {
        "kind": "literal",
        "value": 1,
    }
    compiled["content_hash"] = WorkflowRevision.model_validate(
        compiled["canonical_ir"]
    ).content_hash
    cleanup_span = next(
        span for span in compiled["source_map"] if span["node_id"] == cleanup["node_id"]
    )

    result = as_mapping(
        validate_revision(
            {
                "base_revision_id": BASE_REVISION_ID,
                "candidate": compiled,
            },
            action_catalog=CONTROL_ACTION_CATALOG,
        )
    )

    assert result["candidate"] is None
    errors = error_diagnostics(result)
    assert errors, result
    diagnostic = errors[0]
    assert "typo_parameter" in diagnostic["message"]
    assert diagnostic["start_line"] == cleanup_span["start_line"]
    assert diagnostic["start_column"] == cleanup_span["start_column"]
    assert diagnostic["end_line"] == cleanup_span["end_line"]
    assert diagnostic["end_column"] == cleanup_span["end_column"]


def test_validate_fails_closed_on_unknown_actions_and_dangling_bindings() -> None:
    _, validate_revision, _ = require_authoring_functions()
    compiled = _candidate(compile_result())

    unknown_action = copy.deepcopy(compiled)
    unknown_action["canonical_ir"]["invocations"][0]["action_ref"] = (
        "uninstalled_device.dispatch"
    )
    unknown_action["content_hash"] = WorkflowRevision.model_validate(
        unknown_action["canonical_ir"]
    ).content_hash
    unknown_result = as_mapping(
        validate_revision(
            {
                "base_revision_id": BASE_REVISION_ID,
                "candidate": unknown_action,
            },
            action_catalog=golden_action_catalog(),
        )
    )
    assert unknown_result["candidate"] is None
    assert any(
        "unknown" in diagnostic["message"].lower()
        or diagnostic["code"] == "UNKNOWN_ACTION"
        for diagnostic in error_diagnostics(unknown_result)
    )

    dangling = copy.deepcopy(compiled)
    dangling["canonical_ir"]["invocations"][1]["input_bindings"] = {
        "sample": {
            "kind": "node_output",
            "node_id": "missing-node",
            "output": "sample",
        }
    }
    dangling_result = as_mapping(
        validate_revision(
            {
                "base_revision_id": BASE_REVISION_ID,
                "candidate": dangling,
            },
            action_catalog=golden_action_catalog(),
        )
    )
    assert dangling_result["candidate"] is None
    assert any(
        "missing-node" in diagnostic["message"]
        or "dangling" in diagnostic["code"].lower()
        or "unknown" in diagnostic["code"].lower()
        for diagnostic in error_diagnostics(dangling_result)
    )


def test_static_control_flow_whitelist_includes_group_parallel_and_cleanup() -> None:
    result = compile_result(
        request=authoring_request(
            source=CONTROL_FLOW_SOURCE,
            source_uri="packages/generic_station/generic_station/workflows/control_flow.py",
        ),
        action_catalog={
            **CONTROL_ACTION_CATALOG,
            "host_node.manual_confirm": {
                "inputs": {"prompt": {"type": "string"}},
                "outputs": {},
            },
        },
    )
    candidate = _candidate(result)

    assert error_diagnostics(result) == []
    ast.parse(candidate["python_source"])
    invocations = candidate["canonical_ir"]["invocations"]
    node_types = {invocation.get("node_type", "action") for invocation in invocations}
    action_refs = {invocation["action_ref"] for invocation in invocations}
    assert {"group", "fork", "join", "branch"}.issubset(node_types)
    assert "host_node.manual_confirm" in action_refs
    assert any(invocation.get("cleanup_for") for invocation in invocations)

    revision = WorkflowRevision.model_validate(candidate["canonical_ir"])
    prepare = next(
        invocation
        for invocation in revision.invocations
        if invocation.action_ref == "station.prepare"
    )
    mixes = [
        invocation
        for invocation in revision.invocations
        if invocation.action_ref == "station.mix"
    ]
    inspections = [
        invocation
        for invocation in revision.invocations
        if invocation.action_ref == "station.inspect"
    ]
    branch = next(
        invocation
        for invocation in revision.invocations
        if invocation.node_type == "branch"
    )

    assert len(mixes) == 2
    assert [invocation.input_bindings["amount"].value for invocation in mixes] == [1, 2]
    assert all(
        prepare.node_id
        in binding_node_dependencies(invocation.input_bindings["sample"])
        for invocation in mixes
    )
    assert {invocation.node_id for invocation in inspections}.issubset(
        binding_node_dependencies(branch.input_bindings["condition"])
    )
