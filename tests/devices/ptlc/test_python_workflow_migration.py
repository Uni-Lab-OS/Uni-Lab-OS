"""RED contracts for migrating the real pTLC Operations to package Python.

The checked-in YAML blobs remain the migration oracle and provenance record.
They are never accepted as the Python Code source artifact.

AI-generated code metadata:
Model: OpenAI Codex GPT-5
Generation date: 2026-07-22
Prompt summary: Lock W16/W17 pTLC Python authoring and compiler semantics.
Human review status: Pending.
"""

from __future__ import annotations

import ast
import asyncio
from collections import Counter
from collections.abc import Iterable, Mapping
import hashlib
from pathlib import Path
import textwrap
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from unilabos.app.local_bridge.local_api import LocalApiState, create_app
from unilabos.devices.generic_plc_macro import DeclarativePLCMacroDriver
from unilabos.registry.action_catalog import scan_decorated_device_package
from unilabos.runtime.profile_loader import ProfileLoader, ProfileValidationError
from unilabos.runtime.service import RuntimeService
from unilabos.scheduler.dag_model import TaskDag
from unilabos.workflow.bindings import (
    ExpressionBinding,
    LiteralValue,
    RuntimeParameterRef,
    binding_node_dependencies,
)
from unilabos.workflow.canonical import WorkflowRevision, WorkflowSourceArtifact
from unilabos.workflow.from_python_script import compile_python_script
from unilabos.workflow.operation_tree import compile_operation_tree


WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
REAL_OPERATION_ROOT = Path(__file__).parent / "fixtures" / "real_operations"
PROVENANCE_PATH = REAL_OPERATION_ROOT / "PROVENANCE.yaml"
TEMPLATE_REPOSITORY_ROOT = WORKSPACE_ROOT / "Uni-Lab-Templates"
PTLC_PACKAGE_ROOT = TEMPLATE_REPOSITORY_ROOT / "packages" / "ptlc_station"
PTLC_PROFILE_PATH = PTLC_PACKAGE_ROOT / "package.yaml"
PTLC_PYTHON_PACKAGE = PTLC_PACKAGE_ROOT / "ptlc_station"
PTLC_WORKFLOW_ROOT = PTLC_PYTHON_PACKAGE / "workflows"

WORKFLOW_FIXTURES: dict[str, str] = {
    "develop_execute": "02_develop/develop_execute.yaml",
    "develop_prepare": "02_develop/develop_prepare.yaml",
}
WORKFLOW_SOURCE_URIS: dict[str, str] = {
    name: f"packages/ptlc_station/ptlc_station/workflows/{name}.py"
    for name in WORKFLOW_FIXTURES
}
TYPE_MAP = {
    "STRING": "str",
    "INT": "int",
    "FLOAT": "float",
    "BOOL": "bool",
}
HOST_ACTION_CATALOG: dict[str, dict[str, Any]] = {
    "host_node.manual_confirm": {
        "inputs": {
            "prompt": {"type": "string", "default": ""},
            "timeout_seconds": {"type": "integer", "default": 3600},
            "assignee_user_ids": {"type": "array", "default": []},
        },
        "outputs": {},
    }
}
PYTHON_WORKFLOW_IMPORTER = {
    "schema": "unilab.python/v1",
    "kind": "workflow",
    "codec": "python_ast_v1",
}
STATIC_RANGE_EXPANSION_LIMIT = 1000
HUGE_RANGE_LITERAL = "1" + ("0" * 100)
COMPILER_SAFETY_ACTION_CATALOG: dict[str, dict[str, Any]] = {
    "pump.dose": {
        "inputs": {"amount": {"type": "integer", "required": True}},
        "outputs": {},
    },
    "pump.off": {"inputs": {}, "outputs": {}},
}


def _load_yaml(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _operation(name: str) -> dict[str, Any]:
    return _load_yaml(REAL_OPERATION_ROOT / WORKFLOW_FIXTURES[name])


def _resolve_operation(name: str) -> dict[str, Any]:
    matches = list(REAL_OPERATION_ROOT.glob(f"**/{name}.yaml"))
    assert len(matches) == 1, f"real Operation dependency is ambiguous: {name}"
    return _load_yaml(matches[0])


def _python_source(name: str) -> tuple[str, str]:
    uri = WORKFLOW_SOURCE_URIS[name]
    path = TEMPLATE_REPOSITORY_ROOT / uri
    assert path.is_file(), f"W16/W17 migrated Python workflow is missing: {uri}"
    return path.read_text(encoding="utf-8"), uri


def _workflow_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    assert len(functions) == 1, f"{name} must expose exactly one workflow function"
    function = functions[0]
    decorator = next(
        (
            item
            for item in function.decorator_list
            if isinstance(item, ast.Call)
            and isinstance(item.func, ast.Name)
            and item.func.id == "workflow_definition"
        ),
        None,
    )
    assert decorator is not None, f"{name} requires @workflow_definition"
    decorator_values = {
        keyword.arg: ast.literal_eval(keyword.value)
        for keyword in decorator.keywords
        if keyword.arg is not None
    }
    assert decorator_values.get("workflow_id") == name
    assert decorator_values.get("revision")
    return function


def _call_ref(call: ast.Call) -> str | None:
    if not isinstance(call.func, ast.Attribute):
        return None
    if not isinstance(call.func.value, ast.Name):
        return None
    return f"{call.func.value.id}.{call.func.attr}"


def _python_call_refs(tree: ast.Module) -> list[str]:
    return [
        action_ref
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and (action_ref := _call_ref(node)) is not None
    ]


def _yaml_call_refs(raw_nodes: Any) -> Iterable[str]:
    if not isinstance(raw_nodes, list):
        return
    for node in raw_nodes:
        if not isinstance(node, Mapping):
            continue
        operation = str(node.get("op") or "")
        if operation == "call":
            yield str(node["action"])
        elif operation == "human":
            yield "host_node.manual_confirm"
        elif operation == "run_script":
            dependency = _resolve_operation(str(node["script"]))
            yield from _yaml_call_refs(dependency.get("body"))
        for child_key in ("then", "else", "body", "finally"):
            yield from _yaml_call_refs(node.get(child_key))


def _external_parameter_contract(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    contract: list[dict[str, Any]] = []
    for raw in document.get("vars", []) or []:
        if not isinstance(raw, Mapping) or raw.get("io") != "in":
            continue
        entry = {
            "name": str(raw["name"]),
            "annotation": TYPE_MAP[str(raw["type"])],
            "has_default": "default" in raw,
        }
        if "default" in raw:
            entry["default"] = raw["default"]
        contract.append(entry)
    return contract


def _function_parameter_contract(function: ast.FunctionDef) -> list[dict[str, Any]]:
    arguments = [*function.args.args, *function.args.kwonlyargs]
    positional_offset = len(function.args.args) - len(function.args.defaults)
    defaults: dict[str, ast.expr] = {
        argument.arg: function.args.defaults[index - positional_offset]
        for index, argument in enumerate(function.args.args)
        if index >= positional_offset
    }
    defaults.update(
        {
            argument.arg: default
            for argument, default in zip(
                function.args.kwonlyargs,
                function.args.kw_defaults,
            )
            if default is not None
        }
    )
    contract: list[dict[str, Any]] = []
    for argument in arguments:
        assert isinstance(argument.annotation, ast.Name)
        entry = {
            "name": argument.arg,
            "annotation": argument.annotation.id,
            "has_default": argument.arg in defaults,
        }
        if argument.arg in defaults:
            entry["default"] = ast.literal_eval(defaults[argument.arg])
        contract.append(entry)
    return contract


def _python_artifact(name: str) -> WorkflowSourceArtifact:
    source, uri = _python_source(name)
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return WorkflowSourceArtifact(
        format="python",
        text=source,
        uri=uri,
        content_hash=f"sha256:{digest}",
    )


def _inline_python_artifact(source: str) -> WorkflowSourceArtifact:
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return WorkflowSourceArtifact(
        format="python",
        text=source,
        uri="packages/ptlc_station/ptlc_station/workflows/invalid.py",
        content_hash=f"sha256:{digest}",
    )


def _compiler_action_catalog() -> dict[str, dict[str, Any]]:
    return {
        **scan_decorated_device_package(PTLC_PYTHON_PACKAGE),
        **HOST_ACTION_CATALOG,
    }


def _compile_python(name: str) -> WorkflowRevision:
    artifact = _python_artifact(name)
    return compile_python_script(
        artifact.text,
        action_catalog=_compiler_action_catalog(),
        source_artifact=artifact,
    )


def _compile_yaml(name: str) -> WorkflowRevision:
    return compile_operation_tree(_operation(name), resolver=_resolve_operation)


def _load_ptlc_profile() -> Any:
    return ProfileLoader(
        driver_catalog={"generic_plc_macro": DeclarativePLCMacroDriver}
    ).load(PTLC_PROFILE_PATH)


def _compiler_safety_source(body: str, authoring_path: str) -> str:
    normalized_body = textwrap.dedent(body).strip()
    if authoring_path == "legacy":
        return f"{normalized_body}\n"
    if authoring_path == "function":
        indented_body = textwrap.indent(normalized_body, "    ")
        return (
            '@workflow_definition(workflow_id="safety", revision="draft")\n'
            "def safety():\n"
            f"{indented_body}\n"
        )
    raise AssertionError(f"unknown authoring path: {authoring_path}")


class _RecordingSchedule:
    def __init__(self) -> None:
        self.submitted: list[TaskDag] = []

    def on_job_status(self, callback: Any) -> None:
        del callback

    async def submit_dag(self, dag: TaskDag) -> object:
        self.submitted.append(dag)
        return type("AcceptedRun", (), {"dag": dag})()

    def get_run(self, run_id: str) -> None:
        del run_id
        return None


def _normalized_binding(value: Any, node_indexes: Mapping[str, int]) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, list):
        return [_normalized_binding(item, node_indexes) for item in value]
    if not isinstance(value, Mapping):
        return value
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        if key in {"node_id", "branch_node_id"} and isinstance(item, str):
            normalized[key] = node_indexes[item]
        else:
            normalized[str(key)] = _normalized_binding(item, node_indexes)
    if normalized.get("kind") == "runtime_parameter":
        normalized.pop("default", None)
    return normalized


def _semantic_projection(revision: WorkflowRevision) -> dict[str, Any]:
    node_indexes = {
        invocation.node_id: index
        for index, invocation in enumerate(revision.invocations)
    }
    parameters = [
        {
            "name": parameter.name,
            "type": parameter.type,
            "required": parameter.required,
            **(
                {"default": parameter.default}
                if "default" in parameter.model_fields_set
                else {}
            ),
        }
        for parameter in (revision.parameters or [])
    ]
    invocations = [
        {
            "action_ref": invocation.action_ref,
            "node_type": invocation.node_type,
            "input_bindings": _normalized_binding(
                invocation.input_bindings,
                node_indexes,
            ),
            "control": invocation.control,
            "cleanup_for": [
                node_indexes[node_id] for node_id in invocation.cleanup_for
            ],
        }
        for invocation in revision.invocations
    ]
    edges = sorted(
        (
            node_indexes[edge.source],
            node_indexes[edge.target],
            edge.branch,
        )
        for edge in revision.control_edges
    )
    return {
        "workflow_id": revision.workflow_id,
        "parameters": parameters,
        "invocations": invocations,
        "control_edges": edges,
    }


def _field_names(expression: Any) -> set[str]:
    if isinstance(expression, Mapping):
        names = {
            str(expression["name"])
            for key in expression
            if key == "field" and isinstance(expression.get("name"), str)
        }
        for value in expression.values():
            names.update(_field_names(value))
        return names
    if isinstance(expression, list):
        return {name for item in expression for name in _field_names(item)}
    return set()


def _reachable(start: str, adjacency: Mapping[str, set[str]]) -> set[str]:
    pending = [start]
    visited: set[str] = set()
    while pending:
        node_id = pending.pop()
        if node_id in visited:
            continue
        visited.add(node_id)
        pending.extend(adjacency[node_id] - visited)
    return visited


def test_ptlc_profile_registers_generic_python_workflow_ast_codec() -> None:
    profile_document = _load_yaml(PTLC_PROFILE_PATH)

    assert PYTHON_WORKFLOW_IMPORTER in profile_document["workflow_importers"]
    assert PYTHON_WORKFLOW_IMPORTER in _load_ptlc_profile().workflow_importers


@pytest.mark.parametrize(
    "source",
    [
        '@workflow_definition(workflow_id="broken", revision="draft")\n'
        "def broken(:\n"
        "    pass\n",
        '@workflow_definition(workflow_id="huge_range", revision="draft")\n'
        "def huge_range():\n"
        f"    for _ in range({HUGE_RANGE_LITERAL}):\n"
        "        pump.vacuum_off()\n",
    ],
    ids=["malformed-syntax", "huge-integer-range"],
)
def test_python_profile_normalizes_parser_failures(source: str) -> None:
    profile = _load_ptlc_profile()
    artifact = _inline_python_artifact(source)

    with pytest.raises(ProfileValidationError) as raised:
        profile.import_workflow_source(
            {
                "schema": PYTHON_WORKFLOW_IMPORTER["schema"],
                "kind": PYTHON_WORKFLOW_IMPORTER["kind"],
                "source": source,
            },
            source_artifact=artifact,
        )

    assert str(raised.value).strip()


@pytest.mark.parametrize(
    "source",
    [
        '@workflow_definition(workflow_id="broken", revision="draft")\n'
        "def broken(:\n"
        "    pass\n",
        '@workflow_definition(workflow_id="huge_range", revision="draft")\n'
        "def huge_range():\n"
        f"    for _ in range({HUGE_RANGE_LITERAL}):\n"
        "        pump.vacuum_off()\n",
    ],
    ids=["malformed-syntax", "huge-integer-range"],
)
def test_local_runtime_returns_400_and_zero_dispatch_for_python_parser_failures(
    source: str,
) -> None:
    profile = _load_ptlc_profile()
    artifact = _inline_python_artifact(source)
    schedule = _RecordingSchedule()
    state = LocalApiState(
        schedule,
        profiles={profile.profile_id: profile},
        action_catalog=HOST_ACTION_CATALOG,
    )
    client = TestClient(
        create_app(lambda: state),
        raise_server_exceptions=False,
    )

    response = client.post(
        "/api/runtime/local/runs",
        json={
            "source": {
                "format": "profile_workflow",
                "payload": {
                    "schema": PYTHON_WORKFLOW_IMPORTER["schema"],
                    "kind": PYTHON_WORKFLOW_IMPORTER["kind"],
                    "source": source,
                },
                "artifact": artifact.model_dump(mode="json"),
            },
            "profile_ref": profile.profile_id,
            "parameters": {},
        },
    )

    assert schedule.submitted == []
    assert response.status_code == 400
    assert isinstance(response.json().get("detail"), str)
    assert response.json()["detail"].strip()


@pytest.mark.parametrize("name", ["develop_execute", "develop_prepare"])
def test_profile_and_runtime_import_package_python_with_source_artifact(
    name: str,
) -> None:
    profile = _load_ptlc_profile()
    artifact = _python_artifact(name)
    payload = {
        "schema": PYTHON_WORKFLOW_IMPORTER["schema"],
        "kind": PYTHON_WORKFLOW_IMPORTER["kind"],
        "source": artifact.text,
    }

    revision = profile.import_workflow_source(
        payload,
        source_artifact=artifact,
    )
    assert revision.workflow_id == name
    assert revision.source_artifact == artifact

    schedule = _RecordingSchedule()
    service = RuntimeService(
        schedule,
        profiles={profile.profile_id: profile},
        action_catalog=HOST_ACTION_CATALOG,
    )
    accepted = asyncio.run(
        service.start_run(
            {
                "source": {
                    "format": "profile_workflow",
                    "payload": payload,
                    "artifact": artifact.model_dump(mode="json"),
                },
                "profile_ref": profile.profile_id,
                "parameters": {},
            }
        )
    )

    assert accepted["status"] == "pending"
    assert len(schedule.submitted) == 1
    assert service.get_workflow()["revision"]["sourceArtifact"] == {
        "format": artifact.format,
        "text": artifact.text,
        "uri": artifact.uri,
        "contentHash": artifact.content_hash,
    }


@pytest.mark.parametrize("name", ["develop_execute", "develop_prepare"])
def test_python_and_yaml_compilation_share_execution_content_hash(name: str) -> None:
    assert _compile_python(name).content_hash == _compile_yaml(name).content_hash


def test_parameter_ui_copy_does_not_change_execution_content_hash() -> None:
    revision = _compile_yaml("develop_execute")
    assert revision.parameters
    changed_parameters = [
        parameter.model_copy(
            update={
                "title": f"UI title for {parameter.name}",
                "description": f"UI help for {parameter.name}",
            }
        )
        for parameter in revision.parameters
    ]

    changed_revision = revision.model_copy(update={"parameters": changed_parameters})

    assert changed_revision.content_hash == revision.content_hash


@pytest.mark.parametrize("authoring_path", ["function", "legacy"])
@pytest.mark.parametrize(
    ("body", "error"),
    [
        (
            f"for amount in range({STATIC_RANGE_EXPANSION_LIMIT + 1}):\n"
            "    pump.dose(amount=amount)",
            rf"range.*{STATIC_RANGE_EXPANSION_LIMIT}",
        ),
        (
            'pump.dose(**{"amount": 1})',
            r"\*\*kwargs|keyword unpacking",
        ),
        (
            "for amount in [1]:\n    pump.dose(amount=amount)\nelse:\n    pump.off()",
            r"for.*else",
        ),
    ],
    ids=["over-limit-range", "kwargs-unpacking", "for-else"],
)
def test_both_python_authoring_paths_reject_unsafe_static_expansion(
    authoring_path: str,
    body: str,
    error: str,
) -> None:
    source = _compiler_safety_source(body, authoring_path)

    with pytest.raises(ValueError, match=error):
        compile_python_script(
            source,
            action_catalog=COMPILER_SAFETY_ACTION_CATALOG,
        )


def test_current_python_slice_rejects_control_flow_inside_finally() -> None:
    source = _compiler_safety_source(
        """
        try:
            pump.dose(amount=1)
        finally:
            if True:
                pump.off()
        """,
        "function",
    )

    with pytest.raises(ValueError, match=r"finally.*control flow"):
        compile_python_script(
            source,
            action_catalog=COMPILER_SAFETY_ACTION_CATALOG,
        )


@pytest.mark.parametrize("name", ["develop_execute", "develop_prepare"])
def test_migrated_workflow_is_package_python_with_independent_yaml_provenance(
    name: str,
) -> None:
    artifact = _python_artifact(name)
    tree = ast.parse(artifact.text, filename=artifact.uri)
    _workflow_function(tree, name)
    provenance = _load_yaml(PROVENANCE_PATH)
    provenance_entry = next(
        entry
        for entry in provenance["files"]
        if entry["fixture_path"] == WORKFLOW_FIXTURES[name]
    )

    assert artifact.format == "python"
    assert artifact.uri == WORKFLOW_SOURCE_URIS[name]
    assert artifact.uri.startswith("packages/")
    assert "/workflows/" in artifact.uri
    assert artifact.uri.endswith(".py")
    assert "schema: ptlc.script/v1" not in artifact.text
    assert provenance["source_repository"] == "pTLC_platformUI"
    assert provenance["source_ref"] == "origin/codex/ui-upper-next"
    assert provenance_entry["source_path"].endswith(f"/{name}.yaml")
    assert provenance_entry["source_path"] != artifact.uri
    assert provenance_entry["git_blob"] != artifact.content_hash


@pytest.mark.parametrize("name", ["develop_execute", "develop_prepare"])
def test_python_workflow_calls_exact_real_yaml_actions_from_decorator_registry(
    name: str,
) -> None:
    source, uri = _python_source(name)
    tree = ast.parse(source, filename=uri)
    call_refs = _python_call_refs(tree)
    decorated_actions = set(scan_decorated_device_package(PTLC_PYTHON_PACKAGE))
    expected_calls = Counter(_yaml_call_refs(_operation(name).get("body")))

    assert Counter(call_refs) == expected_calls
    assert all(
        action_ref in decorated_actions or action_ref == "host_node.manual_confirm"
        for action_ref in call_refs
    )
    assert "os_control.human_confirm" not in call_refs
    assert (
        call_refs.count("host_node.manual_confirm")
        == expected_calls["host_node.manual_confirm"]
    )


@pytest.mark.parametrize("name", ["develop_execute", "develop_prepare"])
def test_python_workflow_parameters_match_real_yaml_inputs(name: str) -> None:
    source, uri = _python_source(name)
    function = _workflow_function(ast.parse(source, filename=uri), name)

    assert _function_parameter_contract(function) == _external_parameter_contract(
        _operation(name)
    )


def test_develop_prepare_compiles_exact_actions_and_try_finally_cleanup() -> None:
    source, uri = _python_source("develop_prepare")
    tree = ast.parse(source, filename=uri)
    try_nodes = [node for node in ast.walk(tree) if isinstance(node, ast.Try)]
    assert len(try_nodes) == 2
    for try_node in try_nodes:
        assert not try_node.handlers
        assert not try_node.orelse
        assert [_call_ref(node.value) for node in try_node.finalbody] == [
            "pump.vacuum_off"
        ]

    python_revision = _compile_python("develop_prepare")
    yaml_revision = _compile_yaml("develop_prepare")
    assert _semantic_projection(python_revision) == _semantic_projection(yaml_revision)
    assert python_revision.source_artifact is not None
    assert python_revision.source_artifact.format == "python"

    physical_nodes = [
        node
        for node in python_revision.invocations
        if node.action_ref not in {"os_control.branch", "os_control.join"}
    ]
    cleanup_nodes = [node for node in physical_nodes if node.node_type == "cleanup"]
    assert len(physical_nodes) == 10
    assert len(cleanup_nodes) == 2
    assert [node.action_ref for node in cleanup_nodes] == [
        "pump.vacuum_off",
        "pump.vacuum_off",
    ]
    protected_action_sets = [
        {
            next(
                invocation.action_ref
                for invocation in python_revision.invocations
                if invocation.node_id == protected_id
            )
            for protected_id in cleanup.cleanup_for
        }
        for cleanup in cleanup_nodes
    ]
    assert protected_action_sets == [
        {"pump.vacuum_on", "develop.clean_line"},
        {"pump.vacuum_on", "develop.rinse_suction"},
    ]


def test_develop_execute_compiles_dynamic_result_branches_hitl_and_joins() -> None:
    source, uri = _python_source("develop_execute")
    tree = ast.parse(source, filename=uri)
    function = _workflow_function(tree, "develop_execute")
    if_nodes = [node for node in ast.walk(function) if isinstance(node, ast.If)]
    assert len(if_nodes) == 4
    condition_texts = [ast.unparse(node.test) for node in if_nodes]
    assert "auto_drain" in condition_texts
    assert any("ref_result.ok" in condition for condition in condition_texts)
    assert sum("wl_result.status" in condition for condition in condition_texts) == 2

    python_revision = _compile_python("develop_execute")
    yaml_revision = _compile_yaml("develop_execute")
    assert _semantic_projection(python_revision) == _semantic_projection(yaml_revision)
    assert python_revision.source_artifact is not None
    assert python_revision.source_artifact.format == "python"

    branch_nodes = [
        node for node in python_revision.invocations if node.node_type == "branch"
    ]
    join_nodes = [
        node for node in python_revision.invocations if node.node_type == "join"
    ]
    manual_confirm_nodes = [
        node
        for node in python_revision.invocations
        if node.action_ref == "host_node.manual_confirm"
    ]
    assert len(branch_nodes) == 4
    assert len(join_nodes) == 4
    assert len(manual_confirm_nodes) == 3
    assert all(node.node_type == "manual_confirm" for node in manual_confirm_nodes)

    conditions = [node.input_bindings["condition"] for node in branch_nodes]
    assert all(not isinstance(condition, LiteralValue) for condition in conditions)
    assert any(
        isinstance(condition, RuntimeParameterRef)
        and condition.parameter == "auto_drain"
        for condition in conditions
    )
    expressions = [
        condition
        for condition in conditions
        if isinstance(condition, ExpressionBinding)
    ]
    assert {
        name for condition in expressions for name in _field_names(condition.expression)
    } >= {
        "ok",
        "status",
    }
    dependency_ids = {
        node_id
        for condition in conditions
        for node_id in binding_node_dependencies(condition)
    }
    dependency_actions = {
        node.action_ref
        for node in python_revision.invocations
        if node.node_id in dependency_ids
    }
    assert {"develop.capture_reference", "develop.wait_level"} <= dependency_actions

    adjacency = {node.node_id: set() for node in python_revision.invocations}
    outgoing_branches: dict[str, set[str | None]] = {
        node.node_id: set() for node in branch_nodes
    }
    for edge in python_revision.control_edges:
        adjacency[edge.source].add(edge.target)
        if edge.source in outgoing_branches:
            outgoing_branches[edge.source].add(edge.branch)
    join_ids = {node.node_id for node in join_nodes}
    for branch in branch_nodes:
        branch_targets = [
            edge.target
            for edge in python_revision.control_edges
            if edge.source == branch.node_id and edge.branch in {"true", "false"}
        ]
        assert outgoing_branches[branch.node_id] == {"true", "false"}
        assert len(branch_targets) == 2
        reachable_from_both = set.intersection(
            *(_reachable(target, adjacency) for target in branch_targets)
        )
        assert reachable_from_both & join_ids, (
            f"dynamic branch {branch.node_id} must reconverge through a join"
        )
