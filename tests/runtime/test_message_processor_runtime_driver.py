"""Production DAG dispatch must select injected generic runtime drivers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue
from typing import Any

import pytest
import yaml

import unilabos.app.ws_client as ws_module
from unilabos.app.local_bridge.schedule_ws import ScheduleSession
from unilabos.app.ws_client import (
    DeviceActionManager,
    JobInfo,
    JobStatus,
    MessageProcessor,
)
from unilabos.devices.generic_plc_macro import DeclarativePLCMacroDriver
from unilabos.runtime.event_store import SQLiteEventJournal
from unilabos.runtime.profile_loader import ProfileLoader
from unilabos.scheduler.dag_model import NodeState, TaskDag
from unilabos.scheduler.resource_lock import ResourceLockManager


@dataclass(frozen=True)
class ScriptedMacroResult:
    terminal: str
    outputs: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    physical_state: str = "confirmed"
    reconcile_required: bool = False


class ScriptedDriver:
    def __init__(self, result: ScriptedMacroResult) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def run_macro(
        self,
        macro: str,
        *,
        inputs: dict[str, Any],
    ) -> ScriptedMacroResult:
        self.calls.append((macro, dict(inputs)))
        return self.result


class BlockingDriver:
    """模拟已进入物理动作、且会一直等待设备回包的 generic driver。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()
        self.task: asyncio.Task[Any] | None = None

    async def run_macro(
        self,
        macro: str,
        *,
        inputs: dict[str, Any],
    ) -> ScriptedMacroResult:
        del macro, inputs
        self.task = asyncio.current_task()
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return ScriptedMacroResult(terminal="succeeded")


class RecordingPLC:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def execute(self, sample_id: str, amount: int) -> None:
        self.calls.append((sample_id, amount))


def _write_generic_profile(root: Path) -> Path:
    spec = {
        "schema_version": 2,
        "device": {"id": "generic-station"},
        "actions": [
            {
                "id": "execute",
                "params": [
                    {"name": "sample_id", "type": "string"},
                    {"name": "amount", "type": "integer"},
                ],
                "results": [{"name": "terminal", "type": "string"}],
                "resource_claims": [
                    {
                        "resource_ref": "generic-cell",
                        "resource_type": "process_cell",
                        "mode": "exclusive",
                        "scope": "action",
                    }
                ],
            }
        ],
    }
    profile = {
        "schema_version": 1,
        "profile_id": "generic-runtime-profile",
        "device_spec": "device.yaml",
        "default_device_binding": {
            "device_id": "generic-station",
            "driver_key": "generic_plc_macro",
            "connection_ref": "GENERIC_CONNECTION",
        },
        "resource_topology": {
            "resources": [
                {
                    "id": "generic-cell",
                    "resource_type": "process_cell",
                }
            ]
        },
        "driver_config": {
            "macros": {
                "execute": [
                    {
                        "call": "execute",
                        "args": [
                            {"input": "sample_id"},
                            {"input": "amount"},
                        ],
                    }
                ]
            }
        },
    }
    (root / "device.yaml").write_text(
        yaml.safe_dump(spec, sort_keys=False),
        encoding="utf-8",
    )
    profile_path = root / "profile.yaml"
    profile_path.write_text(
        yaml.safe_dump(profile, sort_keys=False),
        encoding="utf-8",
    )
    return profile_path


def _payload(
    task_id: str,
    *,
    device_id: str = "generic-station",
    output_schema: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "notebook_id": "runtime-driver-test",
        "server_info": {},
        "nodes": [
            {
                "node_id": "execute-node",
                "device_id": device_id,
                "action": "execute",
                "action_args": {"sample_id": "sample-7", "amount": 7},
                "output_schema": output_schema
                or {"terminal": {"type": "string"}},
                "resource_claims": [
                    {
                        "resource_id": "generic-cell",
                        "quantity": 1,
                        "mode": "exclusive",
                        "scope": "action",
                    }
                ],
            }
        ],
        "edges": [],
    }


def _processor(
    *,
    tmp_path: Path,
    runtime_drivers: dict[str, object],
) -> tuple[MessageProcessor, ResourceLockManager, SQLiteEventJournal]:
    locks = ResourceLockManager(runtime_epoch="runtime-driver-epoch")
    journal = SQLiteEventJournal(
        tmp_path / "runtime-driver.sqlite",
        runtime_epoch="runtime-driver-epoch",
    )
    processor = MessageProcessor(
        "ws://runtime-driver-test",
        Queue(),
        DeviceActionManager(),
        resource_lock_manager=locks,
        journal=journal,
        runtime_drivers=runtime_drivers,
    )
    return processor, locks, journal


async def _run_to_terminal(
    processor: MessageProcessor,
    payload: dict[str, Any],
) -> Any:
    processor._loop = asyncio.get_running_loop()  # noqa: SLF001
    await processor._handle_task_dag(payload)  # noqa: SLF001
    task_id = str(payload["task_id"])
    runner = processor._task_dag_runners[task_id]  # noqa: SLF001
    for _ in range(200):
        if task_id not in processor._task_dag_runners:  # noqa: SLF001
            break
        await asyncio.sleep(0)
    assert task_id not in processor._task_dag_runners  # noqa: SLF001
    return runner


async def _settle_until(
    predicate: Callable[[], bool],
    *,
    turns: int = 300,
) -> bool:
    """只让出事件循环，避免用 wall-clock sleep 掩盖取消竞态。"""
    for _ in range(turns):
        if predicate():
            return True
        await asyncio.sleep(0)
    return predicate()


async def _cleanup_blocking_driver(driver: BlockingDriver) -> None:
    """RED 失败时也回收测试制造的悬挂 task，避免污染后续用例。"""
    task = driver.task
    if task is None or task.done():
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_loaded_generic_profile_driver_runs_through_real_task_dag_chain(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profile = ProfileLoader(
        driver_catalog={"generic_plc_macro": DeclarativePLCMacroDriver}
    ).load(_write_generic_profile(tmp_path))
    plc = RecordingPLC()
    driver = DeclarativePLCMacroDriver(
        plc=plc,
        driver_config=profile.driver_config,
    )
    processor, locks, journal = _processor(
        tmp_path=tmp_path,
        runtime_drivers={profile.driver_binding["device_id"]: driver},
    )
    host_calls: list[dict[str, Any]] = []

    async def reject_host_fallback(payload: dict[str, Any]) -> None:
        host_calls.append(payload)
        raise AssertionError("registered runtime driver must bypass HostNode")

    monkeypatch.setattr(processor, "_handle_job_start", reject_host_fallback)

    async def scenario() -> Any:
        return await _run_to_terminal(processor, _payload("loaded-driver-run"))

    runner = asyncio.run(scenario())

    assert plc.calls == [("sample-7", 7)]
    assert host_calls == []
    assert runner._executor.walk.snapshot() == {  # noqa: SLF001
        "execute-node": NodeState.SUCCESS
    }
    projection = journal.load_node_projection("loaded-driver-run", "execute-node")
    assert projection is not None
    assert projection.terminal == "succeeded"
    assert projection.result == {"terminal": "succeeded"}
    assert locks.active_leases() == ()


@pytest.mark.parametrize(
    (
        "result",
        "expected_state",
        "expected_terminal",
        "expected_lock_event",
        "expected_live_lease_state",
    ),
    [
        (
            ScriptedMacroResult(
                terminal="succeeded",
                outputs={"reading": 42},
            ),
            NodeState.SUCCESS,
            "succeeded",
            "lock_released",
            None,
        ),
        (
            ScriptedMacroResult(
                terminal="failed",
                error="device rejected action",
                physical_state="confirmed_failed",
            ),
            NodeState.FAILED,
            "failed",
            "lock_released",
            None,
        ),
        (
            ScriptedMacroResult(
                terminal="cancelled",
                error="device confirmed stop",
                physical_state="confirmed_safe",
            ),
            NodeState.CANCELLED,
            "cancelled",
            "lock_released",
            None,
        ),
        (
            ScriptedMacroResult(
                terminal="failed",
                error="transport disconnected",
                physical_state="unknown",
                reconcile_required=True,
            ),
            NodeState.FAILED,
            "failed",
            "lock_unknown",
            "unknown",
        ),
    ],
    ids=["success", "confirmed-failure", "confirmed-cancel", "physical-unknown"],
)
def test_runtime_driver_terminal_flows_to_journal_and_resource_fence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    result: ScriptedMacroResult,
    expected_state: NodeState,
    expected_terminal: str,
    expected_lock_event: str,
    expected_live_lease_state: str | None,
) -> None:
    driver = ScriptedDriver(result)
    processor, locks, journal = _processor(
        tmp_path=tmp_path,
        runtime_drivers={"generic-station": driver},
    )
    host_calls: list[dict[str, Any]] = []

    async def reject_host_fallback(payload: dict[str, Any]) -> None:
        host_calls.append(payload)
        raise AssertionError("registered runtime driver must bypass HostNode")

    monkeypatch.setattr(processor, "_handle_job_start", reject_host_fallback)
    task_id = f"driver-terminal-{expected_state.value}-{expected_lock_event}"

    async def scenario() -> Any:
        return await _run_to_terminal(
            processor,
            _payload(
                task_id,
                output_schema={"reading": {"type": "number"}},
            ),
        )

    runner = asyncio.run(scenario())

    assert driver.calls == [
        ("execute", {"sample_id": "sample-7", "amount": 7})
    ]
    assert host_calls == []
    assert runner._executor.walk.snapshot() == {  # noqa: SLF001
        "execute-node": expected_state
    }
    projection = journal.load_node_projection(task_id, "execute-node")
    assert projection is not None
    assert projection.terminal == expected_terminal
    event_types = {
        event.type
        for event in journal.list_events(task_id)
        if event.node_id == "execute-node"
    }
    assert expected_lock_event in event_types
    live_leases = locks.active_leases()
    if expected_live_lease_state is None:
        assert live_leases == ()
    else:
        assert len(live_leases) == 1
        assert live_leases[0].state == expected_live_lease_state
        assert live_leases[0].reason == result.error


def test_cancel_task_stops_blocking_runtime_driver_without_hanging_runner(
    tmp_path: Path,
) -> None:
    """真实 cancel_task 入口既收口 runner，也必须停止仍在 await 的 driver task。"""
    driver = BlockingDriver()
    processor, _locks, _journal = _processor(
        tmp_path=tmp_path,
        runtime_drivers={"generic-station": driver},
    )
    task_id = "blocking-driver-cancel"

    async def scenario() -> tuple[bool, bool, bool]:
        processor._loop = asyncio.get_running_loop()  # noqa: SLF001
        await processor._process_message("task_dag", _payload(task_id))  # noqa: SLF001
        assert await _settle_until(driver.started.is_set)

        await processor._process_message(  # noqa: SLF001
            "cancel_task",
            {"task_id": task_id},
        )
        runner_stopped = await _settle_until(
            lambda: task_id not in processor._task_dag_runners  # noqa: SLF001
        )
        await _settle_until(
            lambda: driver.task is not None and driver.task.done()
        )
        driver_cancelled = driver.cancelled.is_set()
        driver_stopped = driver.task is not None and driver.task.done()
        await _cleanup_blocking_driver(driver)
        return runner_stopped, driver_cancelled, driver_stopped

    runner_stopped, driver_cancelled, driver_stopped = asyncio.run(scenario())

    assert runner_stopped, "cancel_task 后 DAG runner 不得悬挂"
    assert driver_cancelled, "cancel_task 必须取消仍在等待设备回包的 driver"
    assert driver_stopped, "driver asyncio task 必须停止等待"


def test_cancelled_runtime_driver_persists_cancel_and_unknown_fence(
    tmp_path: Path,
) -> None:
    """取消已起跑的物理动作须写 node_cancelled + lock_unknown，并保留 unknown lease。"""
    driver = BlockingDriver()
    processor, locks, journal = _processor(
        tmp_path=tmp_path,
        runtime_drivers={"generic-station": driver},
    )
    task_id = "blocking-driver-unknown-fence"

    async def scenario() -> tuple[Any, set[str], Any, tuple[Any, ...]]:
        processor._loop = asyncio.get_running_loop()  # noqa: SLF001
        await processor._process_message("task_dag", _payload(task_id))  # noqa: SLF001
        assert await _settle_until(driver.started.is_set)
        runner = processor._task_dag_runners[task_id]  # noqa: SLF001

        await processor._process_message(  # noqa: SLF001
            "cancel_task",
            {"task_id": task_id},
        )
        assert await _settle_until(
            lambda: task_id not in processor._task_dag_runners  # noqa: SLF001
        )
        projection = journal.load_node_projection(task_id, "execute-node")
        event_types = {
            event.type
            for event in journal.list_events(task_id)
            if event.node_id == "execute-node"
        }
        leases = locks.active_leases()
        await _cleanup_blocking_driver(driver)
        return runner, event_types, projection, leases

    runner, event_types, projection, leases = asyncio.run(scenario())

    assert runner._executor.walk.snapshot() == {  # noqa: SLF001
        "execute-node": NodeState.CANCELLED
    }
    assert projection is not None
    assert projection.terminal == "cancelled"
    assert projection.result["physical_state"] == "unknown"
    assert projection.result["reconcile_required"] is True
    assert {"node_cancelled", "lock_unknown"} <= event_types
    assert "lock_released" not in event_types
    assert len(leases) == 1
    assert leases[0].state == "unknown"


def test_cancelled_runtime_driver_reports_unknown_terminal_to_bridge(
    tmp_path: Path,
) -> None:
    """OS 必须把 driver 取消回流给 bridge；物理未知只能投影为 reconciling。"""
    driver = BlockingDriver()
    processor, _locks, _journal = _processor(
        tmp_path=tmp_path,
        runtime_drivers={"generic-station": driver},
    )
    task_id = "blocking-driver-bridge-reconcile"

    async def send_to_os(message: dict[str, Any]) -> None:
        await processor._process_message(  # noqa: SLF001
            str(message["action"]),
            dict(message["data"]),
        )

    async def scenario() -> tuple[list[dict[str, Any]], object, bool, bool]:
        processor._loop = asyncio.get_running_loop()  # noqa: SLF001
        bridge = ScheduleSession(send_to_os)
        handle = await bridge.submit_dag(TaskDag.from_message(_payload(task_id)))
        assert await _settle_until(driver.started.is_set)

        await bridge.cancel_task(task_id)
        await _settle_until(
            lambda: (
                driver.cancelled.is_set()
                and task_id not in processor._task_dag_runners  # noqa: SLF001
                and not processor.send_queue.empty()
            )
        )

        outbound: list[dict[str, Any]] = []
        while not processor.send_queue.empty():
            outbound.append(processor.send_queue.get_nowait())
        job_statuses = [
            message for message in outbound if message.get("action") == "job_status"
        ]
        for message in job_statuses:
            await bridge.handle_incoming(message)
        projected_state = handle.node_states["execute-node"]
        is_finished = handle.finished
        cancelled_before_cleanup = driver.cancelled.is_set()
        await _cleanup_blocking_driver(driver)
        return (
            job_statuses,
            projected_state,
            is_finished,
            cancelled_before_cleanup,
        )

    job_statuses, projected_state, is_finished, driver_cancelled = asyncio.run(
        scenario()
    )

    assert len(job_statuses) == 1
    assert driver_cancelled
    terminal = job_statuses[0]["data"]
    assert terminal["status"] == "cancelled"
    assert terminal["return_info"]["physical_state"] == "unknown"
    assert terminal["return_info"]["reconcile_required"] is True
    assert projected_state == "reconciling"
    assert not is_finished


def test_cancel_task_preserves_unregistered_hostnode_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """没有 generic driver/runner 时，既有 task cancel 仍经 HostNode.cancel_goal。"""
    processor, _locks, _journal = _processor(
        tmp_path=tmp_path,
        runtime_drivers={},
    )
    task_id = "hostnode-cancel-fallback"
    job = JobInfo(
        job_id="host-job",
        task_id=task_id,
        device_id="legacy-device",
        notebook_id="",
        action_name="execute",
        device_action_key="/devices/legacy-device/execute",
        status=JobStatus.QUEUE,
        start_time=0.0,
    )
    should_start, _lock_became_busy = processor.device_manager.enqueue_job(job)
    assert should_start

    class RecordingHost:
        def __init__(self) -> None:
            self.cancelled_jobs: list[str] = []

        def cancel_goal(self, job_id: str) -> bool:
            self.cancelled_jobs.append(job_id)
            return True

    host = RecordingHost()
    monkeypatch.setattr(
        ws_module.HostNode,
        "get_instance",
        classmethod(lambda _cls, _timeout=0: host),
    )

    asyncio.run(
        processor._process_message(  # noqa: SLF001
            "cancel_task",
            {"task_id": task_id},
        )
    )

    assert host.cancelled_jobs == ["host-job"]
    assert processor.device_manager.get_job_info("host-job") is None


def test_unregistered_device_preserves_existing_hostnode_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    registered_driver = ScriptedDriver(
        ScriptedMacroResult(terminal="succeeded")
    )
    processor, locks, journal = _processor(
        tmp_path=tmp_path,
        runtime_drivers={"registered-device": registered_driver},
    )
    host_calls: list[dict[str, Any]] = []

    async def complete_via_host(payload: dict[str, Any]) -> None:
        host_calls.append(payload)
        processor.notify_task_dag_terminal(
            str(payload["task_id"]),
            str(payload["job_id"]),
            "success",
            return_info={"return_value": {"path": "hostnode"}},
        )

    monkeypatch.setattr(processor, "_handle_job_start", complete_via_host)

    async def scenario() -> Any:
        return await _run_to_terminal(
            processor,
            _payload(
                "host-fallback-run",
                device_id="unregistered-device",
                output_schema={"path": {"type": "string"}},
            ),
        )

    runner = asyncio.run(scenario())

    assert registered_driver.calls == []
    assert len(host_calls) == 1
    assert host_calls[0]["device_id"] == "unregistered-device"
    assert host_calls[0]["action"] == "execute"
    assert host_calls[0]["action_args"] == {
        "sample_id": "sample-7",
        "amount": 7,
    }
    assert runner._executor.walk.snapshot() == {  # noqa: SLF001
        "execute-node": NodeState.SUCCESS
    }
    projection = journal.load_node_projection("host-fallback-run", "execute-node")
    assert projection is not None
    assert projection.result == {"path": "hostnode"}
    assert locks.active_leases() == ()


def test_message_processor_runtime_dispatch_has_no_family_specific_surface() -> None:
    source = Path(ws_module.__file__).read_text(encoding="utf-8").lower()

    assert "unilabos.devices.ptlc" not in source
    assert "/api/runtime/ptlc" not in source
