"""OS startup must compose configured generic Profiles into WebSocket runtime."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

import unilabos.app.communication as communication_module
import unilabos.app.main as main_module
import unilabos.registry.registry as registry_module
import unilabos.utils.log as log_module
from unilabos.app.communication import CommunicationClientFactory
from unilabos.config.config import BasicConfig


def test_parse_args_accepts_repeatable_generic_profile_paths() -> None:
    parser = main_module.parse_args()

    args = parser.parse_args(
        [
            "--profile",
            "/profiles/station-a.yaml",
            "--profile",
            "/profiles/station-b.yaml",
        ]
    )

    assert args.profile == [
        "/profiles/station-a.yaml",
        "/profiles/station-b.yaml",
    ]


def test_main_copies_profile_paths_into_basic_runtime_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class StopBootstrap(RuntimeError):
        pass

    profile_a = str(tmp_path / "station-a.yaml")
    profile_b = str(tmp_path / "station-b.yaml")
    config_path = tmp_path / "local_config.py"
    config_path.write_text("# hermetic startup config\n", encoding="utf-8")
    monkeypatch.setattr(BasicConfig, "runtime_profile_paths", [], raising=False)
    monkeypatch.setattr(main_module, "print_status", lambda *_args: None)
    monkeypatch.setattr(main_module, "print_unilab_banner", lambda *_args: None)
    monkeypatch.setattr(log_module, "configure_logger", lambda **_kwargs: None)
    monkeypatch.setattr(log_module, "configure_comm_logger", lambda **_kwargs: None)
    monkeypatch.setattr(main_module.platform, "node", lambda: "test-host")

    def stop_before_device_bootstrap(**_kwargs: object) -> None:
        raise StopBootstrap

    monkeypatch.setattr(registry_module, "build_registry", stop_before_device_bootstrap)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "unilab",
            "--skip_env_check",
            "--check_mode",
            "--config",
            str(config_path),
            "--profile",
            profile_a,
            "--profile",
            profile_b,
        ],
    )

    with pytest.raises(StopBootstrap):
        main_module.main()

    assert BasicConfig.runtime_profile_paths == [profile_a, profile_b]


def test_websocket_factory_builds_runtime_drivers_from_configured_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_paths = ["/profiles/station-a.yaml", "/profiles/station-b.yaml"]
    resolver = object()
    driver_catalog = {"generic-driver": object()}
    loaded_profiles = {"station-profile": object()}
    runtime_drivers = {"station-a": object()}
    calls: list[tuple[str, object]] = []
    captured_websocket_kwargs: dict[str, Any] = {}

    monkeypatch.setattr(
        BasicConfig,
        "runtime_profile_paths",
        profile_paths,
        raising=False,
    )
    monkeypatch.setattr(
        BasicConfig,
        "runtime_connection_resolver",
        resolver,
        raising=False,
    )

    def discover_driver_catalog() -> dict[str, object]:
        calls.append(("discover", None))
        return driver_catalog

    def load_profiles(
        paths: list[str],
        *,
        driver_catalog: dict[str, object],
    ) -> dict[str, object]:
        calls.append(("load", (paths, driver_catalog)))
        return loaded_profiles

    def build_runtime_drivers(
        profiles: dict[str, object],
        catalog: dict[str, object],
        connection_resolver: object,
    ) -> dict[str, object]:
        calls.append(
            (
                "build",
                (profiles, catalog, connection_resolver),
            )
        )
        return runtime_drivers

    class RecordingWebSocketClient:
        def __init__(self, **kwargs: object) -> None:
            captured_websocket_kwargs.update(kwargs)

    monkeypatch.setattr(
        communication_module,
        "discover_driver_catalog",
        discover_driver_catalog,
        raising=False,
    )
    monkeypatch.setattr(
        communication_module,
        "load_profiles",
        load_profiles,
        raising=False,
    )
    monkeypatch.setattr(
        communication_module,
        "build_runtime_drivers",
        build_runtime_drivers,
        raising=False,
    )
    monkeypatch.setattr(
        "unilabos.app.ws_client.WebSocketClient",
        RecordingWebSocketClient,
    )

    client = CommunicationClientFactory._create_websocket_client()

    assert isinstance(client, RecordingWebSocketClient)
    assert calls == [
        ("discover", None),
        ("load", (profile_paths, driver_catalog)),
        (
            "build",
            (loaded_profiles, driver_catalog, resolver),
        ),
    ]
    assert captured_websocket_kwargs == {"runtime_drivers": runtime_drivers}


def test_websocket_factory_rejects_profiles_without_connection_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forbidden_calls: list[str] = []
    monkeypatch.setattr(
        BasicConfig,
        "runtime_profile_paths",
        ["/profiles/station-a.yaml"],
        raising=False,
    )
    monkeypatch.setattr(
        BasicConfig,
        "runtime_connection_resolver",
        None,
        raising=False,
    )
    for name in (
        "discover_driver_catalog",
        "load_profiles",
        "build_runtime_drivers",
    ):
        monkeypatch.setattr(
            communication_module,
            name,
            lambda *args, _name=name, **kwargs: forbidden_calls.append(_name),
            raising=False,
        )

    with pytest.raises(ValueError, match="connection_resolver"):
        CommunicationClientFactory._create_websocket_client()

    assert forbidden_calls == []


def test_websocket_factory_without_profiles_preserves_zero_argument_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forbidden_calls: list[str] = []
    websocket_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        BasicConfig,
        "runtime_profile_paths",
        [],
        raising=False,
    )
    monkeypatch.setattr(
        BasicConfig,
        "runtime_connection_resolver",
        None,
        raising=False,
    )
    for name in (
        "discover_driver_catalog",
        "load_profiles",
        "build_runtime_drivers",
    ):
        monkeypatch.setattr(
            communication_module,
            name,
            lambda *args, _name=name, **kwargs: forbidden_calls.append(_name),
            raising=False,
        )

    class RecordingWebSocketClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            websocket_calls.append((args, kwargs))

    monkeypatch.setattr(
        "unilabos.app.ws_client.WebSocketClient",
        RecordingWebSocketClient,
    )

    client = CommunicationClientFactory._create_websocket_client()

    assert isinstance(client, RecordingWebSocketClient)
    assert forbidden_calls == []
    assert websocket_calls == [((), {})]
