"""注册表快照（Registry Snapshot）的遗留 import 兼容入口。"""

from .package_catalog.registry_snapshot import (
    RegistryActivationPlan,
    RegistryAsset,
    RegistrySnapshot,
    RegistrySnapshotError,
    compile_registry_snapshot,
)

__all__ = [
    "RegistryActivationPlan",
    "RegistryAsset",
    "RegistrySnapshot",
    "RegistrySnapshotError",
    "compile_registry_snapshot",
]
