"""Production composition contracts for generic Runtime Profiles.

These tests deliberately exercise composition boundaries instead of pTLC-specific
routes. A Profile is data, and the unified API observes the Canonical workflow
owned by the one RuntimeService.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import unilabos.app.communication as communication_module
from unilabos.app.communication import CommunicationClientFactory
from unilabos.app.local_bridge.runtime_action_api import (
    RuntimeActionCatalogProxyError,
)
from unilabos.app.local_bridge.schedule_ws import ScheduleSession
from unilabos.app.local_bridge.server import LocalBridgeServer
from unilabos.config.config import BasicConfig
from unilabos.workflow.submission import workflow_submission_to_revision


def _current_revision_payload() -> dict[str, Any]:
    revision = workflow_submission_to_revision(
        {
            "name": "operator-selected-workflow",
            "nodes": [
                {
                    "id": "operator-selected-dose",
                    "data": {
                        "device_id": "station_device",
                        "method": "dose",
                        "params": {"volume": 2.5},
                    },
                }
            ],
            "edges": [],
        }
    )
    return revision.model_dump(mode="json")


class CurrentWorkflowRuntime:
    """Small RuntimeService contract double with a non-demo current revision."""

    def __init__(self) -> None:
        self.canonical = _current_revision_payload()
        self.start_calls: list[dict[str, Any]] = []

    def get_workflow(self) -> dict[str, Any]:
        return {
            "definition": {
                "id": self.canonical["workflow_id"],
                "name": "Operator selected workflow",
            },
            "revision": {
                "id": self.canonical["revision_id"],
                "canonical": self.canonical,
                "nodes": [
                    {
                        "id": "operator-selected-dose",
                        "label": "Operator selected dose",
                        "deviceId": "station_device",
                        "action": "dose",
                    }
                ],
                "edges": [],
            },
        }

    async def start_run(self, body: dict[str, Any]) -> dict[str, str]:
        self.start_calls.append(body)
        return {"id": "runtime-current-1", "status": "pending"}


def test_production_unified_api_uses_shared_current_canonical() -> None:
    """The unified API injection path must not replace current state."""

    async def scenario() -> None:
        server = LocalBridgeServer(offline=True)
        state = server._get_local_api_state()  # noqa: SLF001
        assert state is not None
        runtime = CurrentWorkflowRuntime()
        state._runtime_service = runtime  # noqa: SLF001 - exercise server composition

        # The only production UI adapter resolves the shared service object.
        assert server._api_server._get_state().runtime_service is runtime  # noqa: SLF001
        current = state.runtime_workflow()
        assert current is not None
        assert current["revision"]["canonical"] == runtime.canonical

    asyncio.run(scenario())


class RecordingRuntimeActionProxy:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def fetch(
        self,
        *,
        force: bool = False,
    ) -> tuple[dict[str, dict[str, Any]], str]:
        assert force is True
        self.calls += 1
        if self.fail:
            raise RuntimeActionCatalogProxyError(
                "ACTION_CATALOG_UNAVAILABLE",
                "OS action endpoint unavailable",
            )
        return (
            {
                "host_node.test_latency": {
                    "inputs": {},
                    "outputs": {"status": {"type": "string"}},
                }
            },
            "runtime-revision-1",
        )


def test_real_bridge_installs_live_catalog_after_host_ready() -> None:
    async def scenario() -> None:
        proxy = RecordingRuntimeActionProxy()
        server = LocalBridgeServer(
            offline=False,
            runtime_action_proxy=proxy,  # type: ignore[arg-type]
        )

        async def send(_message: dict[str, Any]) -> None:
            return None

        session = ScheduleSession(send, session_id="real-edge")
        server._adopt_session(session)  # noqa: SLF001
        state = server._get_local_api_state()  # noqa: SLF001
        assert state is not None
        assert state.runtime_actions()["available"] is False
        revision = {
            "schema_version": "2",
            "revision_id": "latency-revision-1",
            "workflow_id": "latency-workflow",
            "invocations": [
                {
                    "node_id": "latency-1",
                    "action_ref": "host_node.test_latency",
                }
            ],
            "control_edges": [],
        }
        assert state.validate_runtime_workflow(
            {"revision": revision}
        )["valid"] is False

        await session.handle_incoming(
            {"action": "host_node_ready", "data": {"status": "ready"}}
        )

        catalog = state.runtime_actions()
        assert proxy.calls == 1
        assert catalog["available"] is True
        assert catalog["revision"] == "runtime-revision-1"
        assert [
            action["action_ref"] for action in catalog["actions"]
        ] == ["host_node.test_latency"]
        assert state.validate_runtime_workflow(
            {"revision": revision}
        )["valid"] is True

    asyncio.run(scenario())


def test_real_bridge_catalog_failure_stays_fail_closed() -> None:
    async def scenario() -> None:
        proxy = RecordingRuntimeActionProxy(fail=True)
        server = LocalBridgeServer(
            offline=False,
            runtime_action_proxy=proxy,  # type: ignore[arg-type]
        )

        async def send(_message: dict[str, Any]) -> None:
            return None

        session = ScheduleSession(send, session_id="real-edge")
        server._adopt_session(session)  # noqa: SLF001
        state = server._get_local_api_state()  # noqa: SLF001
        assert state is not None

        await session.handle_incoming(
            {"action": "host_node_ready", "data": {"status": "ready"}}
        )

        catalog = state.runtime_actions()
        assert catalog["available"] is False
        assert catalog["actions"] == []
        assert catalog["error"] == "OS action endpoint unavailable"

    asyncio.run(scenario())


def test_cli_profile_uses_generic_configured_connections_without_callable_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real ``--profile`` path resolves connection_ref from generic config.

    ``runtime_connections`` is the generic ref-to-transport registry populated by
    BasicConfig (and therefore available to Python config / CLI / env composition).
    Users must not have to inject a Python resolver callable in test code, and no
    profile name or device family receives a dedicated endpoint or code branch.
    """

    station_transport = object()
    profile_paths = ["/profiles/generic-station.yaml"]
    loaded_profiles = {"generic-station": object()}
    runtime_drivers = {"station_device": object()}
    resolved: list[tuple[str, object | None]] = []

    monkeypatch.setattr(
        BasicConfig,
        "runtime_profile_paths",
        profile_paths,
        raising=False,
    )
    monkeypatch.setattr(
        BasicConfig,
        "runtime_connection_resolver",
        None,
        raising=False,
    )
    monkeypatch.setattr(
        BasicConfig,
        "runtime_connections",
        {"STATION_BUS_A": station_transport},
        raising=False,
    )
    monkeypatch.setattr(
        communication_module,
        "discover_driver_catalog",
        lambda: {"generic-driver": object()},
    )
    monkeypatch.setattr(
        communication_module,
        "load_profiles",
        lambda paths, *, driver_catalog: loaded_profiles,
    )

    def build_runtime_drivers(
        profiles: dict[str, object],
        driver_catalog: dict[str, object],
        connection_resolver: Any,
    ) -> dict[str, object]:
        del driver_catalog
        assert profiles is loaded_profiles
        for connection_ref in ("STATION_BUS_A", "UNKNOWN_BUS"):
            resolved.append((connection_ref, connection_resolver(connection_ref)))
        return runtime_drivers

    class RecordingWebSocketClient:
        def __init__(self, **kwargs: object) -> None:
            self.runtime_drivers = kwargs.get("runtime_drivers")

    monkeypatch.setattr(
        communication_module,
        "build_runtime_drivers",
        build_runtime_drivers,
    )
    monkeypatch.setattr(
        "unilabos.app.ws_client.WebSocketClient",
        RecordingWebSocketClient,
    )

    client = CommunicationClientFactory._create_websocket_client()

    assert client.runtime_drivers is runtime_drivers
    assert resolved == [
        ("STATION_BUS_A", station_transport),
        ("UNKNOWN_BUS", None),
    ]
