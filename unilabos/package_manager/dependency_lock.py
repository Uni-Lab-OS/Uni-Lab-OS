"""包分发（Package Distribution）依赖锁的遗留兼容 import。"""

from .package_distribution import (
    DEPENDENCY_DECLARATION_FILE,
    DEPENDENCY_LOCK_FILE,
    LockedPackage,
    PackageDependencyError,
    PackageDependencyLock,
    PackageDependencyManager,
    load_locked_package_catalogs,
    load_locked_package_sources,
)

__all__ = [
    "DEPENDENCY_DECLARATION_FILE",
    "DEPENDENCY_LOCK_FILE",
    "LockedPackage",
    "PackageDependencyError",
    "PackageDependencyLock",
    "PackageDependencyManager",
    "load_locked_package_catalogs",
    "load_locked_package_sources",
]
