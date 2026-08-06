"""包目录（PackageCatalog）Module 的公开 Interface。"""

from .compilers.python import compile_package_source
from .model import (
    PackageAsset,
    PackageCatalog,
    PackageCompileError,
    PackageDefinition,
    PackageDefinitionCatalog,
    PackageDiagnostic,
    PackageDistributionIdentity,
)
from .registry_snapshot import (
    RegistryActivationPlan,
    RegistryAsset,
    RegistrySnapshot,
    RegistrySnapshotError,
    compile_registry_snapshot,
)
from .sources import WorkspaceSource

__all__ = [
    "PackageAsset",
    "PackageCatalog",
    "PackageCompileError",
    "PackageDefinition",
    "PackageDefinitionCatalog",
    "PackageDiagnostic",
    "PackageDistributionIdentity",
    "RegistryActivationPlan",
    "RegistryAsset",
    "RegistrySnapshot",
    "RegistrySnapshotError",
    "WorkspaceSource",
    "compile_package_source",
    "compile_registry_snapshot",
]
