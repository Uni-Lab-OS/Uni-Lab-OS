"""Compile the pTLC repository's checked-in recipes through the generic Profile."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from unilabos.devices.generic_plc_macro import DeclarativePLCMacroDriver
from unilabos.app.local_bridge.offline_os import OfflineOS
from unilabos.app.local_bridge.schedule_ws import ScheduleSession
from unilabos.runtime.event_store import SQLiteEventJournal
from unilabos.runtime.profile_loader import ProfileLoader
from unilabos.runtime.service import RuntimeService
from unilabos.scheduler.resource_lock import ResourceLockManager
from unilabos.workflow.dag_compile import compile_workflow_revision


WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
PROFILE_PATH = (
    WORKSPACE_ROOT / "Uni-Lab-Templates/packages/ptlc_station/package.yaml"
)
RECIPE_ROOT = WORKSPACE_ROOT / "pTLC_platformUI/UI-Upper/recipes"


@pytest.fixture(scope="module")
def ptlc_profile():
    return ProfileLoader(
        driver_catalog={"generic_plc_macro": DeclarativePLCMacroDriver}
    ).load(PROFILE_PATH)


@pytest.mark.parametrize(
    ("recipe_name", "expected_actions"),
    [
        ("spotting.yaml", ["ptlc_station.spotting"]),
        (
            "spottingandscarp.yaml",
            [
                "ptlc_station.spotting",
                "ptlc_station.before_photo",
                "ptlc_station.scrape",
            ],
        ),
    ],
)
def test_checked_in_ptlc_recipe_imports_and_compiles_as_generic_task_dag(
    ptlc_profile,
    recipe_name: str,
    expected_actions: list[str],
) -> None:
    recipe_path = RECIPE_ROOT / recipe_name
    payload = yaml.safe_load(recipe_path.read_text(encoding="utf-8-sig"))

    revision = ptlc_profile.import_legacy_source(
        payload,
        parameters={"sample_id": "acceptance-sample-001"},
    )
    dag = compile_workflow_revision(
        revision,
        task_id=recipe_path.stem,
        action_catalog=ptlc_profile.action_catalog,
        runtime_parameters={"sample_id": "acceptance-sample-001"},
    )

    assert [node.action_ref for node in revision.invocations] == expected_actions
    assert [
        f"{dag.nodes[node_id].device_id}.{dag.nodes[node_id].action}"
        for node_id in dag.nodes
    ] == expected_actions
    assert len(dag.workflow_revision_hash) == 64
    assert all(
        "sample_id" not in node.input_bindings for node in dag.nodes.values()
    )


@pytest.mark.parametrize("recipe_name", ["spotting.yaml", "spottingandscarp.yaml"])
def test_checked_in_ptlc_recipe_runs_to_durable_terminal_through_runtime_api(
    tmp_path: Path,
    ptlc_profile,
    recipe_name: str,
) -> None:
    recipe_path = RECIPE_ROOT / recipe_name
    payload = yaml.safe_load(recipe_path.read_text(encoding="utf-8-sig"))
    journal = SQLiteEventJournal(
        tmp_path / f"{recipe_path.stem}.sqlite",
        runtime_epoch=f"acceptance-{recipe_path.stem}",
    )
    locks = ResourceLockManager(runtime_epoch=journal.runtime_epoch)
    offline_os = OfflineOS(resource_lock_manager=locks, journal=journal)
    schedule = ScheduleSession(offline_os.receive, session_id="ptlc-acceptance")
    offline_os.bind(schedule)
    service = RuntimeService(
        schedule,
        journal=journal,
        profiles={ptlc_profile.profile_id: ptlc_profile},
    )

    async def scenario() -> tuple[str, dict[str, str] | None]:
        accepted = await service.start_run(
            {
                "source": {"format": "legacy_recipe", "payload": payload},
                "profile_ref": ptlc_profile.profile_id,
                "parameters": {"sample_id": "acceptance-sample-001"},
            }
        )
        run_id = accepted["id"]
        handle = schedule.get_run(run_id)
        assert handle is not None
        await handle.wait()
        projected = service.get_run(run_id)
        for _ in range(10):
            if projected is not None and projected["status"] == "completed":
                break
            await asyncio.sleep(0)
            projected = service.get_run(run_id)
        return run_id, projected

    try:
        run_id, projected = asyncio.run(scenario())
        assert projected == {"id": run_id, "status": "completed"}
        event_types = [event.type for event in journal.list_events(run_id)]
        assert event_types[0] == "run_submitted"
        assert event_types[-1] == "run_completed"
        assert event_types.count("node_succeeded") == len(
            [stage for stage in payload["stages"] if stage.get("enabled", True)]
        )
    finally:
        journal.close()
