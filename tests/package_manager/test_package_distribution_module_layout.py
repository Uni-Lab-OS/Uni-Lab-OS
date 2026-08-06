"""包分发（Package Distribution）分层 Module 的公开兼容合同。"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.package_manager.test_package_dependency_lock import _write_package


def test_new_package_distribution_interface_manages_explicit_dependency(
    tmp_path: Path,
) -> None:
    """新 Module 通过同一公开 Interface 发布并加载显式软件包依赖。

    参数：``tmp_path`` 提供主工作区与外部软件包的隔离父目录。
    返回：无；断言新路径能发布锁并加载完整包目录（PackageCatalog）。
    异常：新 Module、依赖管理 Interface 或显式来源校验缺失时测试失败。
    """

    from unilabos.package_manager.package_distribution import (
        PackageDependencyManager,
        load_locked_package_catalogs,
    )

    # ``workspace_root`` 是依赖声明与锁的写权威所在主工作区。
    workspace_root = tmp_path / "workspace"
    # ``external_root`` 是只通过显式声明授权的外部软件包来源。
    external_root = tmp_path / "external"
    _write_package(
        workspace_root,
        distribution_name="workspace-lab",
        package_name="workspace_lab",
    )
    _write_package(
        external_root,
        distribution_name="external-lab",
        package_name="external_lab",
        device_ids=("reader",),
    )

    # ``dependency_lock`` 是新 Module 完整校验后发布的依赖代际。
    dependency_lock = PackageDependencyManager(workspace_root).add("../external")
    # ``catalogs`` 是从显式来源和锁重新编译得到的包目录集合。
    catalogs = load_locked_package_catalogs(workspace_root)

    assert tuple(item.distribution_name for item in dependency_lock.packages) == (
        "external-lab",
    )
    assert tuple(item.id for item in catalogs[0].definitions.devices) == ("reader",)


def test_legacy_distribution_imports_retain_new_public_object_identities() -> None:
    """根门面和历史模块继续指向包分发（Package Distribution）的同一对象。

    参数：无。
    返回：无；断言依赖模型、管理器、安装与发布入口没有形成平行实现。
    异常：兼容 wrapper 复制实现或新 Module 遗漏公开对象时测试失败。
    """

    # 新 Module 对象是历史入口必须保持的唯一公开实现身份。
    from unilabos.package_manager import (
        LockedPackage as root_locked_package,
    )
    from unilabos.package_manager import (
        PackageDependencyManager as root_dependency_manager,
    )
    from unilabos.package_manager import upload_package as root_upload_package
    from unilabos.package_manager.dependency_lock import (
        LockedPackage as legacy_locked_package,
    )
    from unilabos.package_manager.dependency_lock import (
        PackageDependencyManager as legacy_dependency_manager,
    )
    from unilabos.package_manager.installation import (
        install_package as legacy_install_package,
    )
    from unilabos.package_manager.package_distribution import (
        LockedPackage,
        PackageDependencyManager,
        install_package,
        upload_package,
    )
    from unilabos.package_manager.publication import (
        upload_package as legacy_upload_package,
    )

    assert LockedPackage is root_locked_package is legacy_locked_package
    assert (
        PackageDependencyManager is root_dependency_manager is legacy_dependency_manager
    )
    assert install_package is legacy_install_package
    assert upload_package is root_upload_package is legacy_upload_package


def test_internal_callers_depend_on_package_distribution_interface() -> None:
    """命令行与运行时调用者必须直接依赖包分发（Package Distribution）Interface。

    参数：无。
    返回：无；断言产品调用者不再穿过历史 ``dependency_lock`` 或
    ``publication`` wrapper。
    异常：内部调用者保留遗留导入或没有采用新 Module 时测试失败。
    """

    # ``package_manager_root`` 是需要验证内部调用依赖方向的源码根。
    package_manager_root = (
        Path(__file__).resolve().parents[2] / "unilabos" / "package_manager"
    )
    # ``expected_imports`` 固定每个调用者必须采用的新 Module 绝对身份。
    expected_imports = {
        "cli.py": {
            "unilabos.package_manager.package_distribution",
        },
        "runtime_activation.py": {
            "unilabos.package_manager.package_distribution",
        },
    }
    # ``legacy_modules`` 是只能供外部兼容、不能供产品内部调用的历史路径。
    legacy_modules = {
        "unilabos.package_manager.dependency_lock",
        "unilabos.package_manager.installation",
        "unilabos.package_manager.publication",
    }

    for filename, required_modules in expected_imports.items():
        # ``source_file`` 是当前产品调用者的实际源码文件。
        source_file = package_manager_root / filename
        # ``syntax_tree`` 只解析导入，不执行产品启动逻辑。
        syntax_tree = ast.parse(source_file.read_text(encoding="utf-8"))
        # ``imported_modules`` 保存解析成绝对身份的直接 import 集合。
        imported_modules: set[str] = set()
        for node in ast.walk(syntax_tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            imported_name = node.module or ""
            if node.level:
                imported_name = importlib.util.resolve_name(
                    "." * node.level + imported_name,
                    "unilabos.package_manager",
                )
            imported_modules.add(imported_name)
        assert required_modules <= imported_modules
        assert imported_modules.isdisjoint(legacy_modules)


def test_package_distribution_module_has_no_reverse_layer_dependency() -> None:
    """包分发（Package Distribution）Module 不反向依赖历史实现或运行时层。

    参数：无。
    返回：无；解析新 Module 的全部 import 并断言依赖只指向自身或底层能力。
    异常：出现历史根实现、工作区运行时或驱动运行时依赖时测试失败。
    """

    # ``module_root`` 是包分发（Package Distribution）新 Module 的源码边界。
    module_root = (
        Path(__file__).resolve().parents[2]
        / "unilabos"
        / "package_manager"
        / "package_distribution"
    )
    # ``forbidden_prefixes`` 防止分发实现回流到历史 wrapper 或高层运行时。
    forbidden_prefixes = (
        "unilabos.package_manager.dependency_lock",
        "unilabos.package_manager.installation",
        "unilabos.package_manager.publication",
        "unilabos.package_manager.workspace_runtime",
        "unilabos.package_manager.driver_runtime",
    )
    # ``violations`` 记录相对文件、行号和越层导入，便于修复依赖方向。
    violations: list[str] = []
    for source_file in sorted(module_root.rglob("*.py")):
        # ``relative_file`` 是不绑定工作树绝对路径的 Module 内文件身份。
        relative_file = source_file.relative_to(module_root)
        # ``module_parts`` 用于恢复相对 import 所在 Python 包身份。
        module_parts = list(relative_file.with_suffix("").parts)
        if module_parts[-1] == "__init__":
            module_parts.pop()
        # ``package_name`` 是 ``resolve_name`` 解析相对导入所需当前包。
        package_name = ".".join(
            [
                "unilabos",
                "package_manager",
                "package_distribution",
                *module_parts[:-1],
            ]
        )
        if relative_file.name == "__init__.py":
            package_name = ".".join(
                [
                    "unilabos",
                    "package_manager",
                    "package_distribution",
                    *module_parts,
                ]
            )
        # ``syntax_tree`` 只用于读取 import 依赖，不触发任何安装或网络操作。
        syntax_tree = ast.parse(source_file.read_text(encoding="utf-8"))
        for node in ast.walk(syntax_tree):
            if isinstance(node, ast.Import):
                # ``imported_names`` 是普通 import 的绝对 Module 身份集合。
                imported_names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
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
                if (
                    imported_name == "unilabos.package_manager"
                    or imported_name.startswith(forbidden_prefixes)
                ):
                    violations.append(f"{relative_file}:{node.lineno}:{imported_name}")

    assert violations == []


def test_publication_port_uploads_artifact_before_publishing_resources() -> None:
    """云端发布 Adapter 只消费既有检查产物并按顺序完成归档与资源发布。

    参数：无。
    返回：无；断言公开地址、对象键和同一软件包信息进入全部资源 DTO。
    异常：实现重新检查工作区、漏传归档身份或没有发布资源时测试失败。
    """

    from unilabos.package_manager.package_distribution import publish_inspection

    class RecordingPublicationPort:
        """记录测试中跨系统发布调用的最小传输 Adapter。"""

        def __init__(self) -> None:
            """初始化空调用记录。

            参数：无。
            返回：无。
            异常：无。
            """

            # ``calls`` 保存归档上传与资源发布的可观察先后顺序。
            self.calls: list[tuple[str, object]] = []

        def upload_artifact(self, path: str) -> tuple[str, str]:
            """记录归档上传并返回固定云端身份。

            参数：``path`` 是被发布的本地检查产物路径。
            返回：独立给定的公开地址和对象键。
            异常：无。
            """

            self.calls.append(("artifact", path))
            return "https://packages.example/lab.tar.gz", "models/lab.tar.gz"

        def publish_resources(
            self,
            resources: list[dict[str, object]],
            package_info: dict[str, object],
        ) -> object:
            """记录资源发布并返回成功状态。

            参数：``resources`` 是兼容资源 DTO；``package_info`` 是发布包身份。
            返回：HTTP 201 的最小响应对象。
            异常：无。
            """

            self.calls.append(
                (
                    "resources",
                    {
                        "resources": resources,
                        "package_info": package_info,
                    },
                )
            )
            return SimpleNamespace(status_code=201, text="created")

    # ``inspection`` 是已经完成包目录（PackageCatalog）编译和归档构建的输入。
    inspection = {
        "archive_path": "/tmp/lab.tar.gz",
        "package_info": {"class_namespace": "community.lab"},
        "resources": [{"id": "community.lab.reader"}],
    }
    # ``port`` 是测试唯一替代的外部云端系统边界。
    port = RecordingPublicationPort()

    # ``published`` 是新云端 Adapter 返回的稳定发布结果。
    published = publish_inspection(inspection, port)

    assert [item[0] for item in port.calls] == ["artifact", "resources"]
    assert published["download_url"] == "https://packages.example/lab.tar.gz"
    assert published["package_info"]["oss_object_key"] == "models/lab.tar.gz"
    assert published["resources"][0]["package_info"] == published["package_info"]
    assert published["response_status"] == 201


def test_publication_port_rejects_backend_error_status() -> None:
    """云端资源发布返回非成功状态时必须保留诊断并关闭式失败。

    参数：无。
    返回：无；断言 HTTP 503 与响应文本被归一为 ``PackageCLIError``。
    异常：若实现把失败响应当成已发布代际，预期异常断言失败。
    """

    from unilabos.package_manager import PackageCLIError
    from unilabos.package_manager.package_distribution import publish_inspection

    class FailingPublicationPort:
        """模拟归档成功但资源发布失败的云端传输 Adapter。"""

        def upload_artifact(self, path: str) -> tuple[str, str]:
            """返回固定归档地址以推进到资源发布阶段。

            参数：``path`` 是本地归档路径；测试不读取该文件。
            返回：公开地址和空对象键。
            异常：无。
            """

            return "https://packages.example/lab.tar.gz", ""

        def publish_resources(
            self,
            resources: list[dict[str, object]],
            package_info: dict[str, object],
        ) -> object:
            """返回携带诊断文本的 HTTP 503 响应。

            参数：``resources`` 与 ``package_info`` 是待发布兼容投影。
            返回：HTTP 503 的最小响应对象。
            异常：无；错误状态由发布编排统一解释。
            """

            return SimpleNamespace(status_code=503, text="catalog unavailable")

    # ``inspection`` 是无需重新扫描的最小已检查发布产物。
    inspection = {
        "archive_path": "/tmp/lab.tar.gz",
        "package_info": {"class_namespace": "community.lab"},
        "resources": [{"id": "community.lab.reader"}],
    }

    with pytest.raises(
        PackageCLIError,
        match="503 catalog unavailable",
    ):
        publish_inspection(inspection, FailingPublicationPort())
