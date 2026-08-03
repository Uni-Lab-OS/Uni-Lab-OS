"""F006 Graph identity 与 package resource activation 的跨边界合同。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from unilabos.app.scheduler.inventory.material_projection import (
    build_package_material_projection,
    build_resource_graph_import,
)
from unilabos.package_manager import WorkspaceSource, compile_package_source

TEMPLATE_UUID_A = "83000000-0000-4000-8000-000000000001"
TEMPLATE_UUID_B = "83000000-0000-4000-8000-000000000002"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_resource_workspace(
    root: Path,
    *,
    distribution: str,
    import_package: str,
    definition_id: str = "process_warehouse",
) -> WorkspaceSource:
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
    _write(
        root / import_package / "resources.py",
        f'''from unilabos.registry.decorators import resource

@resource(id="{definition_id}", category=["warehouse"])
def make_process_warehouse(name: str):
    return name
''',
    )
    return WorkspaceSource(root)


def _graph_snapshot(graph_class: str) -> dict[str, object]:
    return {
        "source_id": "fqid-resource-graph.json",
        "nodes": [
            {
                "id": "warehouse-a",
                "uuid": "runtime-warehouse-a",
                "name": "Warehouse A",
                "class": graph_class,
                "type": "warehouse",
                "config": {},
                "data": {},
            }
        ],
    }


@pytest.mark.parametrize(
    "graph_class",
    (
        "community.fqid_projection_lab.process_warehouse",
        "process_warehouse",
    ),
    ids=("canonical-fqid", "unique-legacy-short-alias"),
)
def test_graph_class_resolves_to_the_selected_package_definition(
    tmp_path: Path,
    graph_class: str,
) -> None:
    """Graph 的 canonical FQID 与唯一 legacy short alias 指向同一定义。"""

    source = _write_resource_workspace(
        tmp_path / "package",
        distribution="fqid-projection-lab",
        import_package="fqid_projection_lab",
    )
    catalog = compile_package_source(source)
    definition = catalog.definitions.resources[0]
    projection = build_package_material_projection((source,), (catalog,))

    imported = build_resource_graph_import(
        _graph_snapshot(graph_class),
        projection,
        {f"{definition.module}:{definition.symbol}": TEMPLATE_UUID_A},
    )

    assert imported["materials"][0]["class"] == graph_class
    assert imported["materials"][0]["resource_template_uuid"] == TEMPLATE_UUID_A


def test_ambiguous_legacy_short_graph_class_fails_closed(tmp_path: Path) -> None:
    """两个 package 共享 local id 时，Graph 必须使用 FQID 明确选择。"""

    sources = (
        _write_resource_workspace(
            tmp_path / "package-a",
            distribution="ambiguous-resource-a",
            import_package="ambiguous_resource_a",
        ),
        _write_resource_workspace(
            tmp_path / "package-b",
            distribution="ambiguous-resource-b",
            import_package="ambiguous_resource_b",
        ),
    )
    catalogs = tuple(compile_package_source(source) for source in sources)
    projection = build_package_material_projection(sources, catalogs)
    resolved = {
        f"{record.module}:{record.symbol}": template_uuid
        for catalog, template_uuid in zip(
            catalogs,
            (TEMPLATE_UUID_A, TEMPLATE_UUID_B),
            strict=True,
        )
        for record in catalog.definitions.resources
    }

    with pytest.raises(ValueError, match="(歧义|ambiguous|不唯一)"):
        build_resource_graph_import(
            _graph_snapshot("process_warehouse"),
            projection,
            resolved,
        )


def _write_activation_workspace(root: Path) -> None:
    _write(
        root / "pyproject.toml",
        """
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "graph-activation-lab"
version = "1.0.0"

[tool.setuptools.packages.find]
include = ["graph_activation_lab*"]
""".strip(),
    )
    _write(root / "graph_activation_lab" / "__init__.py", "")
    _write(
        root / "graph_activation_lab" / "definitions.py",
        """from pylabrobot.resources import (
    Carrier,
    Coordinate,
    ResourceHolder,
    create_homogeneous_resources,
)

from unilabos.registry.decorators import device, resource


@device(id="controller", category=["test"])
class Controller:
    def __init__(self):
        self._attached_resource_summary = []

    def post_init(self, ros_node):
        self._attached_resource_summary = [
            {
                "type": type(item).__name__,
                "name": item.name,
                "category": item.category,
                "model": item.model,
                "sites": [child.name for child in item.children],
            }
            for item in ros_node.resource_tracker.resources
        ]

    def attached_resource_summary(self):
        return self._attached_resource_summary


@resource(id="process_warehouse", category=["warehouse"])
def make_process_warehouse(name: str):
    sites = create_homogeneous_resources(
        klass=ResourceHolder,
        locations=[Coordinate(1, 2, 3), Coordinate(4, 5, 6)],
        resource_size_x=10,
        resource_size_y=11,
        resource_size_z=12,
        name_prefix=name,
    )
    return Carrier(
        name=name,
        size_x=100,
        size_y=50,
        size_z=20,
        sites=sites,
        category="warehouse",
        model="fixture-process-warehouse",
    )
""",
    )


_ACTIVATION_SCRIPT = r"""
import json
import sys

import rclpy

from unilabos.package_manager import WorkspaceSource, compile_package_source
from unilabos.package_manager.consumers import register_package_catalog
from unilabos.registry.registry import lab_registry
from unilabos.resources.resource_tracker import ResourceTreeSet
from unilabos.ros.initialize_device import initialize_device_from_dict

workspace = sys.argv[1]
sys.path.insert(0, workspace)
catalog = compile_package_source(WorkspaceSource(workspace))
lab_registry.device_type_registry = {}
lab_registry.resource_type_registry = {}
register_package_catalog(lab_registry, catalog)
graph = ResourceTreeSet.from_raw_dict_list(
    [
        {
            "id": "controller-a",
            "uuid": "runtime-controller-a",
            "name": "Controller A",
            "class": "community.graph_activation_lab.controller",
            "type": "device",
            "config": {},
            "data": {},
        },
        {
            "id": "process-warehouse-a",
            "uuid": "runtime-process-warehouse-a",
            "parent_uuid": "runtime-controller-a",
            "name": "process-warehouse-a",
            "class": "community.graph_activation_lab.process_warehouse",
            "type": "warehouse",
            "config": {},
            "data": {},
        },
    ]
)

node = None
rclpy.init()
try:
    node = initialize_device_from_dict("controller-a", graph.root_nodes[0])
    payload = node.driver_instance.attached_resource_summary()
finally:
    if node is not None:
        node.ros_node_instance.destroy_node()
    rclpy.shutdown()

print(json.dumps(payload))
"""


def test_graph_only_device_activation_uses_package_resource_factory(
    tmp_path: Path,
) -> None:
    """设备 child 按 FQID 激活真实 factory 产物及其 sites。"""

    workspace = tmp_path / "activation-package"
    _write_activation_workspace(workspace)
    env = dict(os.environ)
    pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(REPOSITORY_ROOT), pythonpath) if item
    )
    result = subprocess.run(
        [sys.executable, "-c", _ACTIVATION_SCRIPT, str(workspace)],
        cwd=REPOSITORY_ROOT,
        env=env,
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == [
        {
            "type": "Carrier",
            "name": "process-warehouse-a",
            "category": "warehouse",
            "model": "fixture-process-warehouse",
            "sites": ["process-warehouse-a-0", "process-warehouse-a-1"],
        }
    ]
