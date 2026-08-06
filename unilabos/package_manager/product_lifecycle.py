"""产品生命周期的历史兼容入口；实现位于工作区运行时（Workspace Runtime）。"""

from .workspace_runtime.lifecycle import (
    PreparedWorkspaceProductGeneration,
    WorkspaceGenerationChangedError,
    WorkspaceProductLifecycle,
    close_workspace_product_lifecycle,
    compose_workspace_product_lifecycle,
    get_workspace_product_lifecycle,
    install_workspace_product_lifecycle,
    prepare_stable_workspace_product_generation,
)

__all__ = [
    "PreparedWorkspaceProductGeneration",
    "WorkspaceGenerationChangedError",
    "WorkspaceProductLifecycle",
    "close_workspace_product_lifecycle",
    "compose_workspace_product_lifecycle",
    "get_workspace_product_lifecycle",
    "install_workspace_product_lifecycle",
    "prepare_stable_workspace_product_generation",
]
