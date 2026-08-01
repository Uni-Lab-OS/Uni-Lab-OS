"""领域包发现、构建、分发与消费的稳定 Interface。"""

from .assets import PackageAssetResolver
from .catalog import (
    DefinitionCatalog,
    DefinitionRecord,
    DistributionIdentity,
    PackageAsset,
    PackageCatalog,
    PackageCompileError,
    PackageDiagnostic,
)
from .compiler import compile_package_source, normalize_distribution_name
from .sources import (
    CachedArchiveSource,
    InstalledDistributionSource,
    PackageSource,
    WorkspaceSource,
)

__all__ = [
    "CachedArchiveSource",
    "DefinitionCatalog",
    "DefinitionRecord",
    "DistributionIdentity",
    "InstalledDistributionSource",
    "PackageAsset",
    "PackageAssetResolver",
    "PackageCatalog",
    "PackageCompileError",
    "PackageDiagnostic",
    "PackageSource",
    "WorkspaceSource",
    "compile_package_source",
    "normalize_distribution_name",
]
