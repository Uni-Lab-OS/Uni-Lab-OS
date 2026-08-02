"""External package startup discovers and loads its embedded registry."""

from pathlib import Path

from unilabos.package_manager.legacy import discover_registry_paths_from_project
from unilabos.registry.registry import Registry

PKG = Path(__file__).parent / "fixtures" / "external_variant_pkg"


def test_external_package_discover_and_setup_load(monkeypatch):
    paths = discover_registry_paths_from_project(PKG)
    assert paths == [(PKG / "unilabos_registry").resolve()]

    reg = Registry()
    monkeypatch.setattr(reg, "_setup_called", False)
    monkeypatch.setattr(reg, "_startup_executor", None)
    monkeypatch.setattr(reg, "registry_paths", [])
    monkeypatch.setattr(reg, "device_type_registry", {})
    monkeypatch.setattr(reg, "resource_type_registry", {})
    monkeypatch.setattr(reg, "_run_ast_scan", lambda *args, **kwargs: None)
    monkeypatch.setattr(reg, "_setup_host_node", lambda: None)

    reg.setup(devices_dirs=[PKG], external_only=True)

    a = reg.device_type_registry["vendor.lh.model_a"]
    b = reg.device_type_registry["vendor.lh.model_b"]
    assert a["class"]["module"].endswith(":JsonConfiguredDevice")
    assert b["class"]["module"].endswith(":JsonConfiguredDevice")
    assert "init" not in a["class"]
    assert "init" not in b["class"]
    assert a["implementation"]["variant"] == "model_a"
    assert b["implementation"]["variant"] == "model_b"
    assert a["init_param_enforce"] == {
        "backend_type": "mock",
        "backend_params": {"port": 4008},
        "deck_name": "model-a-deck",
        "channels": 8,
    }
    assert b["init_param_enforce"] == {
        "backend_type": "mock",
        "backend_params": {"port": 4096},
        "deck_name": "model-b-deck",
        "channels": 96,
    }
    assert "setup" in a["class"]["action_value_mappings"]
    assert "initialized" in b["class"]["status_types"]
    assert paths[0] in reg.registry_paths
