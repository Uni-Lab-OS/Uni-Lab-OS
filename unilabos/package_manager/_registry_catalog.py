"""注册表（Registry）静态编译实现的遗留私有 import 兼容入口。"""

from .package_catalog.compilers.python.registry import compile_registry_definitions

__all__ = ["compile_registry_definitions"]
