"""``@device`` 工厂函数的纯静态 AST 合同。"""

from __future__ import annotations

from pathlib import Path

import pytest

from unilabos.registry.ast_registry_scanner import (
    DeviceFactoryScanError,
    _parse_file,
)


def _scan(tmp_path: Path, source: str) -> dict:
    """写入单模块设备包并返回唯一设备扫描结果。"""

    package = tmp_path / "factory_lab"
    package.mkdir()
    package.joinpath("__init__.py").write_text("", encoding="utf-8")
    module = package / "devices.py"
    module.write_text(source, encoding="utf-8")
    devices, resources = _parse_file(module, tmp_path)
    assert resources == []
    assert len(devices) == 1
    return devices[0]


@pytest.mark.parametrize(
    "annotation",
    (
        "CytomatDevice",
        "'CytomatDevice'",
        "DeviceAlias",
        "Annotated[CytomatDevice, 'catalog']",
    ),
)
def test_factory_uses_return_class_contract_and_preserves_first_parameter(
    tmp_path: Path,
    annotation: str,
) -> None:
    """工厂签名负责初始化，返回类负责动作/状态，且解析不导入源码。"""

    metadata = _scan(
        tmp_path,
        "from typing import Annotated\n"
        "from unilabos.registry.decorators import action, device, topic_config\n\n"
        "class BaseDevice:\n"
        "    @action(description='启动')\n"
        "    def start(self, speed: int = 1) -> None:\n"
        "        pass\n\n"
        "class CytomatDevice(BaseDevice):\n"
        "    @property\n"
        "    def temperature(self) -> float:\n"
        "        return 20.0\n\n"
        "DeviceAlias = CytomatDevice\n\n"
        "@device(id='cytomat', category=['incubator'], device_type='pylabrobot')\n"
        f"def make_cytomat(name: str, port: str = '') -> {annotation}:\n"
        "    return CytomatDevice()\n",
    )

    assert metadata["is_factory"] is True
    assert metadata["factory_module"] == "factory_lab.devices:make_cytomat"
    assert metadata["return_class_module"] == "factory_lab.devices:CytomatDevice"
    assert metadata["module"] == "factory_lab.devices:CytomatDevice"
    assert metadata["device_type"] == "pylabrobot"
    assert [item["name"] for item in metadata["init_params"]] == ["name", "port"]
    assert set(metadata["actions"]) == {"start"}
    assert set(metadata["status_properties"]) == {"temperature"}


@pytest.mark.parametrize(
    ("declaration", "expected_code"),
    (
        (
            "@device(id='bad', category=['test'])\n"
            "def make_bad(name: str):\n"
            "    return Device()\n",
            "device_factory_return_missing",
        ),
        (
            "@device(id='bad', category=['test'])\n"
            "def make_bad(name: str) -> Device | None:\n"
            "    return Device()\n",
            "device_factory_return_ambiguous",
        ),
        (
            "@device(id='bad', category=['test'])\n"
            "async def make_bad(name: str) -> Device:\n"
            "    return Device()\n",
            "device_factory_async_unsupported",
        ),
        (
            "@device(id='bad', category=['test'])\n"
            "def make_bad(name: str) -> MissingDevice:\n"
            "    return MissingDevice()\n",
            "device_factory_return_unresolved",
        ),
        (
            "@device(id='bad', category=['test'])\n"
            "def make_bad(name: str) -> Device:\n"
            "    if name:\n"
            "        return Device()\n"
            "    return OtherDevice()\n",
            "device_factory_contract_invalid",
        ),
    ),
)
def test_invalid_factory_contracts_fail_with_stable_codes(
    tmp_path: Path,
    declaration: str,
    expected_code: str,
) -> None:
    """不确定或异步工厂不能降级成宽松运行时导入。"""

    package = tmp_path / "factory_lab"
    package.mkdir()
    package.joinpath("__init__.py").write_text("", encoding="utf-8")
    module = package / "devices.py"
    module.write_text(
        "from unilabos.registry.decorators import device\n\n"
        "class Device:\n"
        "    pass\n\n"
        "class OtherDevice:\n"
        "    pass\n\n"
        + declaration,
        encoding="utf-8",
    )

    with pytest.raises(DeviceFactoryScanError) as caught:
        _parse_file(module, tmp_path)

    assert caught.value.code == expected_code


def test_device_decorator_accepts_safe_module_literal_catalog_constants(
    tmp_path: Path,
) -> None:
    """ids/id_meta 可共享模块级纯字面量，不导入或执行源码。"""

    package = tmp_path / "factory_lab"
    package.mkdir()
    package.joinpath("__init__.py").write_text("", encoding="utf-8")
    module = package / "devices.py"
    module.write_text(
        "from unilabos.registry.decorators import device\n\n"
        "DEVICE_IDS = ['virtual_a', 'virtual_b']\n"
        "DEVICE_META = {\n"
        "    'virtual_a': {'displayname': 'Virtual A', 'model': {'$ref': 'a'}},\n"
        "    'virtual_b': {'displayname': 'Virtual B'},\n"
        "}\n\n"
        "@device(ids=DEVICE_IDS, id_meta=DEVICE_META, category=['virtual'])\n"
        "class VirtualDevice:\n"
        "    pass\n",
        encoding="utf-8",
    )

    devices, resources = _parse_file(module, tmp_path)

    assert resources == []
    assert [item["device_id"] for item in devices] == ["virtual_a", "virtual_b"]
    assert devices[0]["displayname"] == "Virtual A"
    assert devices[0]["model"] == {"$ref": "a"}
