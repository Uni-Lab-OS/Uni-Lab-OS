"""根工作区编排到包目录（PackageCatalog）纯编译器的兼容入口。"""

from __future__ import annotations

from .package_catalog.compilers.python import (
    compile_package_source as compile_python_package_source,
)
from .package_catalog.model import (
    PackageCatalog,
    PackageCompileError,
    PackageDiagnostic,
)
from .package_catalog.sources import WorkspaceSource
from .workspace_startup import WorkspaceStartupPlan, compile_workspace_startup


def compile_package_source(
    source: WorkspaceSource,
    *,
    startup_plan: WorkspaceStartupPlan | None = None,
) -> PackageCatalog:
    """解析工作区启动输入后调用 Python 包目录（PackageCatalog）纯编译器。

    参数：``source`` 是显式授权的工作区来源；``startup_plan`` 是可选的同来源
    已冻结启动输入，产品启动传入后不会再次读取清单。
    返回：完整校验且不可变的包目录（PackageCatalog）。
    异常：来源发现失败或静态定义无效时保持既有 ``PackageCompileError`` 行为。
    """

    if not isinstance(source, WorkspaceSource):
        raise TypeError("source 必须是 WorkspaceSource")
    # ``resolved_startup_plan`` 是根编排层唯一允许解析的工作区启动输入；发现失败
    # 保持历史结构化诊断，不能让输入反转把原始文件错误泄漏给调用者。
    try:
        resolved_startup_plan = startup_plan or compile_workspace_startup(source)
    except (TypeError, ValueError) as error:
        raise PackageCompileError(
            (
                PackageDiagnostic(
                    code="package_source_invalid",
                    message="软件包来源或项目元数据无效",
                ),
            )
        ) from error
    return compile_python_package_source(
        source,
        startup_plan=resolved_startup_plan,
    )


__all__ = ["compile_package_source"]
