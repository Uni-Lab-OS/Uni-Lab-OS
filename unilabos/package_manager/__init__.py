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
from .device_package import (
    DevicePackageDownloadResult,
    DevicePackageError,
    configuration_schema_for_definition,
    device_definition_from_catalog,
    download_device_package,
    validate_configuration_for_definition,
)
from .device_provisioning import (
    DeviceGraphMutationResult,
    DeviceProvisioningError,
    remove_device_instance,
    restore_device_graph,
    stage_device_instance,
    update_device_instance,
)
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
    "DevicePackageDownloadResult",
    "DevicePackageError",
    "DeviceGraphMutationResult",
    "DeviceProvisioningError",
    "InstalledDistributionSource",
    "PackageAsset",
    "PackageAssetResolver",
    "PackageCatalog",
    "PackageCompileError",
    "PackageDiagnostic",
    "PackageSource",
    "WorkspaceSource",
    "compile_package_source",
    "configuration_schema_for_definition",
    "device_definition_from_catalog",
    "download_device_package",
    "normalize_distribution_name",
    "remove_device_instance",
    "restore_device_graph",
    "stage_device_instance",
    "update_device_instance",
    "validate_configuration_for_definition",
]
