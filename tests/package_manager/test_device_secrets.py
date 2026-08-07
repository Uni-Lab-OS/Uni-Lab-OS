"""设备初始化秘密引用的权限、幂等和失败关闭测试。"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from unilabos.package_manager.device_secrets import (
    DeviceSecretError,
    protect_device_configuration,
    resolve_device_configuration,
)
from unilabos.ros import initialize_device as initialize_device_module

_SCHEMA = {
    "type": "object",
    "required": ["endpoint", "password"],
    "properties": {
        "endpoint": {"type": "string"},
        "password": {
            "type": "string",
            "writeOnly": True,
            "x-unilab-secret": True,
        },
    },
    "additionalProperties": False,
}


def test_secret_reference_round_trip_reuses_identical_value(tmp_path: Path) -> None:
    """同一秘密重放必须复用引用，运行期解析只返回独立短生命周期字典。"""

    configuration = {
        "endpoint": "serial:///dev/ttyUSB0",
        "password": "设备密码-🔒",
    }
    first = protect_device_configuration(
        configuration,
        _SCHEMA,
        working_dir=tmp_path,
    )
    second = protect_device_configuration(
        configuration,
        _SCHEMA,
        working_dir=tmp_path,
        existing_configuration=first,
    )

    assert first == second
    assert first["password"] != "device-password"
    assert resolve_device_configuration(first, working_dir=tmp_path) == configuration
    assert first["password"] != "device-password"


def test_changed_secret_gets_new_reference_and_keeps_backup_readable(
    tmp_path: Path,
) -> None:
    """更新秘密必须创建新引用，旧引用继续支持设备图备份恢复。"""

    first = protect_device_configuration(
        {"endpoint": "serial://one", "password": "first-password"},
        _SCHEMA,
        working_dir=tmp_path,
    )
    second = protect_device_configuration(
        {"endpoint": "serial://one", "password": "second-password"},
        _SCHEMA,
        working_dir=tmp_path,
        existing_configuration=first,
    )

    assert first["password"] != second["password"]
    assert resolve_device_configuration(first, working_dir=tmp_path)["password"] == (
        "first-password"
    )
    assert resolve_device_configuration(second, working_dir=tmp_path)["password"] == (
        "second-password"
    )


@pytest.mark.skipif(os.name == "nt", reason="Windows 不提供 POSIX mode 语义")
def test_secret_resolution_rejects_group_readable_file(tmp_path: Path) -> None:
    """秘密文件一旦可被组或其他用户读取，驱动启动必须失败关闭。"""

    protected = protect_device_configuration(
        {"endpoint": "serial://one", "password": "device-password"},
        _SCHEMA,
        working_dir=tmp_path,
    )
    secret_id = protected["password"]["$unilab_secret"]["id"]
    secret_path = tmp_path / "device-secrets" / "v1" / secret_id
    secret_path.chmod(0o640)

    with pytest.raises(DeviceSecretError, match="权限过宽"):
        resolve_device_configuration(protected, working_dir=tmp_path)


def test_secret_reference_contract_rejects_unknown_fields(tmp_path: Path) -> None:
    """设备图秘密引用是封闭合同，附加路径或值字段不能参与解析。"""

    invalid = {
        "password": {
            "$unilab_secret": {
                "schema_version": "device-secret-ref/v1",
                "id": "b579c82a-46ae-4bc5-9ddb-4c4d599f5661",
            },
            "value": "must-not-be-used",
        }
    }

    with pytest.raises(DeviceSecretError, match="未知字段"):
        resolve_device_configuration(invalid, working_dir=tmp_path)


def test_device_initialization_resolves_secret_only_for_driver_constructor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """驱动构造函数必须收到明文，设备图 Resource 投影始终保留秘密引用。"""

    protected = protect_device_configuration(
        {"endpoint": "serial://one", "password": "device-password"},
        _SCHEMA,
        working_dir=tmp_path,
    )
    resource = SimpleNamespace(
        res_content=SimpleNamespace(
            klass="community.review_lab.pump",
            uuid="4598df11-6b39-421e-a3ef-cc68c6a7191e",
            config=protected,
        )
    )
    captured: dict[str, object] = {}

    class _WrappedDevice:
        """记录初始化参数的最小 ROS2 包装替身。"""

        def __init__(self, **kwargs: object) -> None:
            """保存驱动构造参数供本测试核对。

            ``kwargs`` 是 OS 包装层传给设备节点的完整构造参数。函数无返回值。
            """

            captured.update(kwargs)

    def _resolve_definition(registry: object, identity: str):
        """返回固定测试定义；参数是注册表与 FQID，返回规范身份和定义元数据。"""

        del registry
        return (
            identity,
            {
                "class": {"module": "review_lab.device:Pump", "type": "python"},
                "init_param_enforce": None,
            },
        )

    def _get_class(module: str) -> object:
        """忽略固定测试模块名并返回占位驱动类。"""

        del module
        return object

    def _wrap_device(*args: object, **kwargs: object):
        """接收包装配置并返回可记录驱动参数的测试设备类。"""

        del args, kwargs
        return _WrappedDevice

    def _merge_configuration(
        configuration: dict[str, object],
        enforce: object,
    ) -> dict[str, object]:
        """保持已解析配置不变；参数为驱动配置与强制覆盖，返回配置副本。"""

        del enforce
        return configuration

    monkeypatch.setattr(
        initialize_device_module,
        "resolve_registry_definition",
        _resolve_definition,
    )
    monkeypatch.setattr(
        initialize_device_module.default_manager,
        "get_class",
        _get_class,
    )
    monkeypatch.setattr(
        initialize_device_module,
        "ros2_device_node",
        _wrap_device,
    )
    monkeypatch.setattr(
        initialize_device_module,
        "merge_init_param_enforce",
        _merge_configuration,
    )
    monkeypatch.setattr(
        initialize_device_module.BasicConfig,
        "working_dir",
        str(tmp_path),
    )

    result = initialize_device_module.initialize_device_from_dict(
        "local_pump_1",
        resource,
    )

    assert isinstance(result, _WrappedDevice)
    assert captured["driver_params"] == {
        "endpoint": "serial://one",
        "password": "device-password",
    }
    assert resource.res_content.config == protected
