"""Result outputs must survive the production job-status callback path."""

from __future__ import annotations

import asyncio
from queue import Queue
from types import SimpleNamespace
from typing import Any

import unilabos.app.ws_client as ws_module
from unilabos.app.ws_client import (
    DeviceActionManager,
    MessageProcessor,
    QueueItem,
    WebSocketClient,
)
from unilabos.scheduler.dag_model import DagNode, NodeState, TaskDag
from unilabos.scheduler.task_dag_runner import TaskDagRunner
from unilabos.utils.type_check import serialize_result_info


def _binding_dag() -> TaskDag:
    return TaskDag.from_message(
        {
            "task_id": "result-binding",
            "notebook_id": "binding-test",
            "server_info": {},
            "nodes": [
                {
                    "node_id": "measure",
                    "device_id": "generic-sensor",
                    "action": "measure",
                    "output_schema": {"reading": {"type": "number"}},
                },
                {
                    "node_id": "consume",
                    "device_id": "generic-consumer",
                    "action": "consume",
                    "input_bindings": {
                        "amount": {
                            "kind": "node_output",
                            "node_id": "measure",
                            "output": "reading",
                        }
                    },
                    "input_schema": {"amount": {"type": "number"}},
                },
            ],
            "edges": [
                {
                    "source_node_uuid": "measure",
                    "target_node_uuid": "consume",
                }
            ],
        }
    )


async def _settle() -> None:
    for _ in range(8):
        await asyncio.sleep(0)


def test_runner_materializes_node_output_from_job_status_return_info() -> None:
    started: list[tuple[str, dict[str, Any]]] = []

    def start_node(node: DagNode) -> None:
        started.append((node.node_id, dict(node.action_args)))

    runner = TaskDagRunner(_binding_dag(), start_node)

    async def scenario() -> dict[str, NodeState]:
        run_task = asyncio.create_task(runner.run())
        await _settle()
        assert started == [("measure", {})]

        runner.notify_terminal(
            "measure",
            "success",
            return_info=serialize_result_info(
                "",
                True,
                {"reading": 1.25},
            ),
        )
        await _settle()
        assert started == [
            ("measure", {}),
            ("consume", {"amount": 1.25}),
        ]

        runner.notify_terminal(
            "consume",
            "success",
            return_info=serialize_result_info("", True, {}),
        )
        return await asyncio.wait_for(run_task, timeout=1)

    states = asyncio.run(scenario())
    assert states == {
        "measure": NodeState.SUCCESS,
        "consume": NodeState.SUCCESS,
    }
    assert runner._executor.results["measure"].outputs == {  # noqa: SLF001
        "reading": 1.25
    }


def test_publish_job_status_forwards_return_info_to_dag_runner(monkeypatch) -> None:
    processor = MessageProcessor(
        "ws://result-binding-test",
        Queue(),
        DeviceActionManager(),
    )
    received: list[tuple[str, str, dict[str, Any] | None]] = []

    class RecordingRunner:
        def notify_terminal(
            self,
            job_id: str,
            status: str,
            *,
            return_info: dict[str, Any] | None = None,
        ) -> None:
            received.append((job_id, status, return_info))

    class NoHost:
        @staticmethod
        def get_instance(_index: int) -> None:
            return None

    processor._task_dag_runners["result-binding"] = RecordingRunner()  # noqa: SLF001
    monkeypatch.setattr(ws_module, "HostNode", NoHost)
    return_info = serialize_result_info("", True, {"reading": 2.5})
    item = QueueItem(
        task_type="job_call_back_status",
        device_id="generic-sensor",
        action_name="measure",
        task_id="result-binding",
        job_id="measure",
        notebook_id="binding-test",
        device_action_key="/devices/generic-sensor/measure",
    )
    publisher = SimpleNamespace(
        is_disabled=False,
        _job_running_last_sent={},
        queue_processor=SimpleNamespace(handle_job_completed=lambda *_args: None),
        message_processor=processor,
        get_cached_job_start_response_status=lambda *_args: "",
        cache_job_start_response=lambda *_args: None,
        is_connected=lambda: False,
    )

    WebSocketClient.publish_job_status(
        publisher,
        {},
        item,
        "success",
        return_info,
    )

    assert received == [("measure", "success", return_info)]


def test_publish_job_status_preserves_first_failed_terminal(monkeypatch) -> None:
    processor = MessageProcessor(
        "ws://terminal-precedence-test",
        Queue(),
        DeviceActionManager(),
    )
    received: list[tuple[str, str, dict[str, Any] | None]] = []
    sent: list[dict[str, Any]] = []
    cached_status = ""

    class RecordingRunner:
        def notify_terminal(
            self,
            job_id: str,
            status: str,
            *,
            return_info: dict[str, Any] | None = None,
        ) -> None:
            received.append((job_id, status, return_info))

    class NoHost:
        @staticmethod
        def get_instance(_index: int) -> None:
            return None

    def get_cached_status(*_args: Any) -> str:
        return cached_status

    def cache_response(
        _item: QueueItem,
        _message: dict[str, Any],
        status: str,
    ) -> None:
        nonlocal cached_status
        cached_status = status

    processor._task_dag_runners["terminal-precedence"] = RecordingRunner()  # noqa: SLF001
    monkeypatch.setattr(ws_module, "HostNode", NoHost)
    item = QueueItem(
        task_type="job_call_back_status",
        device_id="host_node",
        action_name="test_latency",
        task_id="terminal-precedence",
        job_id="latency",
        notebook_id="",
        device_action_key="/devices/host_node/test_latency",
    )
    publisher = SimpleNamespace(
        is_disabled=False,
        _job_running_last_sent={},
        queue_processor=SimpleNamespace(handle_job_completed=lambda *_args: None),
        message_processor=processor,
        get_cached_job_start_response_status=get_cached_status,
        cache_job_start_response=cache_response,
        is_connected=lambda: True,
    )
    processor.send_message = lambda message: sent.append(message) or True

    failed_info = serialize_result_info("all ping requests timed out", False, {})
    WebSocketClient.publish_job_status(
        publisher,
        {},
        item,
        "failed",
        failed_info,
    )
    WebSocketClient.publish_job_status(
        publisher,
        {},
        item,
        "success",
        serialize_result_info("", True, {}),
    )

    assert cached_status == "failed"
    assert received == [("latency", "failed", failed_info)]
    assert [message["data"]["status"] for message in sent] == ["failed"]
