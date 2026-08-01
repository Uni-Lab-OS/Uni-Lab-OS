"""F006 在 FE-OS 基线上的 PackageCatalog 公共合同。

这些测试只绑定 source-neutral Catalog、现有 Registry/Workflow/Material Authority
的公开边界，不绑定 AST walker 或 CLI adapter 的内部实现。
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
HIDDEN_WORKFLOW_UUID = "22222222-2222-4222-8222-222222222222"
RESOURCE_TEMPLATE_UUID = "10000000-0000-4000-8000-000000000001"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _require_module(name: str, *members: str) -> ModuleType:
    """把缺失 feature 报成逐项 RED，而不是 collection error。"""

    relative = name.split(".")[1:]
    local_module = REPOSITORY_ROOT / "unilabos" / Path(*relative)
    if not (local_module.with_suffix(".py").is_file() or local_module.is_dir()):
        pytest.fail(f"F006 缺少公共模块 {name}", pytrace=False)
    try:
        module = importlib.import_module(name)
    except ModuleNotFoundError as error:
        if error.name != name and not name.startswith(f"{error.name}."):
            raise
        pytest.fail(f"F006 缺少公共模块 {name}", pytrace=False)
    missing = [member for member in members if not hasattr(module, member)]
    if missing:
        pytest.fail(
            f"F006 模块 {name} 缺少公共 Interface: {', '.join(missing)}",
            pytrace=False,
        )
    return module


def _package_api() -> ModuleType:
    return _require_module(
        "unilabos.package_manager",
        "InstalledDistributionSource",
        "PackageAssetResolver",
        "PackageCatalog",
        "PackageSource",
        "WorkspaceSource",
        "compile_package_source",
    )


def _write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def _write_pyproject(root: Path, distribution: str, import_package: str) -> None:
    _write(
        root / "pyproject.toml",
        f"""
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "{distribution}"
version = "1.0.0"

[tool.setuptools.packages.find]
include = ["{import_package}*"]
""".strip(),
    )
    _write(root / import_package / "__init__.py", "")


def _write_device_package(
    root: Path,
    *,
    distribution: str,
    import_package: str,
    device_ids: tuple[str, ...] = ("pump",),
) -> None:
    _write_pyproject(root, distribution, import_package)
    for device_id in device_ids:
        class_name = "".join(part.title() for part in device_id.split("_"))
        _write(
            root / import_package / f"{device_id}.py",
            f'''from unilabos.registry.decorators import action, device

raise RuntimeError("package discovery imported {device_id}")

@device(id="{device_id}", category=["test"])
class {class_name}:
    def __init__(self, endpoint: str, retries: int = 3):
        self.endpoint = endpoint
        self.retries = retries

    @action(description="运行")
    def run(self, duration: float = 1.0) -> None:
        pass
''',
        )


def _write_workflow_package(root: Path) -> str:
    _write_pyproject(root, "workflow-lab", "workflow_lab")
    source = f'''from unilabos.workflow.authoring import workflow_definition

@workflow_definition(
    workflow_uuid="{WORKFLOW_UUID}",
    displayname="混合",
)
def mix() -> None:
    pass
'''
    _write(root / "workflow_lab" / "workflows" / "mix.py", source)
    _write(
        root / "workflow_lab" / "workflows" / "not_declared.py",
        f'''from unilabos.workflow.authoring import workflow_definition

@workflow_definition(
    workflow_uuid="{HIDDEN_WORKFLOW_UUID}",
    displayname="未登记",
)
def hidden() -> None:
    pass
''',
    )
    _write(
        root / "package.yaml",
        f"""package:
  name: workflow_lab

workflows:
  - workflow_uuid: {WORKFLOW_UUID}
    source: workflow_lab/workflows/mix.py
""",
    )
    return source


def _write_resource_package(root: Path) -> None:
    _write_pyproject(root, "resource-lab", "resource_lab")
    _write(
        root / "resource_lab" / "plate.py",
        """from unilabos.registry.decorators import resource

raise RuntimeError("package discovery imported resource source")

@resource(
    id="plate",
    category=["labware"],
    model={"web": {"format": "glb", "entry": "models/plate.glb"}},
)
def make_plate(rows: int = 8):
    return rows
""",
    )
    _write(root / "resource_lab" / "models" / "plate.glb", b"glTF-resource")


def _definitions(catalog: Any, kind: str) -> tuple[Any, ...]:
    return tuple(getattr(catalog.definitions, kind))


def test_each_explicit_source_compiles_to_the_same_source_neutral_catalog_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Workspace/editable 与 installed wheel 共享一个 Catalog Interface。

    同一 package 的物理来源、绝对路径和安装位置不得进入 canonical Catalog。
    composition 可多次调用这个 singular seam，但不产生跨包 Inventory。
    """

    api = _package_api()
    distribution = _require_module(
        "unilabos.package_manager.distribution", "build_workspace_wheel"
    )
    first_root = tmp_path / "workspace-a"
    second_root = tmp_path / "workspace-b"
    _write_device_package(
        first_root,
        distribution="parity-lab",
        import_package="parity_lab",
    )
    _write_device_package(
        second_root,
        distribution="parity-lab",
        import_package="parity_lab",
    )

    workspace_catalog = api.compile_package_source(api.WorkspaceSource(first_root))
    editable_catalog = api.compile_package_source(api.WorkspaceSource(second_root))
    artifact = distribution.build_workspace_wheel(first_root, tmp_path / "dist")

    install_root = tmp_path / "installed"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(install_root),
            str(artifact.wheel),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    monkeypatch.syspath_prepend(str(install_root))
    installed_catalog = api.compile_package_source(
        api.InstalledDistributionSource("parity-lab")
    )

    assert isinstance(api.WorkspaceSource(first_root), api.PackageSource)
    assert all(
        isinstance(catalog, api.PackageCatalog)
        for catalog in (workspace_catalog, editable_catalog, installed_catalog)
    )
    assert workspace_catalog.to_canonical_bytes() == (
        editable_catalog.to_canonical_bytes()
    )
    assert workspace_catalog.to_canonical_bytes() == (
        installed_catalog.to_canonical_bytes()
    )
    canonical = workspace_catalog.to_canonical_bytes().decode("utf-8")
    assert str(first_root) not in canonical
    assert str(second_root) not in canonical
    assert str(install_root) not in canonical
    assert "workspace" not in json.loads(canonical)


def test_workspace_catalog_discovers_definitions_without_import_or_activation(
    tmp_path: Path,
) -> None:
    api = _package_api()
    _write_device_package(
        tmp_path,
        distribution="discovery-lab",
        import_package="discovery_lab",
        device_ids=("selected", "idle"),
    )

    catalog = api.compile_package_source(api.WorkspaceSource(tmp_path))

    assert [item.fqid for item in _definitions(catalog, "devices")] == [
        "community.discovery_lab.idle",
        "community.discovery_lab.selected",
    ]
    assert "discovery_lab.idle" not in sys.modules
    assert "discovery_lab.selected" not in sys.modules
    assert not hasattr(catalog, "materials")
    assert "profile" not in catalog.to_dict()


def test_workflow_identity_comes_from_registered_package_relative_draft(
    tmp_path: Path,
) -> None:
    """Package discovery 只登记 Draft source，不 Apply、不创建 Task。"""

    api = _package_api()
    source = _write_workflow_package(tmp_path)
    catalog = api.compile_package_source(api.WorkspaceSource(tmp_path))
    workflows = _definitions(catalog, "workflows")

    assert len(workflows) == 1
    workflow = workflows[0]
    assert workflow.module == "workflow_lab.workflows.mix"
    assert workflow.symbol == "mix"
    assert workflow.declaring_file == "workflow_lab/workflows/mix.py"
    assert workflow.content_hash == (
        "sha256:" + hashlib.sha256(source.encode("utf-8")).hexdigest()
    )
    assert workflow.details["workflow_uuid"] == WORKFLOW_UUID
    assert workflow.details["source_uri"] == ("package://workflow_lab/workflows/mix.py")
    assert HIDDEN_WORKFLOW_UUID not in catalog.to_canonical_bytes().decode("utf-8")
    assert "workflow_lab.workflows.mix" not in sys.modules

    discovery = _require_module(
        "unilabos.workflow.source_discovery", "register_editable_package_sources"
    )

    class DraftOnlyService:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def register_editable_source(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(kwargs)
            return {"workflow_uuid": kwargs["workflow_uuid"]}

        def __getattr__(self, name: str) -> Any:
            if name in {"apply", "create_task", "create_workflow_task", "run"}:
                raise AssertionError(
                    f"package discovery attempted runtime side effect: {name}"
                )
            raise AttributeError(name)

    service = DraftOnlyService()
    discovery.register_editable_package_sources(service, tmp_path)

    assert service.calls == [
        {
            "workflow_uuid": WORKFLOW_UUID,
            "package_id": "workflow_lab",
            "package_root": tmp_path / "workflow_lab",
            "relative_path": "workflows/mix.py",
        }
    ]


def test_catalog_projects_registry_and_assets_without_overwriting_live_material(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _package_api()
    consumer = _require_module(
        "unilabos.registry.catalog_consumer", "register_package_catalog"
    )
    from unilabos.app.scheduler.inventory.service import InventoryService
    from unilabos.app.scheduler.inventory.store import InventoryStore
    from unilabos.registry.registry import lab_registry

    _write_resource_package(tmp_path)
    source = api.WorkspaceSource(tmp_path)
    catalog = api.compile_package_source(source)
    resolver = api.PackageAssetResolver(source, catalog)

    store = InventoryStore(str(tmp_path / "inventory.db"))
    material_service = InventoryService(store)
    try:
        material_service.upsert_template(
            template_id="existing-template",
            name="Existing",
            category="sample",
        )
        material_service.register_instance(
            template_id="existing-template",
            barcode="LIVE-001",
            edge_uuid="material-live-001",
        )
        before_templates = store.query_all(
            "SELECT * FROM resource_template ORDER BY template_id"
        )
        before_materials = store.query_all(
            "SELECT * FROM material_instance ORDER BY edge_uuid"
        )

        monkeypatch.setattr(lab_registry, "device_type_registry", {})
        monkeypatch.setattr(lab_registry, "resource_type_registry", {})
        consumer.register_package_catalog(lab_registry, catalog)

        entry = lab_registry.resource_type_registry["community.resource_lab.plate"]
        assert entry["source_fqid"] == "community.resource_lab.plate"
        logical_path = entry["model"]["web"]["entry"]
        assert logical_path == "resource_lab/models/plate.glb"
        assert resolver.public_metadata(logical_path) == catalog.assets[0]
        with resolver.open_binary(logical_path) as stream:
            assert stream.read() == b"glTF-resource"

        assert (
            store.query_all("SELECT * FROM resource_template ORDER BY template_id")
            == before_templates
        )
        assert (
            store.query_all("SELECT * FROM material_instance ORDER BY edge_uuid")
            == before_materials
        )
    finally:
        store.close()


def test_actions_project_into_the_existing_process_local_template_catalog(
    tmp_path: Path,
) -> None:
    """Package action contracts must feed D-042 instead of a second workflow catalog."""

    api = _package_api()
    consumer = _require_module(
        "unilabos.registry.catalog_consumer",
        "workflow_template_imports_from_package_catalog",
    )
    from unilabos.workflow.catalog import CatalogAuthority, TemplateCatalog
    from unilabos.workflow.store import WorkflowStore

    _write_device_package(
        tmp_path,
        distribution="template-lab",
        import_package="template_lab",
    )
    catalog = api.compile_package_source(api.WorkspaceSource(tmp_path))
    imports = consumer.workflow_template_imports_from_package_catalog(
        catalog,
        resource_template_uuids={
            "community.template_lab.pump": RESOURCE_TEMPLATE_UUID,
        },
    )
    assert imports

    store = WorkflowStore(tmp_path / "workflow.db")
    try:
        authority = CatalogAuthority(authority_id="os-local", kind="local")
        template_catalog = TemplateCatalog(store)
        first = template_catalog.replace(authority, imports)
        second = template_catalog.replace(authority, imports)

        assert len(first.node_templates) == 1
        projected = first.node_templates[0]
        assert projected["resource_template_uuid"] == RESOURCE_TEMPLATE_UUID
        assert projected["name"] == "run"
        assert projected["meta_data"]["unilab"]["source_fqid"] == (
            "community.template_lab.pump.run"
        )
        assert projected["uuid"] == second.node_templates[0]["uuid"]
        assert first.fingerprint == second.fingerprint
    finally:
        store.close()


def test_graph_node_is_the_only_device_instance_and_connection_config_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _package_api()
    consumer = _require_module(
        "unilabos.registry.catalog_consumer", "register_package_catalog"
    )
    from unilabos.registry.registry import lab_registry
    from unilabos.ros import initialize_device

    _write_device_package(
        tmp_path,
        distribution="activation-lab",
        import_package="activation_lab",
        device_ids=("selected", "idle"),
    )
    catalog = api.compile_package_source(api.WorkspaceSource(tmp_path))
    builtin_entry = {
        "class": {
            "module": "unilabos.devices.builtin:BuiltIn",
            "type": "python",
            "action_value_mappings": {},
        }
    }
    monkeypatch.setattr(
        lab_registry,
        "device_type_registry",
        {"builtin.device": builtin_entry},
    )
    monkeypatch.setattr(lab_registry, "resource_type_registry", {})
    consumer.register_package_catalog(lab_registry, catalog)

    assert lab_registry.device_type_registry["builtin.device"] is builtin_entry
    assert set(lab_registry.device_type_registry) == {
        "builtin.device",
        "community.activation_lab.idle",
        "community.activation_lab.selected",
    }

    imported: list[str] = []
    constructed: list[dict[str, Any]] = []

    class Driver:
        def __init__(self, **kwargs: Any) -> None:
            constructed.append(kwargs)

    def get_class(module: str) -> type[Driver]:
        imported.append(module)
        return Driver

    monkeypatch.setattr(initialize_device.default_manager, "get_class", get_class)
    monkeypatch.setattr(
        initialize_device,
        "ros2_device_node",
        lambda driver, **_kwargs: driver,
    )
    graph_node = SimpleNamespace(
        res_content=SimpleNamespace(
            klass="community.activation_lab.selected",
            uuid="selected-material-uuid",
            config={"endpoint": "serial:///dev/ttyUSB0", "retries": 5},
        )
    )

    initialized = initialize_device.initialize_device_from_dict(
        "selected-instance", graph_node
    )

    assert initialized is not None
    assert imported == ["activation_lab.selected:Selected"]
    assert constructed == [
        {
            "device_id": "selected-instance",
            "device_uuid": "selected-material-uuid",
            "driver_is_ros": False,
            "driver_params": {
                "endpoint": "serial:///dev/ttyUSB0",
                "retries": 5,
            },
        }
    ]
    assert all("activation_lab.idle" not in module for module in imported)


def test_cli_is_an_adapter_for_workspace_and_package_manager(
    tmp_path: Path,
) -> None:
    _package_api()
    cli = _require_module(
        "unilabos.package_manager.cli", "register_package_subcommands"
    )
    from unilabos.app.main import parse_args

    del cli
    parser = parse_args()
    startup = parser.parse_args(
        ["--workspace", str(tmp_path), "--graph", "deployment/graph.json"]
    )
    inspect_args = parser.parse_args(
        ["package", "inspect", "--path", str(tmp_path), "--json"]
    )

    assert startup.workspace == str(tmp_path)
    assert startup.graph == "deployment/graph.json"
    assert inspect_args.package_action == "inspect"
    with pytest.raises(SystemExit):
        parser.parse_args(["--profile", "deployment/profile.yaml"])

    app_package_adapter = (
        Path(importlib.import_module("unilabos.app").__file__).resolve().parent
        / "package_cli.py"
    )
    assert not app_package_adapter.exists()


def test_package_manager_core_has_no_dependency_on_app_adapters() -> None:
    api = _package_api()
    package_root = Path(api.__file__).resolve().parent

    for module_path in sorted(package_root.glob("*.py")):
        source = module_path.read_text(encoding="utf-8")
        assert "from unilabos.app" not in source, module_path.name
        assert "import unilabos.app" not in source, module_path.name

    public_names = set(api.__all__)
    assert {
        "PackageCatalog",
        "PackageSource",
        "WorkspaceSource",
        "InstalledDistributionSource",
        "compile_package_source",
    } <= public_names
    assert "cmd_package" not in public_names
    assert "register_package_subcommands" not in public_names
    assert "unilabos.app.main" not in inspect.getsource(api)
