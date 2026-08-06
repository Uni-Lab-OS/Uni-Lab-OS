"""包目录（PackageCatalog）分层 Module 的公开兼容合同。"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

from tests.package_manager.test_package_catalog_compiler import _write_package


def test_new_package_catalog_interface_compiles_the_same_catalog(
    tmp_path: Path,
) -> None:
    """新 Module 接收已解析输入并产生与遗留入口相同的包目录（PackageCatalog）。

    参数：``tmp_path`` 提供唯一隔离软件包来源。
    返回：无；断言新旧公开 Interface 产生同类对象和相同规范字节。
    异常：新路径、公开编译入口或输入反转接缝缺失时测试失败。
    """

    from unilabos.package_manager import (
        WorkspaceSource,
    )
    from unilabos.package_manager import (
        compile_package_source as compile_legacy_package_source,
    )
    from unilabos.package_manager.package_catalog import (
        PackageCatalog,
    )
    from unilabos.package_manager.package_catalog import (
        compile_package_source as compile_layered_package_source,
    )
    from unilabos.package_manager.workspace_startup import compile_workspace_startup

    # ``workspace_root`` 是新旧编译入口共同观察的唯一可编辑包来源根。
    workspace_root = tmp_path / "workspace"
    _write_package(workspace_root)
    # ``source`` 固定文件读取权威；``startup_plan`` 是根编排层交给纯编译层的输入。
    source = WorkspaceSource(workspace_root)
    startup_plan = compile_workspace_startup(source)

    # ``legacy_catalog`` 是根兼容编排入口产生的行为基准包目录（PackageCatalog）。
    legacy_catalog = compile_legacy_package_source(
        source,
        startup_plan=startup_plan,
    )
    # ``layered_catalog`` 是新 Module 对同一冻结输入产生的包目录（PackageCatalog）。
    layered_catalog = compile_layered_package_source(
        source,
        startup_plan=startup_plan,
    )

    assert isinstance(layered_catalog, PackageCatalog)
    assert type(layered_catalog) is type(legacy_catalog)
    assert layered_catalog.to_canonical_bytes() == legacy_catalog.to_canonical_bytes()


def test_legacy_catalog_imports_retain_the_new_public_type_identities() -> None:
    """遗留根入口和历史模块继续指向新 Module 的同一公开类型。

    参数：无。
    返回：无；断言包目录（PackageCatalog）、来源与注册表快照
    （Registry Snapshot）没有因路径迁移产生平行类型。
    异常：兼容 wrapper 复制实现或新 Interface 遗漏公开类型时测试失败。
    """

    # 三组 import 别名分别捕获根门面、历史模块和新 Module 的实际类型对象身份。
    from unilabos.package_manager import PackageCatalog as root_package_catalog
    from unilabos.package_manager import WorkspaceSource as root_workspace_source
    from unilabos.package_manager.catalog import (
        PackageCatalog as legacy_package_catalog,
    )
    from unilabos.package_manager.package_catalog import (
        PackageCatalog,
        RegistrySnapshot,
        WorkspaceSource,
    )
    from unilabos.package_manager.registry_snapshot import (
        RegistrySnapshot as legacy_registry_snapshot,
    )
    from unilabos.package_manager.sources import (
        WorkspaceSource as legacy_workspace_source,
    )

    assert PackageCatalog is root_package_catalog is legacy_package_catalog
    assert WorkspaceSource is root_workspace_source is legacy_workspace_source
    assert RegistrySnapshot is legacy_registry_snapshot


def test_legacy_compiler_preserves_invalid_source_diagnostic(tmp_path: Path) -> None:
    """根兼容编译入口继续把工作区发现失败归一为既有结构化诊断。

    参数：``tmp_path`` 提供缺少项目清单的隔离工作区来源。
    返回：无；断言遗留入口仍抛出 ``package_source_invalid``。
    异常：输入反转使高层解析异常泄漏为原始 ``ValueError`` 时测试失败。
    """

    import pytest

    from unilabos.package_manager import (
        PackageCompileError,
        WorkspaceSource,
        compile_package_source,
    )

    # ``invalid_workspace_root`` 是存在但缺少 pyproject.toml 的非法包来源。
    invalid_workspace_root = tmp_path / "invalid-workspace"
    invalid_workspace_root.mkdir()

    # ``caught`` 保存根兼容入口对非法来源产生的结构化编译诊断。
    with pytest.raises(PackageCompileError) as caught:
        compile_package_source(WorkspaceSource(invalid_workspace_root))

    assert [item.code for item in caught.value.diagnostics] == [
        "package_source_invalid"
    ]


def test_package_catalog_module_has_no_reverse_dependency_on_parent_layers() -> None:
    """包目录（PackageCatalog）Module 不反向依赖根门面或后续生命周期 Module。

    参数：无。
    返回：无；逐个解析新 Module 的 import，并断言只依赖自身或更底层能力。
    异常：出现遗留根实现、包分发、工作区运行时或候选驱动运行时依赖时测试失败。
    """

    # ``module_root`` 是包目录（PackageCatalog）新 Module 的唯一源码边界。
    module_root = (
        Path(__file__).resolve().parents[2]
        / "unilabos"
        / "package_manager"
        / "package_catalog"
    )
    # ``forbidden_prefixes`` 标识会使低层编译重新依赖高层生命周期的导入方向。
    forbidden_prefixes = (
        "unilabos.package_manager.catalog",
        "unilabos.package_manager.compiler",
        "unilabos.package_manager.registry_snapshot",
        "unilabos.package_manager.sources",
        "unilabos.package_manager._registry_catalog",
        "unilabos.package_manager._workflow_catalog",
        "unilabos.package_manager.workspace_startup",
        "unilabos.package_manager.package_distribution",
        "unilabos.package_manager.workspace_runtime",
        "unilabos.package_manager.driver_runtime",
    )
    # ``violations`` 记录源码路径、行号和越层导入身份，便于直接定位依赖反转。
    violations: list[str] = []
    for source_file in sorted(module_root.rglob("*.py")):
        # ``relative_file`` 是守卫报告使用的 Module 内路径，不绑定工作树绝对位置。
        relative_file = source_file.relative_to(module_root)
        # ``module_parts`` 从源码相对路径恢复 Python Module 身份的组成部分。
        module_parts = list(relative_file.with_suffix("").parts)
        if module_parts[-1] == "__init__":
            module_parts.pop()
        # ``package_name`` 是解析相对 import 所需的当前 Python 包身份。
        package_name = ".".join(
            ["unilabos", "package_manager", "package_catalog", *module_parts[:-1]]
        )
        if relative_file.name == "__init__.py":
            package_name = ".".join(
                ["unilabos", "package_manager", "package_catalog", *module_parts]
            )
        # ``syntax_tree`` 只用于检查 import 依赖，不执行被审查 Module。
        syntax_tree = ast.parse(source_file.read_text(encoding="utf-8"))
        for node in ast.walk(syntax_tree):
            if isinstance(node, ast.Import):
                # ``imported_names`` 是普通 import 声明的完整绝对 Module 身份集合。
                imported_names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                # ``imported_name`` 先保留源码声明，再结合当前包解析为绝对身份。
                imported_name = node.module or ""
                if node.level:
                    imported_name = importlib.util.resolve_name(
                        "." * node.level + imported_name,
                        package_name,
                    )
                imported_names = [imported_name]
            else:
                continue
            for imported_name in imported_names:
                # 注册表快照（Registry Snapshot）的历史 ``publish`` 方法只保留
                # 一个函数内兼容桥；新运行时分层测试会精确限定其父函数和符号。
                legacy_publish_bridge = (
                    relative_file.as_posix() == "registry_snapshot.py"
                    and isinstance(node, ast.ImportFrom)
                    and imported_name
                    == "unilabos.package_manager.workspace_runtime.activation"
                    and [alias.name for alias in node.names]
                    == ["publish_registry_snapshot"]
                )
                if legacy_publish_bridge:
                    continue
                if (
                    imported_name == "unilabos.package_manager"
                    or imported_name.startswith(forbidden_prefixes)
                ):
                    violations.append(f"{relative_file}:{node.lineno}:{imported_name}")

    assert violations == []
