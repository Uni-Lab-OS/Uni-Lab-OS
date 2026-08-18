"""当前 OS selected graph 运行前复位基线合同测试。"""

from __future__ import annotations

from typing import Any

from unilabos.app.runtime_baseline import (
    RuntimeBaselineError,
    freeze_runtime_baseline,
    freeze_runtime_baseline_if_valid,
    get_runtime_baseline,
)


class _Registry:
    def obtain_registry_device_info(self) -> list[dict[str, Any]]:
        return [{
            "id": "device_a",
            "display_name": "Device A",
            "class": {"module": "fixture:Device", "type": "Device", "action_value_mappings": {}},
            "handles": [],
            "category": [],
            "config_info": [],
            "available_sites": [{
                "index": 0,
                "label": "Deck",
                "position": {"x": 10, "y": 20, "z": 30},
                "size": {"width": 40, "height": 50, "depth": 60},
            }],
            "scene": [],
            "device_params": {},
        }]

    def obtain_registry_resource_info(self) -> list[dict[str, Any]]:
        return []


class _Tree:
    def dump(self) -> list[list[dict[str, Any]]]:
        return [[{
            "id": "device_a",
            "uuid": "64000000-0000-4000-8000-000000000001",
            "name": "Device A",
            "parent_uuid": None,
            "type": "device",
            "class": "device_a",
            "pose": {
                "position": {"x": 1, "y": 2, "z": 3},
                "size": {"width": 4, "height": 5, "depth": 6},
                "scale": {"x": 1, "y": 1, "z": 1},
                "rotation": {"x": 0, "y": 0, "z": 90},
            },
            "config": {},
            "data": {},
            "barcode": "device-a",
        }]]


def test_runtime_baseline_freezes_stable_detached_selected_graph(tmp_path) -> None:
    output_path = tmp_path / "runtime-baseline.json"
    first = freeze_runtime_baseline(
        resource_tree_set=_Tree(),
        registry=_Registry(),
        source_id="/workspace/selected.json",
        graph_fingerprint="graph-fingerprint",
        output_path=output_path,
    )

    assert first["schema_version"] == "unilab.runtime-baseline/v1"
    assert first["selected_graph_fingerprint"] == "sha256:graph-fingerprint"
    assert first["baseline_fingerprint"].startswith("sha256:")
    assert first["materials"][0]["source_node_id"] == "device_a"
    assert first["materials"][0]["material_uuid"]
    assert first["sites"][0]["name"] == "Deck"
    assert first["sites"][0]["definition"]["width"] == 40
    first["materials"][0]["name"] = "mutated"
    assert get_runtime_baseline()["materials"][0]["name"] == "Device A"
    assert output_path.is_file()
    assert "sha256:graph-fingerprint" in output_path.read_text(encoding="utf-8")


def test_runtime_baseline_derives_content_fingerprint_outside_workbench() -> None:
    baseline = freeze_runtime_baseline(
        resource_tree_set=_Tree(),
        registry=_Registry(),
        source_id="selected.json",
        graph_fingerprint="",
    )

    assert baseline["selected_graph_fingerprint"].startswith("sha256:")


def test_ambiguous_runtime_baseline_disables_reset_without_blocking_startup(
    tmp_path,
) -> None:
    output_path = tmp_path / "runtime-baseline.json"

    baseline, error = freeze_runtime_baseline_if_valid(
        resource_tree_set=_Tree(),
        registry=_Registry(),
        source_id="",
        graph_fingerprint="graph-fingerprint",
        output_path=output_path,
    )

    assert baseline is None
    assert error == "selected graph 来源身份不完整"
    assert not output_path.exists()
    try:
        get_runtime_baseline()
    except RuntimeBaselineError as caught:
        assert str(caught) == error
    else:
        raise AssertionError("歧义基线必须让复位不可用")
