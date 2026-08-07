"""领域包发现、构建、分发、消费与工作区启动的稳定接口。"""

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
from .sources import (
    CachedArchiveSource,
    InstalledDistributionSource,
    PackageSource,
    WorkspaceSource,
)
from .workspace_material_models import (
    WorkspaceMaterialModelAsset,
    WorkspaceMaterialModelCatalog,
    compile_workspace_material_models,
)
from .workspace_material_shapes import compile_workspace_material_shapes
from .workspace_startup import (
    WorkspaceStartupPlan,
    compile_workspace_startup,
    prepare_workspace_startup,
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
    "WorkspaceMaterialModelAsset",
    "WorkspaceMaterialModelCatalog",
    "WorkspaceSource",
    "WorkspaceStartupPlan",
    "compile_package_source",
    "compile_workspace_material_models",
    "compile_workspace_material_shapes",
    "compile_workspace_startup",
    "normalize_distribution_name",
    "prepare_workspace_startup",
]
