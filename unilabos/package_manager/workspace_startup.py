"""把一个显式工作区（Workspace）投影为产品基线启动配置。"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomllib

from unilabos.workflow.source_discovery import (
    SourceDeclarationError,
    discover_editable_sources,
)
from unilabos.workflow.source_manifest import (
    SourceManifestError,
    parse_editable_package_manifest,
)

from .sources import WorkspaceSource


@dataclass(frozen=True)
class WorkspaceStartupPlan:
    """完成静态校验、尚未激活设备或工作流的启动计划。"""

    # ``source`` 是本轮启动唯一被授权的工作区文件来源。
    source: WorkspaceSource
    # ``distribution_name`` 是 pyproject.toml 声明的原始发行包身份。
    distribution_name: str
    # ``import_package`` 是从发行元数据规范化得到的 Python 导入包身份。
    import_package: str
    # ``package_directory`` 是注册表（Registry）唯一允许扫描的本地包目录。
    package_directory: Path
    # ``community_namespace`` 是本地包对物理图公开的社区命名空间。
    community_namespace: str
    # ``has_workflow_manifest`` 表示工作区是否声明了工作流源码清单。
    has_workflow_manifest: bool
    # ``workflow_source_count`` 是清单静态校验通过的工作流源码数量。
    workflow_source_count: int

    def apply(self, arguments: dict[str, Any]) -> None:
        """把启动计划一次应用到现有产品参数字典。

        参数：``arguments`` 是公共命令行（CLI）解析后的可变参数字典。
        返回：无；应用后设备扫描、社区命名空间和工作流源码授权使用同一工作区。
        异常：调用者同时提供遗留 ``--devices`` 或额外工作流授权根时抛出
        ``ValueError``，防止出现第二套包发现权威。
        """

        if arguments.get("devices"):
            raise ValueError("--workspace 不可与 legacy --devices 同时使用")
        if arguments.get("workflow_editable_package_root"):
            raise ValueError(
                "--workspace 不可与 --workflow_editable_package_root 同时使用"
            )

        # ``workspace_root`` 是本进程唯一显式工作区身份，供图与源码共同寻址。
        workspace_root = str(self.source.root)
        # ``device_directory`` 是注册表（Registry）唯一额外 AST 扫描根。
        device_directory = str(self.package_directory)
        arguments["devices"] = [device_directory]
        arguments["_workspace_root"] = workspace_root
        arguments["_community_namespaces"] = {
            device_directory: self.community_namespace
        }
        arguments["workflow_editable_package_root"] = (
            [workspace_root] if self.has_workflow_manifest else None
        )
        graph_argument = arguments.get("graph")
        if isinstance(graph_argument, str) and graph_argument:
            arguments["graph"] = str(self.resolve_graph(graph_argument))
        if workspace_root in sys.path:
            sys.path.remove(workspace_root)
        sys.path.insert(0, workspace_root)

    def resolve_graph(self, graph_argument: str) -> Path:
        """解析公共命令行（CLI）的物理图文件参数。

        参数：``graph_argument`` 是绝对路径或相对工作区根的图文件路径。
        返回：存在的规范绝对文件路径。
        异常：相对路径逃逸、包含符号链接、缺失或不是普通文件时抛出
        ``ValueError``。
        """

        # ``graph_path`` 保留调用者选择的路径身份，供符号链接检查使用。
        graph_path = Path(graph_argument).expanduser()
        if graph_path.is_absolute():
            try:
                # ``logical_graph`` 把绝对路径重新约束为工作区来源的安全相对路径。
                logical_graph = graph_path.relative_to(self.source.root).as_posix()
            except ValueError as error:
                raise ValueError(
                    f"绝对物理图文件必须位于工作区内: {graph_argument}"
                ) from error
            if not self.source.has_file(logical_graph):
                raise ValueError(f"工作区内物理图文件不存在: {graph_argument}")
            # ``resolved_graph`` 已经过 WorkspaceSource 的全路径符号链接校验。
            resolved_graph = self.source.root.joinpath(
                *Path(logical_graph).parts
            ).resolve()
            return resolved_graph
        logical_graph = graph_path.as_posix()
        if not self.source.has_file(logical_graph):
            raise ValueError(f"工作区内物理图文件不存在: {graph_argument}")
        # ``resolved_graph`` 已经过 WorkspaceSource 的越界与符号链接校验。
        resolved_graph = self.source.root.joinpath(*Path(logical_graph).parts).resolve()
        return resolved_graph


def normalize_distribution_name(distribution_name: str) -> str:
    """把发行包名称规范化为当前工作区导入包身份。

    参数：``distribution_name`` 是 ``pyproject.toml`` 的 ``project.name``。
    返回：小写并把连字符、点和下划线段统一为下划线的 Python 包名。
    异常：参数不是非空字符串或不能形成 Python 标识符时抛出 ``ValueError``。
    """

    if not isinstance(distribution_name, str) or not distribution_name.strip():
        raise ValueError("pyproject.toml project.name 必须是非空字符串")
    import_package = re.sub(r"[-_.]+", "_", distribution_name.strip().lower())
    if not import_package.isidentifier():
        raise ValueError("工作区发行包名称不能规范化为 Python 包身份")
    return import_package


def compile_workspace_startup(source: WorkspaceSource) -> WorkspaceStartupPlan:
    """静态编译启动门禁所需的最小工作区计划。

    参数：``source`` 是公共命令行（CLI）显式授权的工作区来源。
    返回：不导入设备模块、不应用工作流图的不可变启动计划。
    异常：项目元数据、导入包目录或可选 ``package.yaml`` 无效时抛出
    ``ValueError``，不返回部分计划。
    """

    if not isinstance(source, WorkspaceSource):
        raise TypeError("source 必须是 WorkspaceSource")
    # ``project_name`` 是发行元数据身份；``import_package`` 是规范 Python 包身份。
    project_name = _read_project_name(source)
    import_package = normalize_distribution_name(project_name)
    # ``package_directory`` 是注册表（Registry）唯一允许扫描的包目录。
    package_directory = source.root / import_package
    if (
        package_directory.is_symlink()
        or not package_directory.is_dir()
        or package_directory.resolve() != package_directory
        or not package_directory.joinpath("__init__.py").is_file()
    ):
        raise ValueError(
            f"工作区缺少规范 Python 包目录或 __init__.py: {import_package}"
        )
    _validate_registry_python_tree(source, package_directory)

    # ``has_workflow_manifest`` 决定是否授权工作流源码（Workflow Source）发现。
    has_workflow_manifest = source.has_file("package.yaml")
    # ``workflow_source_count`` 证明本次计划确实加载了封闭来源声明。
    workflow_source_count = 0
    if has_workflow_manifest:
        workflow_source_count = _validate_workflow_manifest(
            source,
            import_package=import_package,
        )
    return WorkspaceStartupPlan(
        source=source,
        distribution_name=project_name.strip(),
        import_package=import_package,
        package_directory=package_directory,
        community_namespace=f"community.{import_package}",
        has_workflow_manifest=has_workflow_manifest,
        workflow_source_count=workflow_source_count,
    )


def prepare_workspace_startup(
    arguments: dict[str, Any],
) -> WorkspaceStartupPlan | None:
    """从公共命令行（CLI）参数准备并应用可选工作区。

    参数：``arguments`` 是 argparse 生成的可变参数字典。
    返回：配置了 ``--workspace`` 时返回已应用计划，否则返回 ``None``。
    异常：参数形状或工作区内容无效时抛出 ``TypeError``/``ValueError``；不会导入
    设备实现、应用工作流（Workflow）候选或创建工作流任务（WorkflowTask）。
    """

    if not isinstance(arguments, dict):
        raise TypeError("启动参数必须是 dict")
    workspace_argument = arguments.get("workspace")
    if workspace_argument is None:
        return None
    if not isinstance(workspace_argument, str) or not workspace_argument.strip():
        raise ValueError("--workspace 必须是非空目录路径")
    # ``workspace_source`` 是本轮启动唯一被授权的本地包来源。
    workspace_source = WorkspaceSource(workspace_argument)
    # ``startup_plan`` 是静态校验后的唯一启动投影，不包含工作流任务（WorkflowTask）。
    startup_plan = compile_workspace_startup(workspace_source)
    startup_plan.apply(arguments)
    return startup_plan


def _read_project_name(source: WorkspaceSource) -> str:
    """读取工作区项目的稳定发行包名称。

    参数：``source`` 是已固定根目录的工作区来源。
    返回：``pyproject.toml`` 中非空 ``project.name``。
    异常：UTF-8、TOML 或字段形状无效时抛出 ``ValueError``。
    """

    try:
        project_document = tomllib.loads(
            source.read_bytes("pyproject.toml").decode("utf-8")
        )
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError("工作区 pyproject.toml 无效") from error
    project_table = project_document.get("project")
    if project_table is None:
        raise ValueError("pyproject.toml 缺少 [project]")
    if not isinstance(project_table, dict):
        raise TypeError("pyproject.toml [project] 必须是表")
    project_name = project_table.get("name")
    if not isinstance(project_name, str) or not project_name.strip():
        raise ValueError("pyproject.toml project.name 必须是非空字符串")
    return project_name


def _validate_workflow_manifest(
    source: WorkspaceSource,
    *,
    import_package: str,
) -> int:
    """验证工作流源码（Workflow Source）声明与项目包身份一致。

    参数：``source`` 是已固定工作区来源；``import_package`` 是项目规范导入身份。
    返回：完整声明中的工作流源码（Workflow Source）数量。
    异常：YAML、身份或源码路径无效时转换为不泄漏内容的 ``ValueError``。
    """

    try:
        # ``manifest`` 拥有包身份与工作流源码（Workflow Source）声明顺序。
        manifest = parse_editable_package_manifest(source.read_bytes("package.yaml"))
        if manifest.package_id != import_package:
            raise ValueError("package.yaml 包身份与 pyproject.toml 不一致")
        # ``discovery_plan`` 固定所有来源身份，但不应用候选工作流图。
        discovery_plan = discover_editable_sources((source.root,))
    except (SourceManifestError, SourceDeclarationError) as error:
        raise ValueError("工作区 package.yaml 或工作流源码声明无效") from error
    return len(discovery_plan.registrations)


def _validate_registry_python_tree(
    source: WorkspaceSource,
    package_directory: Path,
) -> None:
    """验证注册表（Registry）扫描树中的 Python 文件没有符号链接路径。

    参数：``source`` 是工作区授权来源；``package_directory`` 是将交给注册表扫描
    的本地包目录。
    返回：无；所有 Python 文件和可递归目录均位于同一工作区授权边界时正常结束。
    异常：目录不可读，或 Python 文件/递归目录经过符号链接时抛出 ``ValueError``。
    """

    # ``pending_directories`` 保存尚未检查的真实包目录，拒绝跟随任何符号链接目录。
    pending_directories = [package_directory]
    while pending_directories:
        # ``current_directory`` 是本轮读取目录项的真实注册表扫描目录。
        current_directory = pending_directories.pop()
        try:
            # ``entries`` 使用稳定排序，保证错误定位和测试结果可重复。
            entries = sorted(current_directory.iterdir())
        except OSError as error:
            raise ValueError(
                f"注册表 Python 扫描目录不可访问: {current_directory}"
            ) from error
        for entry in entries:
            # ``entry`` 是包目录中可能被注册表递归访问的一个文件系统对象。
            if entry.is_symlink():
                if entry.suffix == ".py" or (
                    entry.is_dir() and not entry.name.startswith(("__", "."))
                ):
                    raise ValueError(f"注册表 Python 扫描路径不得经过符号链接: {entry}")
                continue
            if entry.is_dir():
                if not entry.name.startswith(("__", ".")):
                    pending_directories.append(entry)
                continue
            if entry.suffix != ".py":
                continue
            # ``logical_python`` 是 Python 文件相对工作区根的安全逻辑身份。
            logical_python = entry.relative_to(source.root).as_posix()
            if not source.has_file(logical_python):
                raise ValueError(f"注册表 Python 文件不存在: {entry}")


__all__ = [
    "WorkspaceStartupPlan",
    "compile_workspace_startup",
    "normalize_distribution_name",
    "prepare_workspace_startup",
]
