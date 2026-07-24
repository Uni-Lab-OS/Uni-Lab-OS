"""Full pTLC legacy recipe through the generic local Runtime boundary."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from unilabos.app.local_bridge.local_api import create_app
from unilabos.app.local_bridge.offline_os import OfflineOS
from unilabos.app.local_bridge.schedule_ws import ScheduleSession
from unilabos.runtime.event_store import RunEvent, SQLiteEventJournal
from unilabos.runtime.profile_loader import ProfileLoader
from unilabos.runtime.service import RuntimeService
from unilabos.scheduler.resource_lock import ResourceLockManager


FIXTURE = Path(__file__).parent / "fixtures" / "multi_band_legacy_recipe.json"
PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROFILE_PATH = (
    PROJECT_ROOT.parent
    / "Uni-Lab-Templates"
    / "packages"
    / "ptlc_station"
    / "package.yaml"
)
RUN_TERMINALS = {"run_completed", "run_failed", "run_cancelled"}


def _profile():
    return ProfileLoader(
        driver_catalog={"generic_plc_macro": object()}
    ).load(PROFILE_PATH)


def _one_event(
    events: list[RunEvent],
    *,
    event_type: str,
    node_id: str | None = None,
    released_resource_id: str | None = None,
) -> RunEvent:
    matches = [
        event
        for event in events
        if event.type == event_type
        and (node_id is None or event.node_id == node_id)
        and (
            released_resource_id is None
            or event.payload.get("released_resource_id")
            == released_resource_id
        )
    ]
    assert len(matches) == 1, (
        f"expected one {event_type} event for node={node_id!r}, "
        f"resource={released_resource_id!r}; got {matches!r}"
    )
    return matches[0]


async def _await_completed(
    service: RuntimeService,
    journal: SQLiteEventJournal,
    run_id: str,
) -> None:
    for _ in range(2_000):
        status = service.get_run(run_id)
        terminals = [
            event
            for event in journal.list_events(run_id)
            if event.type in RUN_TERMINALS
        ]
        if status == {"id": run_id, "status": "completed"} and terminals:
            return
        await asyncio.sleep(0)
    raise AssertionError(
        "full legacy recipe did not complete; repeated scrape nodes must reuse "
        "the first-scrape workflow_block hold instead of self-deadlocking"
    )


def test_local_runtime_exposes_no_ptlc_specific_api_route() -> None:
    app = create_app(lambda: None)
    paths = {
        route.path
        for route in app.routes
        if isinstance(getattr(route, "path", None), str)
    }

    assert "/api/runtime/local/runs" in paths
    assert not any("/ptlc" in path.lower() for path in paths)


def test_complete_multiband_recipe_runs_and_replays_via_generic_runtime(
    tmp_path: Path,
) -> None:
    recipe = json.loads(FIXTURE.read_text(encoding="utf-8"))
    db_path = tmp_path / "ptlc-full-runtime.sqlite"
    journal = SQLiteEventJournal(db_path, runtime_epoch="ptlc-e2e-os")
    locks = ResourceLockManager(runtime_epoch="ptlc-e2e-os")
    offline = OfflineOS(resource_lock_manager=locks, journal=journal)
    schedule = ScheduleSession(offline.receive, session_id="generic-offline-os")
    offline.bind(schedule)
    service = RuntimeService(
        schedule,
        journal=journal,
        profiles={"ptlc_station": _profile()},
    )

    async def scenario() -> str:
        accepted = await service.start_run(
            {
                "profile_ref": "ptlc_station",
                "source": {
                    "format": "legacy_recipe",
                    "payload": {
                        "name": recipe["name"],
                        "stages": recipe["stages"],
                    },
                },
                "parameters": {"sample_id": recipe["sample_id"]},
            }
        )
        run_id = accepted["id"]
        try:
            await _await_completed(service, journal, run_id)
        finally:
            pending = tuple(offline._tasks.values())  # noqa: SLF001 - test cleanup
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        return run_id

    run_id = asyncio.run(scenario())

    assert [message["action"] for message in offline.received] == ["task_dag"]
    assert all("ptlc" not in message["action"].lower() for message in offline.received)
    dag_payload = offline.received[0]["data"]
    expected_actions = [stage["name"] for stage in recipe["stages"]]
    assert [node["action"] for node in dag_payload["nodes"]] == expected_actions
    assert all(node["device_id"] == "ptlc_station" for node in dag_payload["nodes"])

    nodes_by_action: dict[str, list[dict[str, Any]]] = {}
    for node in dag_payload["nodes"]:
        nodes_by_action.setdefault(node["action"], []).append(node)
    develop_id = nodes_by_action["develop"][0]["node_id"]
    scrape_nodes = nodes_by_action["scrape"]
    assert len(scrape_nodes) == 3
    first_scrape_id = scrape_nodes[0]["node_id"]
    last_scrape_id = scrape_nodes[-1]["node_id"]
    assert first_scrape_id != last_scrape_id
    assert first_scrape_id == "scrape-4"
    assert last_scrape_id == "scrape-8"

    assert first_scrape_id in {
        edge["target_node_uuid"] for edge in dag_payload["edges"]
    }
    assert nodes_by_action["scrape"][0]["resource_releases"] == [
        {
            "hold_id": "develop-to-first-scrape",
            "acquire_node_id": develop_id,
            "resource_ref": "develop-tank-pool",
            "scope": "until_handoff",
        }
    ]
    assert nodes_by_action["scrape"][-1]["resource_releases"] == [
        {
            "hold_id": "first-to-last-scrape",
            "acquire_node_id": first_scrape_id,
            "resource_ref": "photo-scrape-station",
            "scope": "workflow_block",
        }
    ]

    events = journal.list_events(run_id)
    develop_acquired = _one_event(
        events,
        event_type="lock_acquired",
        node_id=develop_id,
    )
    assert {
        (claim["resource_id"], claim["scope"])
        for claim in develop_acquired.payload["claims"]
    } >= {("develop-tank-pool", "until_handoff")}
    develop_succeeded = _one_event(
        events,
        event_type="node_succeeded",
        node_id=develop_id,
    )
    first_scrape_succeeded = _one_event(
        events,
        event_type="node_succeeded",
        node_id=first_scrape_id,
    )
    develop_released = _one_event(
        events,
        event_type="lock_released",
        node_id=develop_id,
        released_resource_id="develop-tank-pool",
    )
    assert develop_succeeded.sequence < first_scrape_succeeded.sequence
    assert first_scrape_succeeded.sequence < develop_released.sequence
    assert develop_released.payload["released_scope"] == "until_handoff"

    scrape_acquired = _one_event(
        events,
        event_type="lock_acquired",
        node_id=first_scrape_id,
    )
    assert {
        (claim["resource_id"], claim["scope"])
        for claim in scrape_acquired.payload["claims"]
    } >= {("photo-scrape-station", "workflow_block")}
    last_scrape_succeeded = _one_event(
        events,
        event_type="node_succeeded",
        node_id=last_scrape_id,
    )
    scrape_released = _one_event(
        events,
        event_type="lock_released",
        node_id=first_scrape_id,
        released_resource_id="photo-scrape-station",
    )
    assert first_scrape_succeeded.sequence < last_scrape_succeeded.sequence
    assert last_scrape_succeeded.sequence < scrape_released.sequence
    assert scrape_released.payload["released_scope"] == "workflow_block"

    terminal_events = [event for event in events if event.type in RUN_TERMINALS]
    assert [event.type for event in terminal_events] == ["run_completed"]
    assert service.get_run(run_id) == {"id": run_id, "status": "completed"}
    assert locks.active_leases() == ()

    journal.close()
    replayed = SQLiteEventJournal(db_path, runtime_epoch="ptlc-e2e-replay")
    try:
        submission = replayed.load_run_submission(run_id)
        assert submission is not None
        assert submission.profile_ref == "ptlc_station"
        assert submission.status == "completed"
        assert submission.compiled_dag == dag_payload
        assert [
            (event.sequence, event.node_id, event.type, event.payload)
            for event in replayed.list_events(run_id)
        ] == [
            (event.sequence, event.node_id, event.type, event.payload)
            for event in events
        ]
        assert replayed.load_cursor(run_id).completed == [
            node["node_id"] for node in dag_payload["nodes"]
        ]
        assert replayed.list_incomplete_run_ids() == []
    finally:
        replayed.close()
