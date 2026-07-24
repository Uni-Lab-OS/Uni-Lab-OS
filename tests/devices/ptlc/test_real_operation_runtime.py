"""RED acceptance tests for importing real Operation trees into the OS DAG.

The workflow inputs are the byte-identical pTLC git snapshots locked by
``test_real_operation_files.py``.  This file deliberately specifies a generic
Profile codec boundary; it must not add a pTLC-specific Runtime endpoint.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import tomllib
from typing import Any, Callable

import pytest
import yaml

from unilabos.devices.generic_plc_macro import DeclarativePLCMacroDriver
from unilabos.registry.registry import Registry
from unilabos.runtime.profile_loader import LoadedProfile, ProfileLoader
from unilabos.runtime.service import RuntimeService
from unilabos.scheduler.dag_executor import DagExecutor
from unilabos.scheduler.dag_model import NodeState, TaskDag
from unilabos.scheduler.result_store import NodeExecutionResult, ResultEnvelope
from unilabos.workflow.canonical import WorkflowRevision
from unilabos.workflow.dag_compile import compile_workflow_revision


WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "real_operations"
PROFILE_PATH = WORKSPACE_ROOT / "Uni-Lab-Templates/packages/ptlc_station/package.yaml"
PTLC_PACKAGE_ROOT = PROFILE_PATH.parent
PTLC_PYTHON_PACKAGE = PTLC_PACKAGE_ROOT / "ptlc_station"

_REAL_OPERATION_ACTION_CONTRACTS: dict[
    str,
    tuple[dict[str, tuple[str, bool, object | None]], dict[str, str]],
] = {
    "develop.capture_reference": (
        {
            "target_tank": ("integer", True, None),
            "timeout_s": ("number", False, 90.0),
        },
        {"ok": "boolean", "has_ref": "boolean", "elapsed_s": "number"},
    ),
    "develop.wait_level": (
        {
            "target_tank": ("integer", True, None),
            "stage": ("string", True, None),
            "staleness_s": ("number", False, 30.0),
            "hard_cap_s": ("number", False, 3600.0),
            "confirm_n": ("integer", False, 2),
        },
        {
            "status": "string",
            "front_percent": "number",
            "threshold": "number",
            "elapsed_s": "number",
        },
    ),
    "develop.drain": (
        {
            "target_tank": ("integer", True, None),
            "drain_duration_s": ("number", False, 5.0),
            "drain_cap_s": ("number", False, 120.0),
            "blow_s": ("number", False, 30.0),
            "dry_duration_s": ("number", False, 0.0),
        },
        {},
    ),
    "develop.init": ({"target_tank": ("integer", True, None)}, {}),
    "develop.plate_retract": (
        {"target_tank": ("integer", True, None)},
        {},
    ),
    "develop.rinse_suction": (
        {"target_tank": ("integer", True, None)},
        {},
    ),
    "develop.clean_line": (
        {
            "target_tank": ("integer", True, None),
            "solvent_volume_ml": ("number", False, 2.0),
        },
        {},
    ),
    "develop.rinse_fill": (
        {
            "target_tank": ("integer", True, None),
            "solvent_volume_ml": ("number", False, 2.0),
        },
        {},
    ),
    "develop.fill": ({"target_tank": ("integer", True, None)}, {}),
    "pump.vacuum_on": ({}, {}),
    "pump.vacuum_off": ({}, {}),
    "robot.require_anchor": (
        {
            "point_id": ("string", True, None),
            "joint_tol_deg": ("number", False, 2.0),
            "pos_tol_mm": ("number", False, 5.0),
            "rot_tol_deg": ("number", False, 5.0),
        },
        {},
    ),
    "rail.move": (
        {"Rail_Target_Position": ("integer", True, None)},
        {},
    ),
}


def _load(relative_path: str) -> dict[str, Any]:
    document = yaml.safe_load(
        (FIXTURE_ROOT / relative_path).read_text(encoding="utf-8")
    )
    assert isinstance(document, dict)
    return document


def _source_resolver(name: str) -> dict[str, Any]:
    matches = list(FIXTURE_ROOT.glob(f"**/{name}.yaml"))
    if len(matches) != 1:
        raise KeyError(f"operation {name!r} resolved to {len(matches)} files")
    document = yaml.safe_load(matches[0].read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def test_ptlc_station_is_an_independently_installable_python_package() -> None:
    pyproject_path = PTLC_PACKAGE_ROOT / "pyproject.toml"
    assert pyproject_path.is_file(), (
        "the migrated pTLC station must be its own installable Python package"
    )
    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    project = pyproject.get("project", {})
    build_system = pyproject.get("build-system", {})

    assert str(project.get("name", "")).replace("-", "_") == "ptlc_station"
    assert project.get("version")
    assert build_system.get("build-backend")
    assert build_system.get("requires")
    assert (PTLC_PYTHON_PACKAGE / "__init__.py").is_file()


def test_ptlc_station_profile_does_not_embed_a_workflow_action_catalog() -> None:
    profile_document = yaml.safe_load(PROFILE_PATH.read_text(encoding="utf-8"))
    assert isinstance(profile_document, dict)
    assert "workflow_action_catalog" not in profile_document, (
        "physical actions belong to decorated device classes, not package.yaml"
    )


def _scan_ptlc_device_package(monkeypatch: pytest.MonkeyPatch) -> Registry:
    registry = Registry()
    monkeypatch.setattr(registry, "_load_config_cache", lambda: {})
    monkeypatch.setattr(registry, "_save_config_cache", lambda _cache: None)
    monkeypatch.syspath_prepend(str(PTLC_PACKAGE_ROOT))
    registry._run_ast_scan(
        devices_dirs=[PTLC_PYTHON_PACKAGE],
        external_only=True,
    )
    return registry


def test_ptlc_station_decorated_devices_cover_every_real_operation_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _scan_ptlc_device_package(monkeypatch)

    assert {"develop", "pump", "robot", "rail"} <= set(
        registry.device_type_registry
    ), "each physical action namespace must be an AST-scannable @device class"

    discovered_actions: dict[str, dict[str, Any]] = {}
    for device_id, device_entry in registry.device_type_registry.items():
        actions = device_entry.get("class", {}).get("action_value_mappings", {})
        for action_name, action_entry in actions.items():
            discovered_actions[f"{device_id}.{action_name}"] = action_entry

    assert set(_REAL_OPERATION_ACTION_CONTRACTS) <= set(discovered_actions)
    for action_ref, (expected_inputs, expected_outputs) in (
        _REAL_OPERATION_ACTION_CONTRACTS.items()
    ):
        action_schema = discovered_actions[action_ref]["schema"]
        goal_schema = action_schema["properties"]["goal"]
        result_schema = action_schema["properties"].get("result", {})
        input_properties = goal_schema.get("properties", {})
        output_properties = result_schema.get("properties", {})
        required_inputs = set(goal_schema.get("required", []))

        # 真实 Operation 只固定其实际使用的兼容参数子集；设备包还必须保留
        # 原 Action YAML 中的可选调试/泵档参数。新增参数不得变成必填，
        # 否则旧流程会在 Preflight 阶段发生破坏性变化。
        assert set(expected_inputs) <= set(input_properties), action_ref
        extra_inputs = set(input_properties) - set(expected_inputs)
        assert not (extra_inputs & required_inputs), action_ref
        assert set(output_properties) == set(expected_outputs), action_ref
        for input_name, (input_type, is_required, default) in (
            expected_inputs.items()
        ):
            assert input_properties[input_name]["type"] == input_type, action_ref
            assert (input_name in required_inputs) is is_required, action_ref
            if not is_required:
                assert input_properties[input_name].get("default") == default, (
                    action_ref
                )
        for output_name, output_type in expected_outputs.items():
            assert output_properties[output_name]["type"] == output_type, action_ref


@pytest.fixture(scope="module")
def profile() -> LoadedProfile:
    return ProfileLoader(
        driver_catalog={"generic_plc_macro": DeclarativePLCMacroDriver}
    ).load(PROFILE_PATH)


def _import_real_operation(
    profile: LoadedProfile,
    relative_path: str,
    *,
    parameters: dict[str, Any],
) -> WorkflowRevision:
    importer = getattr(profile, "import_workflow_source", None)
    if importer is None:
        pytest.fail(
            "generic Profile workflow codec is missing: "
            "LoadedProfile.import_workflow_source",
            pytrace=False,
        )
    revision = importer(
        _load(relative_path),
        parameters=parameters,
        resolver=_source_resolver,
    )
    assert isinstance(revision, WorkflowRevision)
    return revision


def _compile(
    profile: LoadedProfile,
    relative_path: str,
    *,
    parameters: dict[str, Any],
) -> tuple[WorkflowRevision, TaskDag]:
    revision = _import_real_operation(
        profile,
        relative_path,
        parameters=parameters,
    )
    dag = compile_workflow_revision(
        revision,
        task_id=f"real-{revision.workflow_id}",
        action_catalog=profile.action_catalog,
        runtime_parameters=parameters,
    )
    return revision, dag


def test_real_execute_imports_via_generic_profile_codec_and_expands_run_script(
    profile: LoadedProfile,
) -> None:
    revision, dag = _compile(
        profile,
        "02_develop/develop_execute.yaml",
        parameters={"tank": 2, "auto_drain": True, "dry_duration_s": 0.0},
    )

    assert revision.workflow_id == "develop_execute"
    assert len(revision.content_hash) == 64
    assert any(node.node_type == "branch" for node in revision.invocations)
    manual_confirm_nodes = [
        node
        for node in revision.invocations
        if node.action_ref == "host_node.manual_confirm"
    ]
    assert len(manual_confirm_nodes) == 3
    assert all(
        node.node_type == "manual_confirm" for node in manual_confirm_nodes
    )
    assert all(
        node.action_ref != "os_control.human_confirm"
        for node in revision.invocations
    )
    compiled_actions = {
        f"{node.device_id}.{node.action}" for node in dag.nodes.values()
    }
    assert {
        "develop.capture_reference",
        "develop.wait_level",
        "develop.drain",
        "robot.require_anchor",
        "rail.move",
    } <= compiled_actions
    assert all(
        node.action_ref not in {"develop_standby", "rail_move_safe"}
        for node in revision.invocations
    ), "run_script must expand through the same Canonical DAG, not become another VM"
    assert any(
        entry.compiled_node_ids for entry in revision.source_map.entries
    ), "the visual/debugger source map must survive Operation lowering"


class _RecordingSchedule:
    def __init__(self) -> None:
        self.submitted: list[TaskDag] = []
        self._callbacks: list[Callable[[dict[str, Any]], None]] = []

    def on_job_status(self, callback: Callable[[dict[str, Any]], None]) -> None:
        self._callbacks.append(callback)

    async def submit_dag(self, dag: TaskDag) -> object:
        self.submitted.append(dag)
        return type("AcceptedRun", (), {"dag": dag})()

    def get_run(self, run_id: str) -> None:
        del run_id
        return None


def test_real_execute_enters_the_one_runtime_service_as_profile_workflow(
    profile: LoadedProfile,
) -> None:
    schedule = _RecordingSchedule()
    service = RuntimeService(schedule, profiles={profile.profile_id: profile})
    payload = _load("02_develop/develop_execute.yaml")

    accepted = asyncio.run(
        service.start_run(
            {
                "source": {
                    "format": "profile_workflow",
                    "payload": payload,
                    "dependencies": [
                        _load("02_develop/develop_standby.yaml"),
                        _load("08_rail/rail_move_safe.yaml"),
                    ],
                },
                "profile_ref": profile.profile_id,
                "parameters": {
                    "tank": 2,
                    "auto_drain": False,
                    "dry_duration_s": 0.0,
                },
            }
        )
    )

    assert accepted["status"] == "pending"
    assert len(schedule.submitted) == 1
    assert schedule.submitted[0].workflow_revision_hash
    assert {
        f"{node.device_id}.{node.action}"
        for node in schedule.submitted[0].nodes.values()
    } >= {"develop.capture_reference", "develop.drain"}


def test_real_prepare_failure_still_executes_both_finally_vacuum_off_nodes(
    profile: LoadedProfile,
) -> None:
    _, dag = _compile(
        profile,
        "02_develop/develop_prepare.yaml",
        parameters={"tank": 1},
    )
    dispatched: list[str] = []
    cleanup_nodes = [
        node
        for node in dag.nodes.values()
        if f"{node.device_id}.{node.action}" == "pump.vacuum_off"
    ]
    assert len(cleanup_nodes) == 2
    assert all(node.node_type == "cleanup" for node in cleanup_nodes)

    async def dispatch(node: Any) -> NodeExecutionResult:
        action_ref = f"{node.device_id}.{node.action}"
        dispatched.append(action_ref)
        if action_ref == "develop.rinse_suction":
            return NodeExecutionResult(
                state=NodeState.FAILED,
                terminal_info={"error": "injected rinse suction failure"},
            )
        return NodeExecutionResult(
            state=NodeState.SUCCESS,
            envelope=ResultEnvelope(outputs={}),
        )

    states = asyncio.run(DagExecutor(dag, dispatch).run())

    assert NodeState.FAILED in states.values()
    assert dispatched.count("pump.vacuum_off") == 2
    assert dispatched.index("pump.vacuum_off") < dispatched.index(
        "develop.rinse_fill"
    )
    assert dispatched[-1] == "pump.vacuum_off", (
        "the second finally cleanup must finish before the run becomes failed"
    )


@pytest.mark.parametrize("auto_drain", [False, True])
def test_real_execute_branches_are_evaluated_inside_os_not_device_dispatch(
    profile: LoadedProfile,
    auto_drain: bool,
) -> None:
    _, dag = _compile(
        profile,
        "02_develop/develop_execute.yaml",
        parameters={
            "tank": 2,
            "auto_drain": auto_drain,
            "dry_duration_s": 0.0,
        },
    )
    dispatched: list[str] = []

    async def dispatch(node: Any) -> NodeExecutionResult:
        action_ref = f"{node.device_id}.{node.action}"
        dispatched.append(action_ref)
        if action_ref == "develop.capture_reference":
            return NodeExecutionResult(
                state=NodeState.SUCCESS,
                envelope=ResultEnvelope(
                    outputs={"ok": True, "has_ref": True, "elapsed_s": 1.0}
                ),
            )
        if action_ref == "develop.wait_level":
            return NodeExecutionResult(
                state=NodeState.SUCCESS,
                envelope=ResultEnvelope(
                    outputs={
                        "status": "reached",
                        "front_percent": 80.0,
                        "threshold": 75.0,
                        "elapsed_s": 1.0,
                    }
                ),
            )
        return NodeExecutionResult(
            state=NodeState.SUCCESS,
            envelope=ResultEnvelope(outputs={}),
        )

    states = asyncio.run(DagExecutor(dag, dispatch).run())

    assert NodeState.FAILED not in states.values()
    assert NodeState.CANCELLED not in states.values()
    assert "os_control.branch" not in dispatched
    assert "os_control.join" not in dispatched
    assert dispatched.count("develop.capture_reference") == 1
    assert dispatched.count("develop.drain") == 1
    if auto_drain:
        assert dispatched.count("develop.wait_level") == 2
        assert "robot.require_anchor" in dispatched
        assert "rail.move" in dispatched
        assert "host_node.manual_confirm" not in dispatched
    else:
        assert "develop.wait_level" not in dispatched
        assert "robot.require_anchor" not in dispatched
        assert "rail.move" not in dispatched
        assert dispatched.count("host_node.manual_confirm") == 1
    assert "os_control.human_confirm" not in dispatched


def test_real_execute_degraded_t1_keeps_value_across_join_and_skips_t2(
    profile: LoadedProfile,
) -> None:
    _, dag = _compile(
        profile,
        "02_develop/develop_execute.yaml",
        parameters={"tank": 2, "auto_drain": True, "dry_duration_s": 0.0},
    )
    dispatched: list[str] = []

    async def dispatch(node: Any) -> NodeExecutionResult:
        action_ref = f"{node.device_id}.{node.action}"
        dispatched.append(action_ref)
        if action_ref == "develop.capture_reference":
            return NodeExecutionResult(
                state=NodeState.SUCCESS,
                envelope=ResultEnvelope(
                    outputs={"ok": True, "has_ref": True, "elapsed_s": 1.0}
                ),
            )
        if action_ref == "develop.wait_level":
            return NodeExecutionResult(
                state=NodeState.SUCCESS,
                envelope=ResultEnvelope(
                    outputs={
                        "status": "degraded",
                        "front_percent": 0.0,
                        "threshold": 75.0,
                        "elapsed_s": 1.0,
                    }
                ),
            )
        return NodeExecutionResult(
            state=NodeState.SUCCESS,
            envelope=ResultEnvelope(outputs={}),
        )

    states = asyncio.run(DagExecutor(dag, dispatch).run())

    assert NodeState.FAILED not in states.values()
    assert NodeState.CANCELLED not in states.values()
    assert "os_control.branch" not in dispatched
    assert "os_control.join" not in dispatched
    assert dispatched.count("develop.wait_level") == 1, (
        "T1 degraded must skip the reached-only T2 wait"
    )
    assert "robot.require_anchor" not in dispatched
    assert "rail.move" not in dispatched
    assert dispatched.count("host_node.manual_confirm") == 1
    assert "os_control.human_confirm" not in dispatched
    assert dispatched.count("develop.drain") == 1
