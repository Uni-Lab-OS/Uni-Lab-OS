"""工作流源码（Workflow Source）静态编译实现的遗留私有 import 兼容入口。"""

from .package_catalog.compilers.python.workflow import compile_workflow_definitions

__all__ = ["compile_workflow_definitions"]
