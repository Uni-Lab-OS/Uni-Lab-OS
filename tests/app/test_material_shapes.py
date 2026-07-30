"""2.5D 外形登记表与 ``/api/v1/material-shapes``。

外形是设备包的资产，桥只做搬运：坏条目要在启动时被拦下，好条目要原样下发，
没有登记表时端点要明确报 503 而不是返回空列表（前端分不清"没有外形"和
"后端没起来"）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from unilabos.app.local_bridge.local_api import LocalApiState, create_app
from unilabos.app.local_bridge.material_shapes import MaterialShapeRegistry
from unilabos.app.local_bridge.schedule_ws import ScheduleSession


CORE_MANIFEST = (
    Path(__file__).resolve().parents[2]
    / "unilabos"
    / "app"
    / "local_bridge"
    / "shapes"
    / "core_shapes.yaml"
)


async def _sink(_msg: dict[str, Any]) -> None:
    return None


def _client(registry: MaterialShapeRegistry | None) -> TestClient:
    state = LocalApiState(
        ScheduleSession(_sink),
        material_shape_registry=registry,
    )
    return TestClient(create_app(lambda: state))


def _manifest(path: Path, body: str) -> Path:
    path.write_text(
        "schema_version: 1\nbundle: { id: probe }\nshapes:\n" + body,
        encoding="utf-8",
    )
    return path


def _from_bundle(
    manifest: Path, bundle: str
) -> dict[str, dict[str, Any]]:
    """只看某个 bundle 的条目：装了设备包的环境里登记表还会带上包内声明。"""

    return {
        shape["id"]: shape
        for shape in MaterialShapeRegistry(core_manifest=manifest).list_shapes()
        if shape["bundle"] == bundle
    }


def test_core_manifest_declares_the_generic_vessels() -> None:
    shapes = _from_bundle(CORE_MANIFEST, "unilabos-core")

    assert {"labware", "stack", "beaker", "bottle", "capped_bottle"} <= set(shapes)
    assert shapes["beaker"]["categoryTokens"] == ["beaker"]
    # 器皿轮廓按实例包络缩放，不能写死 mm
    assert shapes["beaker"]["units"] == "ratio"
    assert shapes["beaker"]["shadow"] == "round"


def test_unknown_part_type_or_style_drops_the_whole_shape(tmp_path: Path) -> None:
    """一条声明里有画不出来的图元就整条丢弃——宁可退回方盒，不要画半台设备。"""

    manifest = _manifest(
        tmp_path / "shapes.yaml",
        """
  - id: good
    applies_to: [{ category: probe_good }]
    parts:
      - { type: box, style: body, from: [0, 0, 0], to: [1, 1, 1] }
  - id: bad_type
    applies_to: [{ category: probe_bad_type }]
    parts:
      - { type: teleport, style: body }
  - id: bad_style
    applies_to: [{ category: probe_bad_style }]
    parts:
      - { type: box, style: carousel, from: [0, 0, 0], to: [1, 1, 1] }
  - id: bad_generator
    applies_to: [{ category: probe_bad_generator }]
    parts:
      - { type: sites, generator: shelf-boards }
  - id: no_applies_to
    parts:
      - { type: box, style: body, from: [0, 0, 0], to: [1, 1, 1] }
""",
    )

    assert list(_from_bundle(manifest, "probe")) == ["good"]


def test_nested_grid_is_rejected(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path / "shapes.yaml",
        """
  - id: nested
    applies_to: [{ category: probe_nested }]
    parts:
      - type: grid
        count: [2, 2]
        pitch: [10, 10]
        part:
          type: grid
          count: [2, 2]
          pitch: [5, 5]
          part: { type: disc, style: hole, center: [0, 0], z: 1, d: 4 }
""",
    )

    assert _from_bundle(manifest, "probe") == {}


def test_endpoint_serves_the_registry(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path / "shapes.yaml",
        """
  - id: probe_shape
    display_name: 探针
    applies_to:
      - category: probe_exact
      - category_contains: probe
    envelope: [10.0, 20.0, 30.0]
    priority: 7
    sort: rear-edge
    parts:
      - { type: slab, style: plate, z: [0, 5] }
""",
    )
    response = _client(MaterialShapeRegistry(core_manifest=manifest)).get(
        "/api/v1/material-shapes"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 0
    assert payload["data"]["total"] == len(payload["data"]["items"])
    probe = next(
        item for item in payload["data"]["items"] if item["bundle"] == "probe"
    )
    assert probe == {
        "id": "probe_shape",
        "bundle": "probe",
        "categories": ["probe-exact"],
        "categoryTokens": ["probe"],
        "priority": 7,
        "units": "mm",
        "shadow": "box",
        "sort": "rear-edge",
        "parts": [{"type": "slab", "style": "plate", "z": [0, 5]}],
        "displayName": "探针",
        "envelope": [10.0, 20.0, 30.0],
    }


def test_endpoint_reports_unavailable_without_a_registry() -> None:
    response = _client(None).get("/api/v1/material-shapes")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "MATERIAL_SHAPES_UNAVAILABLE"
