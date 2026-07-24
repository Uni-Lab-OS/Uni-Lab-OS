"""Generic loaded Profiles must compose into production runtime drivers."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping

import pytest
import yaml

import unilabos.app.communication as communication_module
import unilabos.app.ws_client as ws_module
from unilabos.runtime.profile_loader import LoadedProfile, ProfileLoader
from unilabos.scheduler.dag_model import TaskDag


class RecordingDriver:
    constructions: list[tuple[object, Mapping[str, Any]]] = []

    def __init__(
        self,
        *,
        plc: object,
        driver_config: Mapping[str, Any],
    ) -> None:
        self.plc = plc
        self.driver_config = driver_config
        self.constructions.append((plc, driver_config))


def _composition_api() -> ModuleType:
    try:
        return importlib.import_module("unilabos.runtime.profile_composition")
    except ModuleNotFoundError as exc:
        if exc.name != "unilabos.runtime.profile_composition":
            raise
        pytest.fail(
            "generic runtime profile composition root is missing",
            pytrace=False,
        )


def _write_profile(
    root: Path,
    *,
    profile_id: str,
    device_id: str,
    driver_key: str = "recording-driver",
    connection_ref: str,
    marker: str,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    spec = {
        "schema_version": 2,
        "device": {"id": device_id},
        "actions": [{"id": "execute"}],
    }
    profile = {
        "schema_version": 1,
        "profile_id": profile_id,
        "device_spec": "device.yaml",
        "default_device_binding": {
            "device_id": device_id,
            "driver_key": driver_key,
            "connection_ref": connection_ref,
        },
        "resource_topology": {"resources": []},
        "driver_config": {
            "marker": marker,
            "macros": {"execute": [{"call": "execute"}]},
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


def _load_profile(
    root: Path,
    *,
    profile_id: str,
    device_id: str,
    driver_key: str = "recording-driver",
    connection_ref: str,
    marker: str,
) -> LoadedProfile:
    profile_path = _write_profile(
        root,
        profile_id=profile_id,
        device_id=device_id,
        driver_key=driver_key,
        connection_ref=connection_ref,
        marker=marker,
    )
    return ProfileLoader(
        driver_catalog={driver_key: RecordingDriver}
    ).load(profile_path)


@pytest.fixture(autouse=True)
def _clear_driver_constructions() -> None:
    RecordingDriver.constructions.clear()


def test_builder_constructs_device_driver_from_loaded_profile_and_connection(
    tmp_path: Path,
) -> None:
    api = _composition_api()
    first = _load_profile(
        tmp_path / "first",
        profile_id="first-profile",
        device_id="station-a",
        connection_ref="PLC_A",
        marker="config-a",
    )
    second = _load_profile(
        tmp_path / "second",
        profile_id="second-profile",
        device_id="station-b",
        connection_ref="PLC_B",
        marker="config-b",
    )
    connection_a = object()
    connection_b = object()
    connections = {"PLC_A": connection_a, "PLC_B": connection_b}
    resolved_refs: list[str] = []

    def resolve_connection(connection_ref: str) -> object | None:
        resolved_refs.append(connection_ref)
        return connections.get(connection_ref)

    drivers = api.build_runtime_drivers(
        {first.profile_id: first, second.profile_id: second},
        {"recording-driver": RecordingDriver},
        resolve_connection,
    )

    assert set(drivers) == {"station-a", "station-b"}
    assert drivers["station-a"].plc is connection_a
    assert drivers["station-b"].plc is connection_b
    assert drivers["station-a"].driver_config is first.driver_config
    assert drivers["station-b"].driver_config is second.driver_config
    assert resolved_refs == ["PLC_A", "PLC_B"]


def test_builder_preflights_all_connections_before_constructing_any_driver(
    tmp_path: Path,
) -> None:
    api = _composition_api()
    valid = _load_profile(
        tmp_path / "valid",
        profile_id="valid-profile",
        device_id="station-a",
        connection_ref="PLC_A",
        marker="valid",
    )
    unresolved = _load_profile(
        tmp_path / "unresolved",
        profile_id="unresolved-profile",
        device_id="station-b",
        connection_ref="MISSING_CONNECTION",
        marker="must-not-construct",
    )
    connection_a = object()

    def resolve_connection(connection_ref: str) -> object | None:
        if connection_ref == "PLC_A":
            return connection_a
        return None

    with pytest.raises(ValueError, match="MISSING_CONNECTION"):
        api.build_runtime_drivers(
            {
                valid.profile_id: valid,
                unresolved.profile_id: unresolved,
            },
            {"recording-driver": RecordingDriver},
            resolve_connection,
        )

    assert RecordingDriver.constructions == []


def test_builder_rejects_duplicate_device_before_resolution_or_construction(
    tmp_path: Path,
) -> None:
    api = _composition_api()
    first = _load_profile(
        tmp_path / "first",
        profile_id="first-profile",
        device_id="shared-station",
        connection_ref="PLC_A",
        marker="first",
    )
    second = _load_profile(
        tmp_path / "second",
        profile_id="second-profile",
        device_id="shared-station",
        connection_ref="PLC_B",
        marker="second",
    )
    resolved_refs: list[str] = []

    def resolve_connection(connection_ref: str) -> object:
        resolved_refs.append(connection_ref)
        return object()

    with pytest.raises(ValueError, match="shared-station"):
        api.build_runtime_drivers(
            {first.profile_id: first, second.profile_id: second},
            {"recording-driver": RecordingDriver},
            resolve_connection,
        )

    assert resolved_refs == []
    assert RecordingDriver.constructions == []


def test_builder_rejects_unknown_driver_before_resolution_or_construction(
    tmp_path: Path,
) -> None:
    api = _composition_api()
    profile = _load_profile(
        tmp_path / "unknown-driver",
        profile_id="unknown-driver-profile",
        device_id="station-a",
        driver_key="missing-driver",
        connection_ref="PLC_A",
        marker="must-not-construct",
    )
    resolved_refs: list[str] = []

    def resolve_connection(connection_ref: str) -> object:
        resolved_refs.append(connection_ref)
        return object()

    with pytest.raises(ValueError, match="missing-driver"):
        api.build_runtime_drivers(
            {profile.profile_id: profile},
            {"recording-driver": RecordingDriver},
            resolve_connection,
        )

    assert resolved_refs == []
    assert RecordingDriver.constructions == []


def test_websocket_client_passes_runtime_driver_mapping_to_message_processor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_drivers = {"station-a": object()}
    captured: dict[str, Any] = {}

    class RecordingMessageProcessor:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            captured.update(kwargs)

        def set_queue_processor(self, queue_processor: object) -> None:
            self.queue_processor = queue_processor

        def set_websocket_client(self, websocket_client: object) -> None:
            self.websocket_client = websocket_client

    monkeypatch.setattr(ws_module, "MessageProcessor", RecordingMessageProcessor)

    client = ws_module.WebSocketClient(runtime_drivers=runtime_drivers)

    assert client.message_processor is not None
    assert captured["runtime_drivers"] is runtime_drivers


def test_no_profile_configuration_preserves_hostnode_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ws_module.WebSocketClient()
    processor = client.message_processor
    assert processor._runtime_drivers == {}  # noqa: SLF001
    host_payloads: list[dict[str, Any]] = []

    async def record_host_dispatch(payload: dict[str, Any]) -> None:
        host_payloads.append(payload)

    monkeypatch.setattr(processor, "_handle_job_start", record_host_dispatch)
    dag = TaskDag.from_message(
        {
            "task_id": "no-profile-run",
            "nodes": [
                {
                    "node_id": "legacy-host-node",
                    "device_id": "existing-host-device",
                    "action": "execute",
                    "action_args": {"amount": 3},
                }
            ],
            "edges": [],
        }
    )

    async def scenario() -> None:
        processor._loop = asyncio.get_running_loop()  # noqa: SLF001
        processor._start_dag_node(  # noqa: SLF001
            dag.nodes["legacy-host-node"],
            dag,
        )
        for _ in range(8):
            await asyncio.sleep(0)

    asyncio.run(scenario())

    assert len(host_payloads) == 1
    assert host_payloads[0]["device_id"] == "existing-host-device"
    assert host_payloads[0]["action"] == "execute"
    assert host_payloads[0]["action_args"] == {"amount": 3}


def test_runtime_composition_modules_have_no_device_family_dependency() -> None:
    composition_module = _composition_api()
    module_paths = (
        Path(composition_module.__file__),
        Path(communication_module.__file__),
        Path(ws_module.__file__),
    )

    for module_path in module_paths:
        source = module_path.read_text(encoding="utf-8").lower()
        assert "ptlc" not in source
