"""工作区（Workspace）和软件包目录（PackageCatalog）的公开接口。"""

from .catalog import PackageCatalog, PackageCompileError
from .cli import PackageCLIError, cmd_package, register_package_subcommands
from .compiler import compile_package_source
from .dependency_lock import (
    LockedPackage,
    PackageDependencyError,
    PackageDependencyLock,
    PackageDependencyManager,
    load_locked_package_catalogs,
)
from .inspection import inspect_package
from .product_lifecycle import (
    PreparedWorkspaceProductGeneration,
    WorkspaceGenerationChangedError,
    WorkspaceProductLifecycle,
    close_workspace_product_lifecycle,
    compose_workspace_product_lifecycle,
    get_workspace_product_lifecycle,
    install_workspace_product_lifecycle,
    prepare_stable_workspace_product_generation,
)
from .project_metadata import PackageProject, parse_project_metadata
from .publication import upload_package
from .refresh_coordinator import (
    StableWorkspaceGenerationMonitor,
    WorkspaceRefreshCoordinator,
)
from .registry_snapshot import (
    RegistryActivationPlan,
    RegistrySnapshot,
    RegistrySnapshotError,
    compile_registry_snapshot,
)
from .runtime_activation import (
    WorkspaceRegistryRuntime,
    prepare_workspace_registry_runtime,
)
from .sources import WorkspaceSource
from .workspace_file_monitor import StableWorkspaceFileMonitor
from .workspace_material_shapes import (
    compile_catalog_material_shapes,
    compile_workspace_material_shapes,
)
from .workspace_runtime import (
    WorkspaceGenerationPublisher,
    WorkspaceInputGeneration,
    WorkspacePackageRuntime,
    WorkspaceRefreshResult,
    WorkspaceRuntimeStatus,
)
from .workspace_startup import (
    WorkspaceStartupPlan,
    compile_workspace_startup,
    prepare_workspace_startup,
)

__all__ = [
    "LockedPackage",
    "PackageCLIError",
    "PackageCatalog",
    "PackageCompileError",
    "PackageDependencyError",
    "PackageDependencyLock",
    "PackageDependencyManager",
    "PackageProject",
    "PreparedWorkspaceProductGeneration",
    "RegistryActivationPlan",
    "RegistrySnapshot",
    "RegistrySnapshotError",
    "StableWorkspaceFileMonitor",
    "StableWorkspaceGenerationMonitor",
    "WorkspaceGenerationChangedError",
    "WorkspaceGenerationPublisher",
    "WorkspaceInputGeneration",
    "WorkspacePackageRuntime",
    "WorkspaceProductLifecycle",
    "WorkspaceRefreshCoordinator",
    "WorkspaceRefreshResult",
    "WorkspaceRegistryRuntime",
    "WorkspaceRuntimeStatus",
    "WorkspaceSource",
    "WorkspaceStartupPlan",
    "close_workspace_product_lifecycle",
    "cmd_package",
    "compile_catalog_material_shapes",
    "compile_package_source",
    "compile_registry_snapshot",
    "compile_workspace_material_shapes",
    "compile_workspace_startup",
    "compose_workspace_product_lifecycle",
    "get_workspace_product_lifecycle",
    "inspect_package",
    "install_workspace_product_lifecycle",
    "load_locked_package_catalogs",
    "parse_project_metadata",
    "prepare_stable_workspace_product_generation",
    "prepare_workspace_registry_runtime",
    "prepare_workspace_startup",
    "register_package_subcommands",
    "upload_package",
]
