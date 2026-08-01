"""A1 PackageCatalog 到 Registry canonical Action record 的公共合同。"""

from __future__ import annotations

import copy
import importlib
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Any

import pytest

from unilabos.package_manager import (
    CachedArchiveSource,
    InstalledDistributionSource,
    PackageCompileError,
    WorkspaceSource,
    compile_package_source,
)
from unilabos.package_manager.distribution import build_workspace_wheel
from unilabos.registry.registry import Registry

RESOURCE_TEMPLATE_UUID = "10000000-0000-4000-8000-000000000001"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_package(
    root: Path,
    *,
    legacy_handles: str = "none",
    legacy_default: str = "none",
) -> None:
    _write(
        root / "pyproject.toml",
        """
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "a1-contract-lab"
version = "1.0.0"

[tool.setuptools.packages.find]
include = ["a1_contract_lab*"]
""".strip(),
    )
    _write(root / "a1_contract_lab" / "__init__.py", "")
    _write(
        root / "a1_contract_lab" / "resources.py",
        """
from unilabos.registry.decorators import resource

@resource(id="plate_96", category=["labware"])
def plate_96():
    return None
""".strip(),
    )
    if legacy_handles not in {"none", "equivalent", "conflict"}:
        raise ValueError("unknown legacy_handles fixture")
    if legacy_default not in {"none", "equivalent", "conflict"}:
        raise ValueError("unknown legacy_default fixture")
    first_key = "wrong_sample" if legacy_handles == "conflict" else "sample"
    handles_clause = ""
    if legacy_handles != "none":
        inputs = [
            (first_key, "ResourceSlot", "sample"),
            ("volume", "number", "volume"),
            ("mode", "string", "mode"),
            ("note", "string", "note"),
            ("batches", "array", "batches"),
            ("payload", "object", "payload"),
        ]
        outputs = [
            ("sample", "ResourceSlot", "sample"),
            ("report", "string", "report"),
        ]
        rendered = [
            "ActionInputHandle("
            f'key="{key}", data_type="{value_type}", label="{key}", '
            f'data_source="goal", data_key="{data_key}"),'
            for key, value_type, data_key in inputs
        ]
        rendered.extend(
            "ActionOutputHandle("
            f'key="{key}", data_type="{value_type}", label="{key}", '
            f'data_source="result", data_key="{data_key}"),'
            for key, value_type, data_key in outputs
        )
        handles_clause = (
            "\n        handles=[\n            "
            + "\n            ".join(rendered)
            + "\n        ],"
        )
    default_clause = {
        "none": "",
        "equivalent": ', goal_default={"level": 2}',
        "conflict": ', goal_default={"level": 3}',
    }[legacy_default]
    _write(
        root / "a1_contract_lab" / "device.py",
        f'''
from dataclasses import dataclass
from typing import Annotated, Any, Dict, Literal, TypedDict

from pydantic import Field

from a1_contract_lab.resources import plate_96
from unilabos.registry.annotations import AllowedResourceTemplates, JSONValue
from unilabos.registry.decorators import (
    ActionInputHandle,
    ActionOutputHandle,
    action,
    device,
)
from unilabos.registry.placeholder_type import ResourceSlot


class TransferResult(TypedDict):
    sample: Annotated[
        ResourceSlot,
        AllowedResourceTemplates(plate_96),
        Field(title="处理后样品"),
    ]
    report: str


@dataclass(frozen=True)
class MeasureResult:
    accepted: bool
    reading: float | None


@device(id="pump", category=["test"], displayname="A1 泵")
class Pump:
    @action(description="转移样品",{handles_clause}
    )
    def transfer(
        self,
        sample: Annotated[
            ResourceSlot,
            AllowedResourceTemplates(plate_96),
        ],
        volume: Annotated[float, Field(title="体积", ge=0)] = 1.25,
        /,
        mode: Literal["safe", "fast"] = "safe",
        *,
        note: str | None = None,
        batches: list[int] = [],
        payload: dict[str, JSONValue] = {{}},
    ) -> TransferResult:
        raise NotImplementedError

    @action(description="dataclass result")
    async def measure(self, channel: int) -> MeasureResult:
        raise NotImplementedError

    @action(description="inline result")
    def inspect(self, strict: bool = True) -> {{"code": str, "count": int}}:
        raise NotImplementedError

    @action(description="closed empty result")
    def reset(self) -> None:
        return None

    @action(description="legacy default assertion"{default_clause})
    def defaulted(self, level: int = 2) -> None:
        return None

    @action(description="implicit resource pass-through")
    def consume(self, sample: ResourceSlot) -> None:
        return None

    @action(description="opaque bare dict result")
    def raw_bare(self, options: dict[str, JSONValue]) -> dict:
        return options

    @action(description="opaque PEP 585 dict result")
    def raw_pep585(self, options: dict[str, JSONValue]) -> dict[str, Any]:
        return options

    @action(description="opaque typing Dict result")
    def raw_typing(self, options: dict[str, JSONValue]) -> Dict[str, Any]:
        return options

    def health(self) -> str:
        return "ok"
'''.strip(),
    )


def _action(catalog: Any, name: str) -> Mapping[str, Any]:
    device = next(
        record
        for record in catalog.definitions.devices
        if record.fqid == "community.a1_contract_lab.pump"
    )
    actions = device.details["actions"]
    return next(item for item in actions if item["name"] == name)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _register(catalog: Any, monkeypatch: pytest.MonkeyPatch) -> Registry:
    consumer = importlib.import_module("unilabos.registry.catalog_consumer")
    registry = Registry()
    monkeypatch.setattr(registry, "device_type_registry", {})
    monkeypatch.setattr(registry, "resource_type_registry", {})
    consumer.register_package_catalog(registry, catalog)
    return registry


def _require_public_member(module_name: str, member: str) -> Any:
    module: ModuleType = importlib.import_module(module_name)
    if not hasattr(module, member):
        pytest.fail(
            f"A1 缺少公共 Interface: {module_name}.{member}",
            pytrace=False,
        )
    return getattr(module, member)


def test_workspace_wheel_cache_and_installed_sources_share_one_canonical_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """物理来源不能改变 Action schema、order 或 ResourceTemplate symbol。"""

    workspace = tmp_path / "workspace"
    _write_package(workspace)
    from_workspace = compile_package_source(WorkspaceSource(workspace))
    artifact = build_workspace_wheel(workspace, tmp_path / "dist")
    from_cache = compile_package_source(
        CachedArchiveSource(artifact.wheel, artifact.artifact_digest)
    )

    installed_root = tmp_path / "installed"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(installed_root),
            str(artifact.wheel),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    monkeypatch.syspath_prepend(str(installed_root))
    from_installed = compile_package_source(
        InstalledDistributionSource("a1-contract-lab")
    )

    schemas = [
        _plain(_action(catalog, "transfer")["schema"])
        for catalog in (from_workspace, from_cache, from_installed)
    ]
    assert schemas[0] == schemas[1] == schemas[2]

    schema = schemas[0]
    extension = schema["x-unilabos-action-contract"]
    assert extension == {
        "version": 1,
        "input_order": [
            "sample",
            "volume",
            "mode",
            "note",
            "batches",
            "payload",
        ],
        "output_order": ["sample", "report"],
        "resource_template_symbols": {
            "goal": {
                "sample": ["a1_contract_lab.resources:plate_96"],
            },
            "result": {
                "sample": ["a1_contract_lab.resources:plate_96"],
            },
        },
    }
    goal = schema["properties"]["goal"]
    assert goal["required"] == ["sample"]
    assert goal["properties"]["volume"] == {
        "type": "number",
        "minimum": 0,
        "default": 1.25,
        "title": "体积",
    }
    assert goal["properties"]["mode"] == {
        "type": "string",
        "enum": ["safe", "fast"],
        "default": "safe",
    }
    assert goal["properties"]["note"] == {
        "anyOf": [{"type": "string"}, {"type": "null"}],
        "default": None,
    }
    assert goal["properties"]["batches"] == {
        "type": "array",
        "items": {"type": "integer"},
        "default": [],
    }
    assert goal["properties"]["payload"] == {
        "type": "object",
        "additionalProperties": True,
        "default": {},
    }


def test_named_result_forms_none_and_opaque_dict_use_one_schema_envelope(
    tmp_path: Path,
) -> None:
    _write_package(tmp_path)
    catalog = compile_package_source(WorkspaceSource(tmp_path))

    expected = {
        "consume": [],
        "defaulted": [],
        "measure": ["accepted", "reading"],
        "inspect": ["code", "count"],
        "reset": [],
    }
    for action_name, output_order in expected.items():
        action = _plain(_action(catalog, action_name))
        schema = action["schema"]
        assert schema["x-unilabos-action-contract"]["output_order"] == output_order
        result = schema["properties"]["result"]
        assert list(result["properties"]) == output_order
        assert result["required"] == output_order
        assert result["additionalProperties"] is False
        assert "input_contract" not in action
        assert "output_contract" not in action
        assert "action_contract" not in action

    reading = _plain(_action(catalog, "measure"))["schema"]["properties"]["result"][
        "properties"
    ]["reading"]
    assert reading == {
        "anyOf": [{"type": "number"}, {"type": "null"}],
    }


@pytest.mark.parametrize(
    "action_name",
    ["raw_bare", "raw_pep585", "raw_typing"],
)
def test_opaque_dict_result_is_a_legal_closed_empty_result(
    tmp_path: Path,
    action_name: str,
) -> None:
    """允许 opaque mapping，但绝不能猜测 named output。"""

    _write_package(tmp_path)
    catalog = compile_package_source(WorkspaceSource(tmp_path))

    action = _plain(_action(catalog, action_name))
    contract = action["schema"]
    assert contract["x-unilabos-action-contract"]["output_order"] == []
    assert contract["properties"]["result"] == {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }


def test_package_catalog_and_registry_record_share_the_exact_canonical_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_package(tmp_path)
    catalog = compile_package_source(WorkspaceSource(tmp_path))
    registry = _register(catalog, monkeypatch)

    action = registry.device_type_registry["community.a1_contract_lab.pump"]["class"][
        "action_value_mappings"
    ]["transfer"]
    catalog_action = _plain(_action(catalog, "transfer"))

    assert action["schema"] == catalog_action["schema"]
    assert action["goal_default"] == {
        "volume": 1.25,
        "mode": "safe",
        "note": None,
        "batches": [],
        "payload": {},
    }
    assert action["goal"] == {
        name: name
        for name in catalog_action["schema"]["x-unilabos-action-contract"][
            "input_order"
        ]
    }
    assert "input_contract" not in action
    assert "output_contract" not in action


def test_package_catalog_calls_the_unique_public_action_parser_once_per_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """scanner 不能在唯一 parser 之外重写 annotation 字符串。"""

    _write_package(tmp_path)
    contract_module = importlib.import_module(
        "unilabos.registry.action_contract_schema"
    )
    original = contract_module.parse_action_contract
    calls: list[str] = []

    def recording_parser(module: Any, action: Any, *, module_name: str) -> Any:
        calls.append(f"{module_name}:{action.name}")
        return original(module, action, module_name=module_name)

    monkeypatch.setattr(contract_module, "parse_action_contract", recording_parser)

    catalog = compile_package_source(WorkspaceSource(tmp_path))

    assert sorted(calls) == [
        "a1_contract_lab.device:consume",
        "a1_contract_lab.device:defaulted",
        "a1_contract_lab.device:inspect",
        "a1_contract_lab.device:measure",
        "a1_contract_lab.device:raw_bare",
        "a1_contract_lab.device:raw_pep585",
        "a1_contract_lab.device:raw_typing",
        "a1_contract_lab.device:reset",
        "a1_contract_lab.device:transfer",
    ]
    assert all(not item.endswith(":health") for item in calls)
    assert len(catalog.definitions.devices) == 1


def test_legacy_scanner_uses_the_same_parser_and_does_not_type_auto_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_package(tmp_path)
    contract_module = importlib.import_module(
        "unilabos.registry.action_contract_schema"
    )
    original = contract_module.parse_action_contract
    calls: list[str] = []

    def recording_parser(module: Any, action: Any, *, module_name: str) -> Any:
        calls.append(f"{module_name}:{action.name}")
        return original(module, action, module_name=module_name)

    monkeypatch.setattr(contract_module, "parse_action_contract", recording_parser)
    scanner = importlib.import_module("unilabos.registry.ast_registry_scanner")
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as executor:
        scanned = scanner.scan_directory(
            tmp_path / "a1_contract_lab",
            python_path=tmp_path,
            executor=executor,
            cache={"version": 8, "files": {}},
        )

    assert "pump" in scanned["devices"]
    assert sorted(calls) == [
        "a1_contract_lab.device:consume",
        "a1_contract_lab.device:defaulted",
        "a1_contract_lab.device:inspect",
        "a1_contract_lab.device:measure",
        "a1_contract_lab.device:raw_bare",
        "a1_contract_lab.device:raw_pep585",
        "a1_contract_lab.device:raw_typing",
        "a1_contract_lab.device:reset",
        "a1_contract_lab.device:transfer",
    ]
    class_meta = scanned["devices"]["pump"]
    explicit = class_meta["actions"]
    assert "x-unilabos-action-contract" in explicit["transfer"]["schema"]
    assert "health" in class_meta["auto_methods"]
    assert "schema" not in class_meta["auto_methods"]["health"]


def test_legacy_handle_conflict_fails_the_whole_catalog_compile(
    tmp_path: Path,
) -> None:
    _write_package(tmp_path, legacy_handles="conflict")

    with pytest.raises(PackageCompileError) as caught:
        compile_package_source(WorkspaceSource(tmp_path))

    assert {item.code for item in caught.value.diagnostics} == {
        "action_handle_contract_conflict"
    }
    assert all(
        "/actions/transfer/handles" in (item.path or "")
        for item in caught.value.diagnostics
    )


def test_complete_equivalent_legacy_handles_are_only_a_compatibility_assertion(
    tmp_path: Path,
) -> None:
    _write_package(tmp_path, legacy_handles="equivalent")

    catalog = compile_package_source(WorkspaceSource(tmp_path))
    transfer = _plain(_action(catalog, "transfer"))

    assert transfer["schema"]["x-unilabos-action-contract"]["input_order"] == [
        "sample",
        "volume",
        "mode",
        "note",
        "batches",
        "payload",
    ]
    assert transfer["schema"]["x-unilabos-action-contract"]["output_order"] == [
        "sample",
        "report",
    ]
    assert "input_contract" not in transfer
    assert "output_contract" not in transfer


def test_equivalent_legacy_goal_default_is_only_a_compatibility_assertion(
    tmp_path: Path,
) -> None:
    _write_package(tmp_path, legacy_default="equivalent")

    catalog = compile_package_source(WorkspaceSource(tmp_path))
    defaulted = _plain(_action(catalog, "defaulted"))

    assert defaulted["schema"]["properties"]["goal"]["properties"]["level"] == {
        "type": "integer",
        "default": 2,
    }
    assert defaulted["goal_default"] == {"level": 2}


def test_conflicting_legacy_goal_default_fails_the_whole_catalog_compile(
    tmp_path: Path,
) -> None:
    _write_package(tmp_path, legacy_default="conflict")

    with pytest.raises(PackageCompileError) as caught:
        compile_package_source(WorkspaceSource(tmp_path))

    assert {item.code for item in caught.value.diagnostics} == {
        "action_default_contract_conflict"
    }
    assert all(
        (item.path or "") == "/actions/defaulted/goal_default/level"
        for item in caught.value.diagnostics
    )


def test_legacy_registry_diagnostic_blocks_the_complete_template_projection(
    tmp_path: Path,
) -> None:
    _write_package(tmp_path, legacy_handles="conflict")
    scanner = importlib.import_module("unilabos.registry.ast_registry_scanner")
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as executor:
        scanned = scanner.scan_directory(
            tmp_path / "a1_contract_lab",
            python_path=tmp_path,
            executor=executor,
            cache={"version": 8, "files": {}},
        )
    registry = Registry()
    registry.device_type_registry = {
        "pump": registry._build_device_entry_from_ast(
            "pump",
            scanned["devices"]["pump"],
            allow_definition_imports=False,
        )
    }
    adapter = _require_public_member(
        "unilabos.registry.catalog_consumer",
        "workflow_template_imports_from_registry_snapshot",
    )
    projection_error = _require_public_member(
        "unilabos.registry.catalog_consumer",
        "RegistryTemplateProjectionError",
    )

    with pytest.raises(projection_error) as caught:
        adapter(
            MappingProxyType(copy.deepcopy(registry.device_type_registry)),
            authority_id="os-local",
            resource_template_identity_resolver=lambda _identity: (
                RESOURCE_TEMPLATE_UUID
            ),
        )

    assert caught.value.code == "action_handle_contract_conflict"
    assert caught.value.path.startswith("/devices/pump/actions/transfer/handles")


def test_malformed_resource_symbol_metadata_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_package(tmp_path)
    registry = _register(
        compile_package_source(WorkspaceSource(tmp_path)),
        monkeypatch,
    )
    broken = copy.deepcopy(registry.device_type_registry)
    extension = broken["community.a1_contract_lab.pump"]["class"][
        "action_value_mappings"
    ]["transfer"]["schema"]["x-unilabos-action-contract"]
    extension["resource_template_symbols"]["goal"]["sample"] = "not-a-list"
    adapter = _require_public_member(
        "unilabos.registry.catalog_consumer",
        "workflow_template_imports_from_registry_snapshot",
    )
    projection_error = _require_public_member(
        "unilabos.registry.catalog_consumer",
        "RegistryTemplateProjectionError",
    )

    with pytest.raises(projection_error) as caught:
        adapter(
            MappingProxyType(broken),
            authority_id="os-local",
            resource_template_identity_resolver=lambda _identity: (
                RESOURCE_TEMPLATE_UUID
            ),
        )

    assert caught.value.code == "invalid_action_contract"
    assert "resource_template_symbols/goal/sample" in caught.value.path


def test_registry_adapter_projects_site_selector_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_package(tmp_path)
    registry = _register(
        compile_package_source(WorkspaceSource(tmp_path)),
        monkeypatch,
    )
    snapshot = copy.deepcopy(registry.device_type_registry)
    action = snapshot["community.a1_contract_lab.pump"]["class"][
        "action_value_mappings"
    ]["transfer"]
    action["schema"]["properties"]["goal"]["properties"]["note"][
        "x-unilabos-editor-control"
    ] = "site_selector"
    adapter = _require_public_member(
        "unilabos.registry.catalog_consumer",
        "workflow_template_imports_from_registry_snapshot",
    )

    imports = adapter(
        MappingProxyType(snapshot),
        authority_id="os-local",
        resource_template_identity_resolver=lambda _identity: RESOURCE_TEMPLATE_UUID,
    )

    transfer = next(item for item in imports if item.template["name"] == "transfer")
    note = next(
        item
        for item in transfer.handles
        if item["io_type"] == "target" and item["handle_key"] == "note"
    )
    assert note["meta_data"]["unilab"]["editor_control"] == "site_selector"


def test_ros_transport_fields_cannot_rewrite_the_canonical_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_package(tmp_path)
    catalog = compile_package_source(WorkspaceSource(tmp_path))
    device = catalog.definitions.devices[0]
    consumers = importlib.import_module("unilabos.package_manager.consumers")
    metadata = consumers._device_ast_metadata(device)
    metadata["actions"] = {"transfer": metadata["actions"]["transfer"]}
    metadata["actions"]["transfer"]["action_args"]["action_type"] = (
        "test_transport:FakeAction"
    )
    registry_module = importlib.import_module("unilabos.registry.registry")

    class Goal:
        @staticmethod
        def get_fields_and_field_types() -> dict[str, str]:
            return {
                "sample": "string",
                "volume": "double",
                "mode": "string",
                "note": "string",
                "batches": "sequence<int32>",
                "payload": "string",
                "unexpected": "string",
            }

    class Result:
        @staticmethod
        def get_fields_and_field_types() -> dict[str, str]:
            return {"sample": "string", "report": "string"}

    class Feedback:
        @staticmethod
        def get_fields_and_field_types() -> dict[str, str]:
            return {"progress": "double"}

    class FakeAction:
        pass

    FakeAction.Goal = Goal
    FakeAction.Feedback = Feedback
    FakeAction.Result = Result
    monkeypatch.setattr(
        registry_module,
        "resolve_type_object",
        lambda _identity: FakeAction,
    )

    with pytest.raises(ValueError, match="ROS goal mapping"):
        Registry()._build_device_entry_from_ast(
            device.fqid,
            metadata,
            allow_definition_imports=False,
        )


def test_ros_transport_mappings_are_validated_and_preserved_as_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_package(tmp_path)
    catalog = compile_package_source(WorkspaceSource(tmp_path))
    device = catalog.definitions.devices[0]
    consumers = importlib.import_module("unilabos.package_manager.consumers")
    metadata = consumers._device_ast_metadata(device)
    metadata["actions"] = {"transfer": metadata["actions"]["transfer"]}
    action_args = metadata["actions"]["transfer"]["action_args"]
    action_args["action_type"] = "test_transport:FakeAction"
    action_args["feedback"] = {"progress": "completion"}
    registry_module = importlib.import_module("unilabos.registry.registry")

    class Goal:
        @staticmethod
        def get_fields_and_field_types() -> dict[str, str]:
            return {
                "sample": "string",
                "volume": "double",
                "mode": "string",
                "note": "string",
                "batches": "sequence<int32>",
                "payload": "string",
                "unilabos_param": "string",
            }

    class Feedback:
        @staticmethod
        def get_fields_and_field_types() -> dict[str, str]:
            return {"progress": "double"}

    class Result:
        @staticmethod
        def get_fields_and_field_types() -> dict[str, str]:
            return {
                "sample": "string",
                "report": "string",
                "unilabos_samples": "sequence<string>",
            }

    class FakeAction:
        pass

    FakeAction.Goal = Goal
    FakeAction.Feedback = Feedback
    FakeAction.Result = Result
    monkeypatch.setattr(
        registry_module,
        "resolve_type_object",
        lambda _identity: FakeAction,
    )

    registry = Registry()
    entry = registry._build_device_entry_from_ast(
        device.fqid,
        metadata,
        allow_definition_imports=False,
    )["class"]["action_value_mappings"]["transfer"]

    assert entry["goal"] == {
        name: name
        for name in entry["schema"]["x-unilabos-action-contract"]["input_order"]
    }
    assert entry["feedback"] == {"progress": "completion"}
    assert entry["result"] == {"sample": "sample", "report": "report"}


def test_registry_snapshot_adapter_excludes_auto_actions_and_keeps_input_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_package(tmp_path)
    catalog = compile_package_source(WorkspaceSource(tmp_path))
    registry = _register(catalog, monkeypatch)
    adapter = _require_public_member(
        "unilabos.registry.catalog_consumer",
        "workflow_template_imports_from_registry_snapshot",
    )
    snapshot = MappingProxyType(copy.deepcopy(registry.device_type_registry))

    imports = adapter(
        snapshot,
        authority_id="os-local",
        resource_template_identity_resolver=lambda source_identity: {
            "community.a1_contract_lab.pump": RESOURCE_TEMPLATE_UUID,
            "a1_contract_lab.resources:plate_96": RESOURCE_TEMPLATE_UUID,
        }[source_identity],
    )

    names = [item.template["name"] for item in imports]
    assert names == [
        "consume",
        "defaulted",
        "inspect",
        "measure",
        "raw_bare",
        "raw_pep585",
        "raw_typing",
        "reset",
        "transfer",
    ]
    assert "health" not in names
    transfer = next(item for item in imports if item.template["name"] == "transfer")
    handles = list(transfer.handles)
    assert [item["handle_key"] for item in handles if item["io_type"] == "target"] == [
        "sample",
        "volume",
        "mode",
        "note",
        "batches",
        "payload",
        "ready",
    ]
    assert [item["handle_key"] for item in handles if item["io_type"] == "source"] == [
        "sample",
        "report",
        "ready",
    ]
    consume = next(item for item in imports if item.template["name"] == "consume")
    implicit = next(
        item
        for item in consume.handles
        if item["handle_key"] == "sample" and item["io_type"] == "source"
    )
    assert implicit["data_source"] == "result"
    assert implicit["data_key"] == "sample"
    assert implicit["meta_data"]["unilab"]["implicit_passthrough"] is True
