"""Production composition roots must enable the generic runtime services."""

from __future__ import annotations

import asyncio
from queue import Queue
from typing import Any

import unilabos.app.local_bridge.offline_os as offline_module
import unilabos.app.ws_client as ws_module
import unilabos.scheduler.task_dag_runner as runner_module
from unilabos.app.local_bridge.offline_os import OfflineOS
from unilabos.app.ws_client import DeviceActionManager, MessageProcessor
from unilabos.runtime.event_store import SQLiteEventJournal
from unilabos.scheduler.dag_model import NodeState, TaskDag
from unilabos.scheduler.resource_lock import (
    LeaseRequest,
    ResolvedResourceClaim,
    ResourceLockManager,
)
from unilabos.scheduler.task_dag_runner import TaskDagRunner


def _payload(task_id: str, *, device_id: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "notebook_id": "composition-test",
        "server_info": {},
        "nodes": [
            {
                "node_id": f"{task_id}-node",
                "device_id": device_id,
                "action": "execute",
                "resource_claims": [
                    {
                        "resource_id": "shared-workcell",
                        "quantity": 1,
                        "mode": "exclusive",
                    }
                ],
            }
        ],
        "edges": [],
    }


def _dag(task_id: str = "runner-task") -> TaskDag:
    return TaskDag.from_message(_payload(task_id, device_id="generic-device"))


async def _settle() -> None:
    for _ in range(8):
        await asyncio.sleep(0)


def test_task_dag_runner_forwards_shared_runtime_services(monkeypatch, tmp_path) -> None:
    locks = ResourceLockManager(runtime_epoch="epoch-1")
    journal = SQLiteEventJournal(tmp_path / "runtime.sqlite", runtime_epoch="epoch-1")
    captured: dict[str, object] = {}

    class RecordingExecutor:
        def __init__(self, dag, submit, **kwargs) -> None:
            captured.update(kwargs)

        def cancel(self) -> None:
            return None

        async def run(self) -> dict[str, NodeState]:
            return {"runner-task-node": NodeState.SUCCESS}

    monkeypatch.setattr(runner_module, "DagExecutor", RecordingExecutor)
    runner = TaskDagRunner(
        _dag(),
        lambda _node: None,
        resource_lock_manager=locks,
        journal=journal,
    )

    assert runner is not None
    assert captured["resource_lock_manager"] is locks
    assert captured["journal"] is journal


def test_message_processor_reuses_one_runtime_across_task_dag_runs(
    monkeypatch, tmp_path
) -> None:
    locks = ResourceLockManager(runtime_epoch="epoch-1")
    journal = SQLiteEventJournal(tmp_path / "runtime.sqlite", runtime_epoch="epoch-1")
    release_runs: asyncio.Event
    captured: list[dict[str, object]] = []

    class RecordingRunner:
        def __init__(self, dag, on_start_node, **kwargs) -> None:
            self.dag = dag
            captured.append(kwargs)

        async def run(self) -> dict[str, NodeState]:
            await release_runs.wait()
            return {
                node_id: NodeState.SUCCESS for node_id in self.dag.nodes
            }

    monkeypatch.setattr(ws_module, "TaskDagRunner", RecordingRunner)
    processor = MessageProcessor(
        "ws://runtime-test",
        Queue(),
        DeviceActionManager(),
        resource_lock_manager=locks,
        journal=journal,
    )

    async def scenario() -> None:
        nonlocal release_runs
        release_runs = asyncio.Event()
        processor._loop = asyncio.get_running_loop()  # noqa: SLF001
        await processor._handle_task_dag(  # noqa: SLF001
            _payload("run-one", device_id="generic-device-a")
        )
        await processor._handle_task_dag(  # noqa: SLF001
            _payload("run-two", device_id="generic-device-b")
        )
        await _settle()

        assert len(captured) == 2
        assert captured[0]["resource_lock_manager"] is locks
        assert captured[1]["resource_lock_manager"] is locks
        assert captured[0]["journal"] is journal
        assert captured[1]["journal"] is journal

        first_lease = await captured[0]["resource_lock_manager"].acquire_all(
            LeaseRequest(
                holder_id="run-one-node",
                claims=(
                    ResolvedResourceClaim(
                        resource_id="shared-workcell",
                        mode="exclusive",
                    ),
                ),
            )
        )
        assert first_lease is not None
        second_lease = await captured[1]["resource_lock_manager"].acquire_all(
            LeaseRequest(
                holder_id="run-two-node",
                claims=(
                    ResolvedResourceClaim(
                        resource_id="shared-workcell",
                        mode="exclusive",
                    ),
                ),
            )
        )
        assert second_lease is None

        release_runs.set()
        await _settle()

    asyncio.run(scenario())


def test_offline_os_reuses_runtime_services_for_every_quick_debug_run(
    monkeypatch, tmp_path
) -> None:
    locks = ResourceLockManager(runtime_epoch="epoch-1")
    journal = SQLiteEventJournal(tmp_path / "runtime.sqlite", runtime_epoch="epoch-1")
    captured: list[dict[str, object]] = []

    class RecordingExecutor:
        def __init__(self, dag, submit, **kwargs) -> None:
            self.dag = dag
            captured.append(kwargs)

        def cancel(self) -> None:
            return None

        async def run(self) -> dict[str, NodeState]:
            return {node_id: NodeState.SUCCESS for node_id in self.dag.nodes}

    monkeypatch.setattr(offline_module, "DagExecutor", RecordingExecutor)
    offline = OfflineOS(resource_lock_manager=locks, journal=journal)

    async def scenario() -> None:
        await offline._start_task(  # noqa: SLF001
            _payload("offline-one", device_id="simulated-device-a")
        )
        await offline._start_task(  # noqa: SLF001
            _payload("offline-two", device_id="simulated-device-b")
        )
        await _settle()

    asyncio.run(scenario())

    assert len(captured) == 2
    assert captured[0]["resource_lock_manager"] is locks
    assert captured[1]["resource_lock_manager"] is locks
    assert captured[0]["journal"] is journal
    assert captured[1]["journal"] is journal
