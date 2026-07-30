"""Registry setup loads embedded registries from ``devices_dirs``."""

from pathlib import Path

from unilabos.registry.registry import Registry


def test_setup_loads_external_registry_from_devices_dir(monkeypatch):
    project_root = Path(__file__).parent / "fixtures" / "external_variant_pkg"
    registry_root = (project_root / "unilabos_registry").resolve()
    loaded_paths = []

    reg = Registry()
    monkeypatch.setattr(reg, "_setup_called", False)
    monkeypatch.setattr(reg, "_startup_executor", None)
    monkeypatch.setattr(reg, "registry_paths", [])
    monkeypatch.setattr(reg, "_run_ast_scan", lambda *args, **kwargs: None)
    monkeypatch.setattr(reg, "_load_community_device_registries", lambda _dirs: None)
    monkeypatch.setattr(reg, "_setup_host_node", lambda: None)
    monkeypatch.setattr(
        reg,
        "load_device_types",
        lambda path, complete_registry=False: loaded_paths.append(Path(path).resolve()),
    )
    monkeypatch.setattr(reg, "load_resource_types", lambda *args, **kwargs: None)

    reg.setup(devices_dirs=[project_root], external_only=True)

    assert loaded_paths == [registry_root]
    assert reg.registry_paths == [registry_root]
