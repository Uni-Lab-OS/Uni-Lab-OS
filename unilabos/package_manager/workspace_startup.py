"""工作区启动发现的历史兼容入口；实现位于工作区运行时（Workspace Runtime）。"""

from .workspace_runtime.discovery import (
    WorkspaceStartupPlan,
    compile_package_source,
    compile_workspace_startup,
    normalize_distribution_name,
    prepare_workspace_startup,
    project_catalog_startup_plan,
    read_package_root,
)

__all__ = [
    "WorkspaceStartupPlan",
    "compile_package_source",
    "compile_workspace_startup",
    "normalize_distribution_name",
    "prepare_workspace_startup",
    "project_catalog_startup_plan",
    "read_package_root",
]
