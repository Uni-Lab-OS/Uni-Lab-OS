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

# ``compile_package_source`` 仍由 ``compiler`` 拥有；当前源码树与
# ``source_discovery.load_editable_package_manifest`` 尚未对齐，避免在包导入期强拉。


def compile_package_source(*args, **kwargs):
    from .compiler import compile_package_source as _compile

    return _compile(*args, **kwargs)


def normalize_distribution_name(*args, **kwargs):
    from .compiler import normalize_distribution_name as _normalize

    return _normalize(*args, **kwargs)


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
