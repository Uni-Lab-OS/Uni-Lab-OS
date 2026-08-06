"""运行代差异分类的历史兼容入口；实现位于工作区运行时（Workspace Runtime）。"""

from .workspace_runtime.generation import candidate_fingerprint, restart_reasons

__all__ = ["candidate_fingerprint", "restart_reasons"]
