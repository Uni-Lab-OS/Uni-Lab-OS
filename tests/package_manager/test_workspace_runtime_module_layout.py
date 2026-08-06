"""工作区运行时（Workspace Runtime）分层 Module 的公开兼容合同。"""

from __future__ import annotations

import ast
import importlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

from tests.package_manager.test_registry_snapshot_activation import (
    _compile_snapshot,
    _write_registry_workspace,
)


def test_workspace_runtime_exposes_complete_public_interface() -> None:
    """新 Module 集中公开发现、监视、代际、激活和生命周期 Interface。

    参数：无。
    返回：无；断言调用者只需导入一个工作区运行时（Workspace Runtime）Module。
    异常：新分层入口缺失或遗漏既有公开能力时测试失败。
    """

    # ``runtime_module`` 是工作区运行时（Workspace Runtime）唯一公开 Module 身份。
    runtime_module = importlib.import_module(
        "unilabos.package_manager.workspace_runtime"
    )
    # ``expected_members`` 是调用者不应再从历史平铺文件拼装的稳定 Interface。
    expected_members = {
        "StableWorkspaceFileMonitor",
        "StableWorkspaceGenerationMonitor",
        "WorkspaceGenerationPublisher",
        "WorkspaceInputGeneration",
        "WorkspacePackageRuntime",
        "WorkspaceProductLifecycle",
        "WorkspaceRefreshCoordinator",
        "WorkspaceRefreshResult",
        "WorkspaceRegistryRuntime",
        "WorkspaceRuntimeStatus",
        "WorkspaceSource",
        "WorkspaceStartupPlan",
        "compile_package_source",
        "compile_workspace_startup",
        "prepare_workspace_registry_runtime",
        "prepare_workspace_startup",
        "publish_registry_snapshot",
    }

    assert expected_members <= set(runtime_module.__all__)
    assert all(hasattr(runtime_module, member) for member in expected_members)


def test_legacy_workspace_runtime_imports_keep_public_object_identity() -> None:
    """根门面与历史模块继续指向工作区运行时（Workspace Runtime）同一对象。

    参数：无。
    返回：无；断言兼容入口只重导出，不保留第二套实现或状态权威。
    异常：任一历史入口复制类或函数时测试失败。
    """

    from unilabos.package_manager import (
        WorkspacePackageRuntime as root_package_runtime,
    )
    from unilabos.package_manager import (
        WorkspaceRegistryRuntime as root_registry_runtime,
    )
    from unilabos.package_manager import (
        compile_package_source as root_compile_package_source,
    )
    from unilabos.package_manager.compiler import (
        compile_package_source as legacy_compile_package_source,
    )
    from unilabos.package_manager.product_lifecycle import (
        WorkspaceProductLifecycle as legacy_product_lifecycle,
    )
    from unilabos.package_manager.refresh_coordinator import (
        WorkspaceRefreshCoordinator as legacy_refresh_coordinator,
    )
    from unilabos.package_manager.runtime_activation import (
        WorkspaceRegistryRuntime as legacy_registry_runtime,
    )
    from unilabos.package_manager.runtime_diff import (
        candidate_fingerprint as legacy_candidate_fingerprint,
    )
    from unilabos.package_manager.workspace_file_monitor import (
        StableWorkspaceFileMonitor as legacy_file_monitor,
    )
    from unilabos.package_manager.workspace_runtime import (
        StableWorkspaceFileMonitor,
        WorkspacePackageRuntime,
        WorkspaceProductLifecycle,
        WorkspaceRefreshCoordinator,
        WorkspaceRegistryRuntime,
        candidate_fingerprint,
        compile_package_source,
    )

    assert compile_package_source is root_compile_package_source
    assert compile_package_source is legacy_compile_package_source
    assert WorkspaceRegistryRuntime is root_registry_runtime
    assert WorkspaceRegistryRuntime is legacy_registry_runtime
    assert WorkspacePackageRuntime is root_package_runtime
    assert WorkspaceProductLifecycle is legacy_product_lifecycle
    assert WorkspaceRefreshCoordinator is legacy_refresh_coordinator
    assert StableWorkspaceFileMonitor is legacy_file_monitor
    assert candidate_fingerprint is legacy_candidate_fingerprint


def test_registry_snapshot_publication_is_owned_by_workspace_runtime(
    tmp_path: Path,
) -> None:
    """新发布 Interface 与历史快照方法都原子发布完整注册表快照。

    参数：``tmp_path`` 提供含设备和资源定义的隔离工作区。
    返回：无；断言两条兼容调用都保留内置定义并发布同一完整候选代。
    异常：运行时发布职责缺失、历史桥失效或发生部分发布时测试失败。
    """

    from unilabos.package_manager.workspace_runtime import publish_registry_snapshot

    # ``source`` 是一次性编译的包目录（PackageCatalog）授权来源。
    source = _write_registry_workspace(
        tmp_path / "workspace",
        distribution="runtime-publish-lab",
        import_package="runtime_publish_lab",
        device_ids=("reactor",),
        resource_ids=("plate",),
    )
    # ``snapshot`` 是新旧发布入口必须共同消费的不可变候选代。
    snapshot = _compile_snapshot(source)
    # 两个 ``registry`` 分别观察新 Interface 与遗留方法的等价结果。
    direct_registry = SimpleNamespace(
        device_type_registry={"host_node": {"source": "builtin"}},
        resource_type_registry={"builtin_plate": {"source": "builtin"}},
    )
    legacy_registry = SimpleNamespace(
        device_type_registry={"host_node": {"source": "builtin"}},
        resource_type_registry={"builtin_plate": {"source": "builtin"}},
    )

    publish_registry_snapshot(snapshot, direct_registry)
    snapshot.publish(legacy_registry)

    assert direct_registry.device_type_registry == legacy_registry.device_type_registry
    assert (
        direct_registry.resource_type_registry == legacy_registry.resource_type_registry
    )
    assert "community.runtime_publish_lab.reactor" in (
        direct_registry.device_type_registry
    )
    assert "community.runtime_publish_lab.plate" in (
        direct_registry.resource_type_registry
    )


def test_product_callers_depend_directly_on_workspace_runtime_module() -> None:
    """产品调用者依赖新工作区运行时（Workspace Runtime），不穿过历史 wrapper。

    参数：无。
    返回：无；断言根门面、投影编译器和产品组合根采用新 Module 路径。
    异常：内部调用者继续引用历史平铺实现时测试失败。
    """

    # ``repository_root`` 是解析 OS 产品调用者导入方向的源码根。
    repository_root = Path(__file__).resolve().parents[2]
    # ``expected_imports`` 固定每个产品调用者应采用的新分层 Module 身份。
    expected_imports = {
        "unilabos/package_manager/__init__.py": {
            "unilabos.package_manager.workspace_runtime",
        },
        "unilabos/package_manager/inspection.py": {
            "unilabos.package_manager.workspace_runtime.discovery",
        },
        "unilabos/package_manager/workspace_material_models.py": {
            "unilabos.package_manager.workspace_runtime.discovery",
        },
        "unilabos/package_manager/workspace_material_shapes.py": {
            "unilabos.package_manager.workspace_runtime.discovery",
        },
        "unilabos/app/workspace_package_bootstrap.py": {
            "unilabos.package_manager.workspace_runtime",
        },
    }
    # ``legacy_modules`` 只能保留给外部兼容调用，产品实现不得继续依赖。
    legacy_modules = {
        "unilabos.package_manager.compiler",
        "unilabos.package_manager.product_lifecycle",
        "unilabos.package_manager.refresh_coordinator",
        "unilabos.package_manager.runtime_activation",
        "unilabos.package_manager.runtime_diff",
        "unilabos.package_manager.workspace_file_monitor",
        "unilabos.package_manager.workspace_startup",
    }

    for relative_path, required_modules in expected_imports.items():
        # ``source_file`` 是一个必须改用新 Module 的实际产品调用者。
        source_file = repository_root / relative_path
        # ``syntax_tree`` 只观察 import 依赖，不执行产品启动或作者模块。
        syntax_tree = ast.parse(source_file.read_text(encoding="utf-8"))
        # ``package_name`` 是相对导入解析所需的调用者 Python 包身份。
        package_name = ".".join(source_file.parent.relative_to(repository_root).parts)
        # ``imported_modules`` 保存当前调用者全部直接模块依赖绝对身份。
        imported_modules: set[str] = set()
        for node in ast.walk(syntax_tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            imported_name = node.module or ""
            if node.level:
                imported_name = importlib.util.resolve_name(
                    "." * node.level + imported_name,
                    package_name,
                )
            imported_modules.add(imported_name)
        assert required_modules <= imported_modules
        assert imported_modules.isdisjoint(legacy_modules)


def test_workspace_runtime_dependency_direction_has_one_legacy_publish_bridge() -> None:
    """新运行时不依赖旧 wrapper，纯快照只保留一个函数内发布兼容桥。

    参数：无。
    返回：无；断言新 Module 没有反向旧实现，且包目录（PackageCatalog）不会在
    加载时拥有实时发布职责。
    异常：出现越层 import 或新增第二个运行时兼容桥时测试失败。
    """

    # ``package_manager_root`` 是新 Module、纯快照与历史 wrapper 的共同源码根。
    package_manager_root = (
        Path(__file__).resolve().parents[2] / "unilabos" / "package_manager"
    )
    # ``legacy_module_names`` 是新工作区运行时（Workspace Runtime）禁止依赖的路径。
    legacy_module_names = {
        "unilabos.package_manager.compiler",
        "unilabos.package_manager.product_lifecycle",
        "unilabos.package_manager.refresh_coordinator",
        "unilabos.package_manager.runtime_activation",
        "unilabos.package_manager.runtime_diff",
        "unilabos.package_manager.workspace_file_monitor",
        "unilabos.package_manager.workspace_startup",
    }
    # ``violations`` 保存新 Module 对历史 wrapper 的精确依赖位置。
    violations: list[str] = []
    runtime_root = package_manager_root / "workspace_runtime"
    for source_file in sorted(runtime_root.rglob("*.py")):
        # ``module_parts`` 与 ``package_name`` 共同解析文件内相对 import。
        module_parts = source_file.relative_to(runtime_root).with_suffix("").parts
        package_name = "unilabos.package_manager.workspace_runtime"
        if module_parts[-1] != "__init__":
            package_name += "." + ".".join(module_parts[:-1])
        syntax_tree = ast.parse(source_file.read_text(encoding="utf-8"))
        for node in ast.walk(syntax_tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            imported_name = node.module or ""
            if node.level:
                imported_name = importlib.util.resolve_name(
                    "." * node.level + imported_name,
                    package_name,
                )
            if imported_name in legacy_module_names:
                violations.append(
                    f"{source_file.relative_to(runtime_root)}:{node.lineno}:"
                    f"{imported_name}"
                )
    assert violations == []

    # ``snapshot_tree`` 用于精确限定纯快照对实时发布 Module 的唯一函数内桥。
    snapshot_file = package_manager_root / "package_catalog" / "registry_snapshot.py"
    snapshot_tree = ast.parse(snapshot_file.read_text(encoding="utf-8"))
    # ``runtime_imports`` 保存包目录快照内所有指向运行时层的 import 位置与父函数。
    runtime_imports: list[tuple[str | None, str]] = []
    for parent in ast.walk(snapshot_tree):
        if not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(parent):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "workspace_runtime.activation"
            ):
                runtime_imports.append((parent.name, node.names[0].name))

    assert runtime_imports == [("publish", "publish_registry_snapshot")]
