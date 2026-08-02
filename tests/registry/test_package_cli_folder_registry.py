"""package-management 支持文件夹式外部注册表的当前合同。

两个变体共享一个 Python class，使用 JSON ``init_param_enforce`` 区分；
``class.init`` 对象工厂 DSL 不得从 loaded/source_registry 重新出现。
"""

from pathlib import Path

from unilabos import package_manager
from unilabos.package_manager.legacy import (
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


def test_folder_registry_compatibility_is_not_a_public_package_catalog_api():
    assert not hasattr(package_manager, "read_external_registry_devices")
    assert not hasattr(package_manager, "read_registry_yaml_devices")
