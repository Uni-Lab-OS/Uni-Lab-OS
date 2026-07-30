"""package-management 支持文件夹式外部注册表的当前合同。

两个变体共享一个 Python class，使用 JSON ``init_param_enforce`` 区分；
``class.init`` 对象工厂 DSL 不得从 loaded/source_registry 重新出现。
"""

from pathlib import Path

from unilabos.app.package_cli import (
    inspect_package,
    read_external_registry_devices,
    read_registry_yaml_devices,
)

PKG = Path(__file__).parent / "fixtures" / "external_variant_pkg"


def test_read_external_registry_devices_discovers_folder_layout():
    # 包根没有 registry.yaml —— 旧的根目录读取器应为空
    assert read_registry_yaml_devices(PKG) == {}

    # 新增的文件夹式读取器应发现 devices/ 下的两个变体
    entries = read_external_registry_devices(PKG)
    assert set(entries) == {"vendor.lh.model_a", "vendor.lh.model_b"}

    a, b = entries["vendor.lh.model_a"], entries["vendor.lh.model_b"]
    # 同一个 class，不同 JSON enforce；加载结果不得保留旧 class.init。
    assert a["class"]["module"] == b["class"]["module"]
    assert a["class"]["module"].endswith(":JsonConfiguredDevice")
    assert "init" not in a["class"]
    assert "init" not in b["class"]
    assert a["init_param_enforce"]["channels"] == 8
    assert a["init_param_enforce"]["backend_params"]["port"] == 4008
    assert b["init_param_enforce"]["channels"] == 96
    assert b["init_param_enforce"]["backend_params"]["port"] == 4096
    # $ref 已展开：contracts/liquid_handler.yaml 的 action/status 已并入条目
    assert "setup" in a["class"]["action_value_mappings"]
    assert "initialized" in b["class"]["status_types"]


def test_inspect_package_uses_folder_registry_source(tmp_path):
    info = inspect_package(str(PKG), namespace=None, out_dir=str(tmp_path))

    assert sorted(info["devices"]) == ["vendor.lh.model_a", "vendor.lh.model_b"]
    assert info["class_namespace"] == "community.example_variant_pkg"

    by_id = {r["id"]: r for r in info["resources"]}
    source_a = by_id["vendor.lh.model_a"]["source_registry"]
    source_b = by_id["vendor.lh.model_b"]["source_registry"]
    assert "init" not in source_a["class"]
    assert "init" not in source_b["class"]
    assert source_a["init_param_enforce"]["channels"] == 8
    assert source_a["init_param_enforce"]["deck_name"] == "model-a-deck"
    assert source_b["init_param_enforce"]["channels"] == 96
    assert source_b["init_param_enforce"]["deck_name"] == "model-b-deck"

    assert by_id["vendor.lh.model_a"]["init_param_enforce"] == source_a["init_param_enforce"]
    assert by_id["vendor.lh.model_b"]["init_param_enforce"] == source_b["init_param_enforce"]
