"""包分发（Package Distribution）分层 Module 的公开兼容合同。"""

from __future__ import annotations

import ast
import importlib.util
from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.package_manager.test_package_dependency_lock import _write_package
from unilabos.package_manager.package_catalog import PackageCatalog


def _iter_imports_with_enclosing_function(
    node: ast.AST,
    *,
    enclosing_function: str | None = None,
) -> Iterable[tuple[ast.Import | ast.ImportFrom, str | None]]:
    """递归枚举导入节点及其最内层所属函数。

    参数：``node`` 是当前 AST 子树；``enclosing_function`` 是父子树所属的最内层
    函数名，模块级为 ``None``。
    返回：按源码树顺序产生导入节点与所属函数名，不执行被检查源码。
    异常：无；Python 解析错误由调用者在进入本函数前传播。
    """

    # ``current_function`` 在进入同步或异步函数时推进精确白名单上下文。
    current_function = (
        node.name
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        else enclosing_function
    )
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        yield node, current_function
    for child_node in ast.iter_child_nodes(node):
        # ``child_node`` 继承当前最内层函数，嵌套函数会在下一层自行替换。
        yield from _iter_imports_with_enclosing_function(
            child_node,
            enclosing_function=current_function,
        )


def _package_distribution_reverse_dependency_violations(
    module_root: Path,
) -> list[str]:
    """检查包分发（Package Distribution）全部源码的反向层依赖。

    参数：``module_root`` 是待检查 Module 根，文件按相对路径稳定排序。
    返回：``文件:行号:模块`` 格式的稳定违规列表；只允许规范默认编译器函数内
    唯一、无别名地延迟导入工作区目录编译器。
    异常：源码不可读或语法无效时传播文件系统或 ``SyntaxError``，不得跳过检查。
    """

    # ``forbidden_prefixes`` 是包分发层禁止拥有的历史或高层 Module 闭集。
    forbidden_prefixes = (
        "unilabos.package_manager.dependency_lock",
        "unilabos.package_manager.installation",
        "unilabos.package_manager.publication",
        "unilabos.package_manager.workspace_runtime",
        "unilabos.package_manager.driver_runtime",
    )
    # ``violations`` 按文件和 AST 顺序保存精确越层依赖位置。
    violations: list[str] = []
    for source_file in sorted(module_root.rglob("*.py")):
        # ``relative_file`` 是报告和唯一兼容桥匹配使用的 Module 内稳定路径。
        relative_file = source_file.relative_to(module_root)
        # ``module_parts`` 用于恢复相对 import 所在 Python 包身份。
        module_parts = list(relative_file.with_suffix("").parts)
        if module_parts[-1] == "__init__":
            module_parts.pop()
        # ``package_name`` 是 ``resolve_name`` 解析相对导入所需当前包身份。
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
        # ``syntax_tree`` 只观察依赖方向，不导入或执行被检查 Module。
        syntax_tree = ast.parse(source_file.read_text(encoding="utf-8"))
        for node, enclosing_function in _iter_imports_with_enclosing_function(
            syntax_tree
        ):
            if isinstance(node, ast.Import):
                # ``imported_names`` 是普通 import 声明的完整模块身份集合。
                imported_names = [alias.name for alias in node.names]
            else:
                # ``imported_name`` 结合当前包把相对声明解析为绝对模块身份。
                imported_name = node.module or ""
                if node.level:
                    imported_name = importlib.util.resolve_name(
                        "." * node.level + imported_name,
                        package_name,
                    )
                imported_names = [imported_name]
            for imported_name in imported_names:
                # ``allowed_default_compiler_bridge`` 是唯一接受的函数内反向依赖：
                # 历史调用未注入编译器时，延迟取得规范工作区目录编译器。
                allowed_default_compiler_bridge = (
                    relative_file.as_posix() == "dependency_manager.py"
                    and enclosing_function == "_default_compile_package_source"
                    and isinstance(node, ast.ImportFrom)
                    and imported_name
                    == "unilabos.package_manager.workspace_runtime.discovery"
                    and [(alias.name, alias.asname) for alias in node.names]
                    == [("compile_package_source", None)]
                )
                if allowed_default_compiler_bridge:
                    continue
                if (
                    imported_name == "unilabos.package_manager"
                    or imported_name.startswith(forbidden_prefixes)
                ):
                    violations.append(f"{relative_file}:{node.lineno}:{imported_name}")
    return violations


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
    from unilabos.package_manager.workspace_runtime import (
        WorkspaceSource,
        compile_package_source,
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

    # ``compiled_roots`` 记录两个公开操作实际委托给同一目录编译 Interface 的来源。
    compiled_roots: list[Path] = []

    def compile_catalog(source: WorkspaceSource) -> PackageCatalog:
        """记录包分发（Package Distribution）公开 Interface 的每次目录编译。

        参数：``source`` 是主工作区或显式外部包的安全来源。
        返回：统一工作区编译器产生的不可变包目录（PackageCatalog）。
        异常：来源或静态合同无效时传播规范编译异常。
        """

        # ``compiled_root`` 是本次编译唯一观察的来源根身份。
        compiled_root = source.root
        compiled_roots.append(compiled_root)
        return compile_package_source(source)

    # ``dependency_lock`` 是新 Module 完整校验后发布的依赖代际。
    dependency_lock = PackageDependencyManager(
        workspace_root,
        compile_catalog=compile_catalog,
    ).add("../external")
    # ``catalogs`` 是从显式来源和锁重新编译得到的包目录集合。
    catalogs = load_locked_package_catalogs(
        workspace_root,
        compile_catalog=compile_catalog,
    )

    assert tuple(item.distribution_name for item in dependency_lock.packages) == (
        "external-lab",
    )
    assert tuple(item.id for item in catalogs[0].definitions.devices) == ("reader",)
    assert compiled_roots == [
        external_root.resolve(),
        workspace_root.resolve(),
        external_root.resolve(),
        workspace_root.resolve(),
    ]


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
        "workspace_runtime/activation.py": {
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
        # ``caller_package`` 是相对 import 解析所需的实际调用者包身份。
        caller_package = "unilabos.package_manager"
        relative_parent = source_file.parent.relative_to(package_manager_root)
        if relative_parent.parts:
            caller_package += "." + ".".join(relative_parent.parts)
        # ``imported_modules`` 保存解析成绝对身份的直接 import 集合。
        imported_modules: set[str] = set()
        for node in ast.walk(syntax_tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            imported_name = node.module or ""
            if node.level:
                imported_name = importlib.util.resolve_name(
                    "." * node.level + imported_name,
                    caller_package,
                )
            imported_modules.add(imported_name)
        assert required_modules <= imported_modules
        assert imported_modules.isdisjoint(legacy_modules)


def test_package_distribution_module_has_no_reverse_layer_dependency() -> None:
    """包分发（Package Distribution）全树不反向依赖历史实现或运行时层。

    参数：无。
    返回：无；解析新 Module 的全部顶层和函数内 import，仅允许默认编译器函数内
    的精确延迟兼容桥。
    异常：其他位置出现历史根实现、工作区运行时或驱动运行时依赖时测试失败。
    """

    # ``module_root`` 是包分发（Package Distribution）新 Module 的源码边界。
    module_root = (
        Path(__file__).resolve().parents[2]
        / "unilabos"
        / "package_manager"
        / "package_distribution"
    )
    # ``violations`` 必须为空，且伪非法函数回归测试已证明 helper 不会漏检。
    violations = _package_distribution_reverse_dependency_violations(module_root)

    assert violations == []


def test_reverse_dependency_guard_rejects_non_whitelisted_function_import(
    tmp_path: Path,
) -> None:
    """依赖方向守卫必须拒绝白名单函数以外的函数内运行时反向导入。

    参数：``tmp_path`` 提供一个隔离的伪包分发（Package Distribution）源码根。
    返回：无；断言相同运行时导入出现在其他函数时仍被报告为越层依赖。
    异常：守卫错误跳过全部函数内导入时，精确违规列表断言失败。
    """

    # ``module_root`` 是只包含一个非法函数内反向导入的伪 Module 边界。
    module_root = tmp_path / "package_distribution"
    module_root.mkdir()
    # ``source_file`` 模拟非白名单函数偷偷依赖工作区运行时（Workspace Runtime）。
    source_file = module_root / "rogue.py"
    source_file.write_text(
        "def load_runtime_compiler():\n"
        "    from unilabos.package_manager.workspace_runtime.discovery "
        "import compile_package_source\n"
        "    return compile_package_source\n",
        encoding="utf-8",
    )

    # ``violations`` 必须包含非法函数内导入，证明白名单不会扩大到整棵 AST。
    violations = _package_distribution_reverse_dependency_violations(module_root)

    assert violations == [
        "rogue.py:2:unilabos.package_manager.workspace_runtime.discovery"
    ]


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


def test_http_publication_adapter_preserves_delegate_contract() -> None:
    """HTTP 发布 Adapter 保持委托参数及发布传输异常的原始身份。

    参数：无。
    返回：无；断言归档场景、资源 DTO 和软件包信息原样委托，并且
    ``publish_inspection`` 不改包传输 Adapter 抛出的异常对象。
    异常：参数被复制或改写、传输异常被包装成 ``PackageCLIError`` 时测试失败。
    """

    from unilabos.package_manager.package_distribution import (
        HttpClientPublicationAdapter,
        publish_inspection,
    )

    class PublicationTransportError(RuntimeError):
        """表示测试云端资源发布边界返回的固定传输失败。"""

    # ``transport_error`` 是必须穿过发布编排保持对象身份的传输失败实例。
    transport_error = PublicationTransportError("publication transport unavailable")

    class RecordingHttpClient:
        """记录现有 HTTP 客户端两个发布操作收到的原始参数。"""

        def __init__(self) -> None:
            """初始化调用记录和可切换的资源发布失败状态。

            参数：无。
            返回：无。
            异常：无。
            """

            # 两类调用分别保留委托参数身份；``fail_publication`` 控制固定点失败。
            self.upload_calls: list[tuple[str, str]] = []
            self.publication_calls: list[
                tuple[list[dict[str, object]], dict[str, object]]
            ] = []
            self.fail_publication = False

        def upload_file_to_oss(
            self,
            path: str,
            *,
            scene: str,
        ) -> tuple[str, str]:
            """记录归档路径和 OSS 场景并返回固定产物身份。

            参数：``path`` 是归档路径；``scene`` 是现有 HTTP 合同要求的上传场景。
            返回：公开下载地址和对象键。
            异常：无。
            """

            self.upload_calls.append((path, scene))
            return "https://packages.example/direct.tar.gz", "models/direct.tar.gz"

        def upload_package_resources(
            self,
            resources: list[dict[str, object]],
            package_info: dict[str, object],
        ) -> object:
            """记录资源发布参数并按测试状态返回成功或原始传输失败。

            参数：``resources`` 是兼容资源 DTO；``package_info`` 是软件包发布信息。
            返回：未启用失败时返回 HTTP 200 的最小响应对象。
            异常：启用失败时抛出同一 ``transport_error`` 对象。
            """

            self.publication_calls.append((resources, package_info))
            if self.fail_publication:
                raise transport_error
            return SimpleNamespace(status_code=200, text="ok")

    # ``http_client`` 是被适配的现有传输实现；``adapter`` 是待验证的真实 Adapter。
    http_client = RecordingHttpClient()
    adapter = HttpClientPublicationAdapter(http_client)
    # ``resources`` 与 ``package_info`` 用于证明直接委托不复制参数容器。
    resources = [{"id": "community.lab.reader"}]
    package_info = {"class_namespace": "community.lab"}

    assert adapter.upload_artifact("/tmp/direct.tar.gz") == (
        "https://packages.example/direct.tar.gz",
        "models/direct.tar.gz",
    )
    adapter.publish_resources(resources, package_info)

    assert http_client.upload_calls == [("/tmp/direct.tar.gz", "models")]
    assert http_client.publication_calls[0][0] is resources
    assert http_client.publication_calls[0][1] is package_info

    http_client.fail_publication = True
    # ``inspection`` 是进入发布编排但使用显式地址跳过第二次归档上传的检查产物。
    inspection = {
        "archive_path": "/tmp/lab.tar.gz",
        "package_info": {"class_namespace": "community.lab"},
        "resources": [{"id": "community.lab.reader"}],
    }
    with pytest.raises(PublicationTransportError) as caught:
        publish_inspection(
            inspection,
            adapter,
            download_url="https://packages.example/lab.tar.gz",
        )

    assert caught.value is transport_error


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
