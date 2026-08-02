"""Package-local Xacro assets projected through the Material read API."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from unilabos.app.scheduler.inventory import (
    InventoryService,
    ResourceTemplateIdentity,
)
from unilabos.app.scheduler.inventory.api import create_app
from unilabos.app.scheduler.inventory.material_projection import (
    build_package_material_projection,
    build_resource_graph_import,
)
from unilabos.package_manager import WorkspaceSource, compile_package_source

TEMPLATE_UUID = "81000000-0000-4000-8000-000000000601"


def _write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def _workspace(root: Path) -> WorkspaceSource:
    _write(
        root / "pyproject.toml",
        """
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "material-model-lab"
version = "1.0.0"

[tool.setuptools.packages.find]
include = ["material_model_lab*"]

[tool.setuptools.package-data]
material_model_lab = ["**/*.xacro", "**/*.stl", "**/*.yml"]
""".strip(),
    )
    _write(root / "material_model_lab" / "__init__.py", "")
    _write(
        root / "material_model_lab" / "devices" / "robot" / "device.py",
        """from unilabos.registry.decorators import device

@device(
    id="model_robot",
    category=["robotic_arm"],
    model={
        "format": "xacro",
        "entry": "models/device.xacro",
        "macro": "model_robot",
        "shape": {
            "format": "unilab.shape/v1",
            "entry": "models/shape.yml",
        },
    },
)
class ModelRobot:
    pass
""",
    )
    _write(
        root
        / "material_model_lab"
        / "devices"
        / "robot"
        / "models"
        / "device.xacro",
        '<robot xmlns:xacro="http://ros.org/wiki/xacro"/>',
    )
    _write(
        root
        / "material_model_lab"
        / "devices"
        / "robot"
        / "models"
        / "meshes"
        / "body.stl",
        b"solid body\nendsolid body\n",
    )
    _write(
        root
        / "material_model_lab"
        / "devices"
        / "robot"
        / "models"
        / "shape.yml",
        """schema_version: 1
shape:
  id: model_robot
  applies_to:
    - category: robotic_arm
  envelope: [500, 400, 300]
  parts:
    - type: box
      from: [0, 0, 0]
      to: [500, 400, 300]
""",
    )
    return WorkspaceSource(root)


def test_graph_projects_package_model_and_serves_its_audited_asset_closure(
    tmp_path: Path,
) -> None:
    source = _workspace(tmp_path / "package")
    catalog = compile_package_source(source)
    projection = build_package_material_projection((source,), (catalog,))
    definition = projection.definitions["model_robot"]

    assert definition.model is not None
    assert definition.model["format"] == "xacro"
    assert definition.model["macro"] == "model_robot"
    assert definition.model["path"].endswith("/models/device.xacro")
    assert len(projection.model_assets) == 3

    imported = build_resource_graph_import(
        {
            "source_id": "model-graph.json",
            "nodes": [
                {
                    "id": "robot",
                    "uuid": "runtime-robot",
                    "class": "model_robot",
                    "type": "device",
                    "name": "Robot",
                    "config": {},
                    "data": {},
                    "position": [0, 0, 0],
                    "size": [500, 400, 300],
                }
            ],
        },
        projection,
        {definition.source_identity: TEMPLATE_UUID},
    )
    inventory = InventoryService.open(
        working_dir=tmp_path / "runtime",
        resource_templates={
            TEMPLATE_UUID: ResourceTemplateIdentity(
                uuid=TEMPLATE_UUID,
                material_class=definition.source_identity,
            )
        },
        material_shapes=projection.shapes,
        material_model_assets=projection.model_assets,
    )
    try:
        inventory.bootstrap_resource_graph(imported)
        with TestClient(create_app(inventory)) as client:
            graph = client.get("/api/v1/materials/graph").json()
            model = graph["data"]["nodes"][0]["material"]["config"]["rendering"][
                "model"
            ]
            assert model == definition.model

            xacro = client.get(model["path"])
            assert xacro.status_code == 200
            assert xacro.headers["content-type"].startswith("application/")
            assert b"xmlns:xacro" in xacro.content

            mesh_path = model["meshDir"] + "/meshes/body.stl"
            mesh = client.get(mesh_path)
            assert mesh.status_code == 200
            assert mesh.content.startswith(b"solid body")

            assert client.get(model["meshDir"] + "/../device.py").status_code == 404
    finally:
        inventory.close()
