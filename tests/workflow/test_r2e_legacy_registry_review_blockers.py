"""Legacy Registry transport markers must not weaken atomic A1 publication."""

from __future__ import annotations

import copy
import importlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest

from unilabos.package_manager import (
    PackageCompileError,
    WorkspaceSource,
    compile_package_source,
)
from unilabos.package_manager.consumers import register_package_catalog
from unilabos.registry.catalog_consumer import (
    RegistryTemplateProjectionError,
    workflow_template_imports_from_registry_snapshot,
)
from unilabos.registry.registry import Registry

_FIXTURE_WORKSPACE = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "r2e_szlab_workspace"
)


def _write_registry_package(root: Path, method_decorator: str) -> None:
    package = root / "legacy_transport_lab"
    package.mkdir()
    (root / "pyproject.toml").write_text(
        """[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "legacy-transport-lab"
version = "0.1.0"
requires-python = ">=3.11"
""",
        encoding="utf-8",
    )
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "device.py").write_text(
        f'''from unilabos.registry.decorators import action, device, legacy_action

@device(id="legacy_transport", category=["test"])
class LegacyTransport:
    @{method_decorator}
    def transport(self, payload: tuple[int, int]) -> None:
        pass
''',
        encoding="utf-8",
    )


def test_legacy_action_keeps_runtime_record_without_calling_canonical_parser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_registry_package(tmp_path, "legacy_action()")
    contract_module = importlib.import_module(
        "unilabos.registry.action_contract_schema"
    )
    parser_calls: list[str] = []

    def forbidden_parser(*_args: Any, **_kwargs: Any) -> Any:
        parser_calls.append("called")
        pytest.fail("@legacy_action must not invoke the canonical Action parser")

    monkeypatch.setattr(
        contract_module,
        "parse_action_contract",
        forbidden_parser,
    )
    scanner = importlib.import_module("unilabos.registry.ast_registry_scanner")
    with ThreadPoolExecutor(max_workers=1) as executor:
        scanned = scanner.scan_directory(
            tmp_path / "legacy_transport_lab",
            python_path=tmp_path,
            executor=executor,
            cache={"version": 8, "files": {}},
        )

    action = scanned["devices"]["legacy_transport"]["actions"]["transport"]
    assert parser_calls == []
    assert "contract_diagnostic" not in action
    scanner_schema = action.get("schema")
    if scanner_schema is not None:
        assert isinstance(scanner_schema, dict)
        assert "x-unilabos-action-contract" not in scanner_schema

    registry = Registry()
    entry = registry._build_device_entry_from_ast(
        "legacy_transport",
        scanned["devices"]["legacy_transport"],
        allow_definition_imports=False,
    )
    runtime_action = entry["class"]["action_value_mappings"]["transport"]
    assert "contract_diagnostic" not in runtime_action
    runtime_schema = runtime_action.get("schema")
    if runtime_schema is not None:
        assert isinstance(runtime_schema, dict)
        assert "x-unilabos-action-contract" not in runtime_schema
    decorators = importlib.import_module("unilabos.registry.decorators")
    assert callable(getattr(decorators, "legacy_action", None))


def test_invalid_typed_action_still_fails_complete_package_compile(
    tmp_path: Path,
) -> None:
    _write_registry_package(tmp_path, "action()")

    with pytest.raises(PackageCompileError) as caught:
        compile_package_source(WorkspaceSource(tmp_path))

    assert any(
        diagnostic.severity == "error"
        and "transport" in (diagnostic.path or "")
        for diagnostic in caught.value.diagnostics
    )


def _identity_uuid(identity: str) -> str:
    return str(uuid5(NAMESPACE_URL, identity))


def _complete_registry_snapshot() -> dict[str, Any]:
    catalog = compile_package_source(WorkspaceSource(_FIXTURE_WORKSPACE))
    registry = Registry()
    previous_devices = registry.device_type_registry
    previous_resources = registry.resource_type_registry
    try:
        registry.device_type_registry = {}
        registry.resource_type_registry = {}
        register_package_catalog(registry, catalog)
        snapshot = copy.deepcopy(registry.device_type_registry)
    finally:
        registry.device_type_registry = previous_devices
        registry.resource_type_registry = previous_resources
    snapshot["host_node"] = {
        "workflow_template_projection": False,
        "display_name": "Host Node",
        "class": {
            "module": "unilabos.ros.nodes.presets.host_node:HostNode",
            "action_value_mappings": {},
        },
    }
    for entry in snapshot.values():
        entry["workflow_template_projection"] = False
    return snapshot


def test_complete_registry_projection_keeps_host_owner_and_package_typed_actions(
) -> None:
    snapshot = MappingProxyType(_complete_registry_snapshot())

    imports = workflow_template_imports_from_registry_snapshot(
        snapshot,
        authority_id="os-local",
        resource_template_identity_resolver=_identity_uuid,
    )

    assert {item.template["name"] for item in imports} >= {
        "material_source",
        "prepare",
        "finish",
    }


def test_source_level_projection_flag_cannot_hide_invalid_typed_action() -> None:
    snapshot = _complete_registry_snapshot()
    snapshot["invalid_builtin"] = {
        "workflow_template_projection": False,
        "class": {
            "module": "legacy.invalid:Device",
            "action_value_mappings": {
                "broken": {
                    "contract_diagnostic": {
                        "code": "invalid_annotation",
                        "path": "/actions/broken/goal/value",
                        "message": "unsupported annotation",
                    }
                }
            },
        },
    }

    with pytest.raises(RegistryTemplateProjectionError) as caught:
        workflow_template_imports_from_registry_snapshot(
            MappingProxyType(snapshot),
            authority_id="os-local",
            resource_template_identity_resolver=_identity_uuid,
        )

    assert caught.value.code == "invalid_annotation"
    assert caught.value.path == "/devices/invalid_builtin/actions/broken/goal/value"
