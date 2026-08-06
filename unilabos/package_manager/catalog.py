"""包目录（PackageCatalog）模型的遗留 import 兼容入口。"""

from .package_catalog.model import (
    PackageAsset,
    PackageCatalog,
    PackageCompileError,
    PackageDefinition,
    PackageDefinitionCatalog,
    PackageDiagnostic,
    PackageDistributionIdentity,
)

__all__ = [
    "PackageAsset",
    "PackageCatalog",
    "PackageCompileError",
    "PackageDefinition",
    "PackageDefinitionCatalog",
    "PackageDiagnostic",
    "PackageDistributionIdentity",
]
