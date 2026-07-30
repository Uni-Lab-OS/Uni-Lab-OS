"""社区设备使用完整命名空间实体键，不经过 alias 桥接。"""

from unilabos.registry.registry import Registry


def test_community_device_is_not_registered_under_stripped_alias(tmp_path):
    package_dir = tmp_path / "acme_devices"
    package_dir.mkdir()
    (package_dir / "driver.py").write_text(
        """
from unilabos.registry.decorators import device


@device(id="direct_device", category=["test"])
class DirectCommunityDevice:
    pass
""",
        encoding="utf-8",
    )

    registry = Registry()
    registry.setup(
        devices_dirs=[str(package_dir)],
        external_only=True,
        community_namespaces={
            str(package_dir.resolve()): "community.acme",
        },
    )

    assert "community.acme.direct_device" in registry.device_type_registry
    assert "acme.direct_device" not in registry.device_type_registry
