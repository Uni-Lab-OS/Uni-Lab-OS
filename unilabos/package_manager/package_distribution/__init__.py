"""包分发（Package Distribution）的公开 Interface。"""

from .acquisition import PackageAcquirerPort, acquire_package
from .adapters import (
    HttpClientPublicationAdapter,
    LegacyTemplateBackendAdapter,
    PackageBuilder,
    PublicationPort,
    install_package,
    publish_build,
    upload_package,
)
from .build import (
    PackageBuildArtifact,
    PackageBuildError,
    audit_package_wheel,
    build_workspace_package,
)
from .cache import PackageCache
from .dependency_manager import (
    PackageDependencyManager,
    load_locked_package_catalogs,
    load_locked_package_sources,
)
from .inspection import CatalogCompiler, inspect_package
from .models import (
    DEPENDENCY_DECLARATION_FILE,
    DEPENDENCY_LOCK_FILE,
    LockedPackage,
    PackageDependencyError,
    PackageDependencyLock,
    ResolvedPackageSource,
)
from .publication import PackagePublisherPort, publish_package_artifact
from .transfer_models import PackageDownloadRequest, PackageReleaseDescriptor

__all__ = [
    "DEPENDENCY_DECLARATION_FILE",
    "DEPENDENCY_LOCK_FILE",
    "CatalogCompiler",
    "HttpClientPublicationAdapter",
    "LegacyTemplateBackendAdapter",
    "LockedPackage",
    "PackageAcquirerPort",
    "PackageBuildArtifact",
    "PackageBuildError",
    "PackageBuilder",
    "PackageCache",
    "PackageDependencyError",
    "PackageDependencyLock",
    "PackageDependencyManager",
    "PackageDownloadRequest",
    "PackagePublisherPort",
    "PackageReleaseDescriptor",
    "PublicationPort",
    "ResolvedPackageSource",
    "acquire_package",
    "audit_package_wheel",
    "build_workspace_package",
    "inspect_package",
    "install_package",
    "load_locked_package_catalogs",
    "load_locked_package_sources",
    "publish_build",
    "publish_package_artifact",
    "upload_package",
]
