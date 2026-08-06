"""运行激活的历史兼容入口；实现位于工作区运行时（Workspace Runtime）。"""

from .workspace_runtime.activation import (
    WorkspaceRegistryRuntime,
    prepare_workspace_registry_runtime,
    publish_registry_snapshot,
)

__all__ = [
    "WorkspaceRegistryRuntime",
    "prepare_workspace_registry_runtime",
    "publish_registry_snapshot",
]
