"""Registry 从目录化 YAML 加载当前 JSON-enforced 设备变体。

两个变体共享驱动类与 $ref 合同；初始化差异只存在于顶层
``init_param_enforce``，不得存在 ``class.init``。
"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from unilabos.registry.registry import Registry

FIX = Path(__file__).parent / "fixtures" / "external_variant_registry"


def test_registry_loads_multiple_variants_sharing_same_class():
    reg = Registry()  # singleton (needs unilabos_msgs -> run on full env / 4090)
    if reg._startup_executor is None:
        reg._startup_executor = ThreadPoolExecutor(max_workers=2)

    reg.load_device_types(FIX, complete_registry=False)  # DIR, not a single file

    a = reg.device_type_registry["vendor.lh.model_a"]
    b = reg.device_type_registry["vendor.lh.model_b"]

    assert a["class"]["module"].endswith(":JsonConfiguredDevice")
    assert b["class"]["module"].endswith(":JsonConfiguredDevice")
    assert a["implementation"]["variant"] == "model_a"
    assert b["implementation"]["variant"] == "model_b"
    assert "init" not in a["class"]
    assert "init" not in b["class"]
    assert a["init_param_enforce"] == {
        "backend_type": "mock",
        "backend_params": {"port": 4008},
        "deck_name": "model-a-deck",
        "channels": 8,
    }
    assert b["init_param_enforce"] == {
        "backend_type": "mock",
        "backend_params": {"port": 4096},
        "deck_name": "model-b-deck",
        "channels": 96,
    }
    # $ref expanded into the shared contract
    assert "setup" in a["class"]["action_value_mappings"]
    assert "initialized" in b["class"]["status_types"]
