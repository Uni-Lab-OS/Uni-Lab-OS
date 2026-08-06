"""包目录编译的历史兼容入口；实现位于工作区运行时（Workspace Runtime）。"""

from .workspace_runtime.discovery import compile_package_source

__all__ = ["compile_package_source"]
