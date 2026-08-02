"""A1 independent-review regressions at package and Registry seams."""

from __future__ import annotations

import copy
import importlib
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from tests.package_manager.test_a1_canonical_action_catalog import (
    RESOURCE_TEMPLATE_UUID,
    _register,
    _write_package,
)
from unilabos.package_manager import (
    PackageCompileError,
    WorkspaceSource,
    compile_package_source,
)


def _typed_ros_package(root: Path) -> Any:
    _write_package(root)
    device_path = root / "a1_contract_lab" / "device.py"
    source = device_path.read_text(encoding="utf-8")
    source = source.replace(
        "from a1_contract_lab.resources import plate_96",
        "from a1_contract_lab.resources import plate_96",
    ).replace(
        '@action(description="转移样品",',
        '@action(description="转移样品", action_type="test_transport:ReviewAction",',
    )
    device_path.write_text(source, encoding="utf-8")
    return compile_package_source(WorkspaceSource(root))


def _message_type(fields: Mapping[str, str]) -> type[Any]:
    class Message:
        @staticmethod
        def get_fields_and_field_types() -> dict[str, str]:
            return dict(fields)

    return Message


def _ros_action(
    *,
    goal_override: tuple[str, str] | None = None,
    result_override: tuple[str, str] | None = None,
) -> type[Any]:
    goal = {
        "sample": "string",
        "volume": "double",
        "mode": "string",
        "note": "string",
        "batches": "sequence<int32>",
        "payload": "string",
        "unilabos_param": "string",
    }
    result = {
        "sample": "string",
        "report": "string",
        "unilabos_samples": "sequence<string>",
    }
    if goal_override is not None:
        goal[goal_override[0]] = goal_override[1]
    if result_override is not None:
        result[result_override[0]] = result_override[1]

    class ReviewAction:
        Goal = _message_type(goal)
        Feedback = _message_type({"progress": "sequence<double>"})
        Result = _message_type(result)

    return ReviewAction


@pytest.mark.parametrize(
    ("section", "field", "ros_type"),
    [
        ("goal", "volume", "sequence<double>"),
        ("goal", "batches", "int32"),
        ("goal", "note", "sequence<string>"),
        ("goal", "sample", "sequence<string>"),
        ("result", "report", "sequence<string>"),
        ("result", "sample", "sequence<string>"),
    ],
)
def test_ros_goal_and_result_types_must_encode_the_canonical_business_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    section: str,
    field: str,
    ros_type: str,
) -> None:
    """Matching ROS field names cannot hide scalar/container/slot mismatches."""

    catalog = _typed_ros_package(tmp_path)
    action_type = _ros_action(
        goal_override=(field, ros_type) if section == "goal" else None,
        result_override=(field, ros_type) if section == "result" else None,
    )
    registry_module = importlib.import_module("unilabos.registry.registry")
    monkeypatch.setattr(
        registry_module,
        "resolve_type_object",
        lambda _name: action_type,
    )

    with pytest.raises(ValueError, match=r"ROS (goal|result).*(冲突|不兼容)"):
        _register(catalog, monkeypatch)


def test_ros_feedback_remains_transport_only_when_business_types_are_compatible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _typed_ros_package(tmp_path)
    action_type = _ros_action()
    registry_module = importlib.import_module("unilabos.registry.registry")
    monkeypatch.setattr(
        registry_module,
        "resolve_type_object",
        lambda _name: action_type,
    )

    registry = _register(catalog, monkeypatch)
    transfer = registry.device_type_registry["community.a1_contract_lab.pump"]["class"][
        "action_value_mappings"
    ]["transfer"]

    assert transfer["feedback"] == {"progress": "progress"}


def _write_conflicting_legacy_handle(root: Path, conflict: str) -> None:
    _write_package(root, legacy_handles="equivalent")
    device_path = root / "a1_contract_lab" / "device.py"
    source = device_path.read_text(encoding="utf-8")
    marker = 'ActionInputHandle(key="sample", data_type="ResourceSlot"'
    replacement = {
        "io_type": (
            'ActionInputHandle(io_type="source", key="sample", data_type="ResourceSlot"'
        ),
        "required": (
            'ActionInputHandle(required=False, key="sample", data_type="ResourceSlot"'
        ),
    }[conflict]
    assert marker in source
    device_path.write_text(source.replace(marker, replacement, 1), encoding="utf-8")


def _scan_to_registry_snapshot(root: Path) -> Mapping[str, Any]:
    scanner = importlib.import_module("unilabos.registry.ast_registry_scanner")
    with ThreadPoolExecutor(max_workers=1) as executor:
        scanned = scanner.scan_directory(
            root / "a1_contract_lab",
            python_path=root,
            executor=executor,
            cache={"version": 8, "files": {}},
        )
    device = scanned["devices"]["pump"]
    return MappingProxyType(
        {
            "community.a1_contract_lab.pump": {
                "source_fqid": "community.a1_contract_lab.pump",
                "display_name": "A1 泵",
                "class": {
                    "module": device["module"],
                    "action_value_mappings": copy.deepcopy(device["actions"]),
                },
            }
        }
    )


@pytest.mark.parametrize("conflict", ["io_type", "required"])
def test_explicit_legacy_handle_contract_conflicts_fail_package_and_registry_projection(
    tmp_path: Path,
    conflict: str,
) -> None:
    _write_conflicting_legacy_handle(tmp_path, conflict)

    with pytest.raises(PackageCompileError) as package_failure:
        compile_package_source(WorkspaceSource(tmp_path))
    assert {item.code for item in package_failure.value.diagnostics} == {
        "action_handle_contract_conflict"
    }

    snapshot = _scan_to_registry_snapshot(tmp_path)
    adapter_module = importlib.import_module("unilabos.registry.catalog_consumer")
    with pytest.raises(
        adapter_module.RegistryTemplateProjectionError
    ) as projection_failure:
        adapter_module.workflow_template_imports_from_registry_snapshot(
            snapshot,
            authority_id="os-local",
            resource_template_identity_resolver=lambda _identity: (
                RESOURCE_TEMPLATE_UUID
            ),
        )
    assert projection_failure.value.code == "action_handle_contract_conflict"
    assert projection_failure.value.path.endswith("/actions/transfer/handles")
