"""用户可上传的 SZLab mock 设备包端到端静态与运行合同。"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from unilabos.package_manager import (
    CachedArchiveSource,
    WorkspaceSource,
    compile_package_source,
)
from unilabos.package_manager.community import resolve_graph_packages
from unilabos.package_manager.device_package import (
    configuration_schema_for_definition,
    validate_configuration_for_definition,
)
from unilabos.package_manager.distribution import build_workspace_wheel

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_MOCK_WORKSPACE = _REPOSITORY_ROOT / "examples" / "szlab_mock_package"
_MOCK_GRAPH = _MOCK_WORKSPACE / "deployment" / "graphs" / "mock-szlab-local.json"
_DEFINITION_FQID = "community.unilab_szlab_mock.mock_s08_cap_station"


def test_mock_workspace_compiles_to_unique_szlab_shaped_catalog() -> None:
    """Workspace 必须产生唯一 mock 身份、一个设备和四个显式 Action。"""

    catalog = compile_package_source(WorkspaceSource(_MOCK_WORKSPACE))

    assert catalog.distribution.name == "unilab-szlab-mock"
    assert catalog.distribution.version == "0.1.0"
    assert catalog.namespace == "community.unilab_szlab_mock"
    assert len(catalog.definitions.devices) == 1
    definition = catalog.definitions.devices[0]
    assert definition.fqid == _DEFINITION_FQID
    assert definition.displayname == "Mock S08 开关盖工位"
    assert {item["name"] for item in definition.details["actions"]} == {
        "ping",
        "process_cap",
        "read_status",
        "reset",
    }


def test_mock_configuration_schema_is_renderable_by_electron() -> None:
    """初始化参数必须投影为 Electron 可严格渲染且全部带默认值的 Schema。"""

    catalog = compile_package_source(WorkspaceSource(_MOCK_WORKSPACE))
    schema = configuration_schema_for_definition(catalog.definitions.devices[0])

    assert schema["required"] == []
    assert schema["additionalProperties"] is False
    assert schema["properties"] == {
        "station_name": {"type": "string", "default": "Mock S08 Cap Station"},
        "auto_connect": {"type": "boolean", "default": True},
        "cycle_delay_ms": {"type": "integer", "default": 20},
        "cap_slot_count": {"type": "integer", "default": 5},
        "initial_occupied_slots": {"type": "integer", "default": 0},
        "channel_map": {
            "type": "object",
            "x-unilab-python-type": "dict[str, str] | None",
            "default": None,
        },
    }


def test_mock_configuration_accepts_explicit_nullable_default() -> None:
    """Electron 回传 Schema 的显式 null 默认值时必须与省略字段语义一致。"""

    catalog = compile_package_source(WorkspaceSource(_MOCK_WORKSPACE))
    definition = catalog.definitions.devices[0]

    omitted = validate_configuration_for_definition(definition, {})
    explicit = validate_configuration_for_definition(
        definition,
        {"channel_map": None},
    )

    assert omitted["channel_map"] is None
    assert explicit["channel_map"] is None


def test_mock_graph_uses_workspace_catalog_without_duplicate_registration(
    tmp_path: Path,
) -> None:
    """直接调试 Graph 应复用 workspace Catalog，不应再解析第二份同 namespace 包。"""

    catalog = compile_package_source(WorkspaceSource(_MOCK_WORKSPACE))
    graph = json.loads(_MOCK_GRAPH.read_text(encoding="utf-8"))

    resolution = resolve_graph_packages(
        graph,
        working_dir=tmp_path,
        available_catalogs=(catalog,),
    )

    assert resolution.classes == (_DEFINITION_FQID,)
    assert resolution.sources == ()
    assert resolution.catalogs == ()


def test_mock_wheel_preserves_catalog_identity(tmp_path: Path) -> None:
    """构建后的 wheel 必须通过自审计并保持 Workspace Catalog 完全一致。"""

    workspace_catalog = compile_package_source(WorkspaceSource(_MOCK_WORKSPACE))
    artifact = build_workspace_wheel(_MOCK_WORKSPACE, tmp_path / "dist")
    cached_catalog = compile_package_source(
        CachedArchiveSource(artifact.wheel, artifact.artifact_digest)
    )

    assert artifact.wheel.is_file()
    assert (
        artifact.catalog.to_canonical_bytes() == workspace_catalog.to_canonical_bytes()
    )
    assert cached_catalog.to_canonical_bytes() == workspace_catalog.to_canonical_bytes()


def test_mock_driver_executes_open_close_cycle_without_hardware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """驱动必须在无网络和无 PLC 环境中完成开盖、关盖及状态重置。"""

    monkeypatch.syspath_prepend(str(_MOCK_WORKSPACE))
    module = importlib.import_module("unilab_szlab_mock.cap_station")
    driver = module.MockS08CapStation(cycle_delay_ms=0)

    ping = driver.ping("electron-validation")
    opened = driver.process_cap(operation="open", sample_id=101)
    closed = driver.process_cap(operation="close", sample_id=101)
    status = driver.read_status()
    reset = driver.reset(reconnect=True)

    assert ping == {
        "success": True,
        "message": "electron-validation",
        "station_name": "Mock S08 Cap Station",
        "cycle_count": 0,
    }
    assert opened["success"] is True
    assert opened["occupied_slots"] == 1
    assert opened["cycle_count"] == 1
    assert closed["success"] is True
    assert closed["occupied_slots"] == 0
    assert closed["cycle_count"] == 2
    assert status["occupied_slots"] == 0
    assert status["cycle_count"] == 2
    assert reset["connected"] is True
    assert reset["cycle_count"] == 0
