"""工作区（Workspace）和软件包目录（PackageCatalog）的公开接口。"""

from .catalog import PackageCatalog, PackageCompileError
from .cli import PackageCLIError, cmd_package
from .compiler import compile_package_source
from .inspection import inspect_package
from .installation import install_package
from .project_metadata import PackageProject, parse_project_metadata
from .publication import upload_package
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
from .workspace_material_shapes import (
    compile_catalog_material_shapes,
    compile_workspace_material_shapes,
)
from .workspace_startup import (
    WorkspaceStartupPlan,
    compile_workspace_startup,
    prepare_workspace_startup,
)

__all__ = [
    "PackageCLIError",
    "PackageCatalog",
    "PackageCompileError",
    "PackageProject",
    "RegistryActivationPlan",
    "RegistrySnapshot",
    "RegistrySnapshotError",
    "WorkspaceRegistryRuntime",
    "WorkspaceSource",
    "WorkspaceStartupPlan",
    "cmd_package",
    "compile_package_source",
    "compile_catalog_material_shapes",
    "compile_registry_snapshot",
    "compile_workspace_material_shapes",
    "compile_workspace_startup",
    "inspect_package",
    "install_package",
    "parse_project_metadata",
    "prepare_workspace_registry_runtime",
    "prepare_workspace_startup",
    "upload_package",
]
