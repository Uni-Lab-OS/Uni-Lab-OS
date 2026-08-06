"""显式软件包来源到完整软件包目录（PackageCatalog）的深模块。"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

from ._registry_catalog import compile_registry_definitions
from ._workflow_catalog import compile_workflow_definitions
from .catalog import (
    PackageAsset,
    PackageCatalog,
    PackageCompileError,
    PackageDefinitionCatalog,
    PackageDiagnostic,
    PackageDistributionIdentity,
)
from .sources import WorkspaceSource
from .workspace_startup import compile_workspace_startup

_IGNORED_PARTS = {"__pycache__", ".git", ".pytest_cache", ".mypy_cache"}
_IGNORED_SUFFIXES = {".pyc", ".pyo"}


def compile_package_source(source: WorkspaceSource) -> PackageCatalog:
    """完整静态编译一个显式工作区软件包来源。

    参数：``source`` 是唯一授权的工作区来源 Adapter。
    返回：包含全部设备、资源、显式工作流源码（Workflow Source）和资产的不可变
    软件包目录（PackageCatalog）。
    异常：来源、语法、身份、动作合同（Action Contract）或资产不合法时抛出
    ``PackageCompileError``；函数不修改 ``sys.path``、不导入作者模块且不发布部分状态。
    """

    if not isinstance(source, WorkspaceSource):
        raise TypeError("source 必须是 WorkspaceSource")
    try:
        # ``startup_plan`` 复用工作区、项目元数据和工作流清单的唯一解析结果。
        startup_plan = compile_workspace_startup(source)
        logical_files = _collect_package_files(
            source,
            startup_plan.package_directory,
        )
    except (TypeError, ValueError) as error:
        raise PackageCompileError(
            (
                PackageDiagnostic(
                    code="package_source_invalid",
                    message="软件包来源或项目元数据无效",
                ),
            )
        ) from error

    # ``file_contents`` 是一次编译观察到的完整文件字节，不从磁盘二次猜测内容。
    file_contents = {
        logical_path: source.read_bytes(logical_path) for logical_path in logical_files
    }
    python_files = _validate_python_sources(
        source=source,
        file_contents=file_contents,
    )
    content_hashes = {
        logical_path: _sha256(content)
        for logical_path, content in file_contents.items()
    }
    devices, resources = compile_registry_definitions(
        workspace_root=source.root,
        namespace=startup_plan.community_namespace,
        python_files=python_files,
        content_hashes=content_hashes,
    )
    workflows = compile_workflow_definitions(
        source=source,
        import_package=startup_plan.import_package,
        namespace=startup_plan.community_namespace,
        manifest=startup_plan.workflow_manifest,
        content_hashes=content_hashes,
    )
    all_fqids = [item.fqid for item in (*devices, *resources, *workflows)]
    if len(set(all_fqids)) != len(all_fqids):
        raise PackageCompileError(
            (
                PackageDiagnostic(
                    code="duplicate_definition",
                    message="不同定义种类共享同一全限定身份",
                ),
            )
        )
    assets = tuple(
        PackageAsset(
            logical_path=logical_path,
            digest=content_hashes[logical_path],
            size=len(content),
        )
        for logical_path, content in sorted(file_contents.items())
        if not logical_path.endswith(".py")
    )
    # ``project`` 是工作区启动和目录编译共用的项目元数据事实。
    project = startup_plan.project_metadata
    distribution = PackageDistributionIdentity(
        name=project.name,
        normalized_name=project.normalized_name,
        version=project.version,
        description=project.description,
        license=project.license,
        homepage=project.homepage,
        requires_python=project.requires_python,
        dependencies=project.dependencies,
    )
    content_items = [
        ("pyproject.toml", source.read_bytes("pyproject.toml")),
        *(
            [("package.yaml", source.read_bytes("package.yaml"))]
            if startup_plan.has_workflow_manifest
            else []
        ),
        *sorted(file_contents.items()),
    ]
    return PackageCatalog.create(
        distribution=distribution,
        import_package=startup_plan.import_package,
        namespace=startup_plan.community_namespace,
        definitions=PackageDefinitionCatalog(
            devices=devices,
            resources=resources,
            workflows=workflows,
        ),
        assets=assets,
        content_digest=_content_digest(content_items),
    )


def _collect_package_files(
    source: WorkspaceSource,
    package_directory: Path,
) -> tuple[str, ...]:
    """稳定列出导入包内全部安全普通文件。

    参数：``source`` 是授权来源；``package_directory`` 是唯一导入包目录。
    返回：排除缓存和字节码后的 POSIX 逻辑路径集合。
    异常：遇到符号链接、不可读目录或越界对象时抛出 ``ValueError``。
    """

    pending_directories = [package_directory]
    logical_files: list[str] = []
    while pending_directories:
        current_directory = pending_directories.pop()
        try:
            entries = sorted(current_directory.iterdir(), key=lambda item: item.name)
        except OSError as error:
            raise ValueError("软件包目录不可读取") from error
        for entry in entries:
            if entry.name in _IGNORED_PARTS:
                continue
            if entry.is_symlink():
                raise ValueError("软件包目录不得包含符号链接")
            if entry.is_dir():
                pending_directories.append(entry)
                continue
            if not entry.is_file() or entry.suffix in _IGNORED_SUFFIXES:
                continue
            logical_path = entry.relative_to(source.root).as_posix()
            if not source.has_file(logical_path):
                raise ValueError("软件包文件不在授权来源内")
            logical_files.append(logical_path)
    return tuple(sorted(logical_files))


def _validate_python_sources(
    *,
    source: WorkspaceSource,
    file_contents: dict[str, bytes],
) -> tuple[Path, ...]:
    """在注册表扫描前完整验证所有 Python 文件的编码和语法。

    参数：``source`` 是授权工作区；``file_contents`` 是本次固定文件观察。
    返回：可交给产品 AST 注册表解析器的规范绝对路径。
    异常：任一文件编码或语法无效时抛出单个 ``PackageCompileError``，不会继续投影。
    """

    python_files: list[Path] = []
    diagnostics: list[PackageDiagnostic] = []
    for logical_path, content in sorted(file_contents.items()):
        if not logical_path.endswith(".py"):
            continue
        try:
            source_text = content.decode("utf-8")
            ast.parse(source_text, filename=logical_path)
        except UnicodeError:
            diagnostics.append(
                PackageDiagnostic(
                    code="python_encoding_error",
                    message="Python 源码必须使用 UTF-8",
                    path=logical_path,
                )
            )
            continue
        except SyntaxError as error:
            diagnostics.append(
                PackageDiagnostic(
                    code="python_syntax_error",
                    message="Python 源码语法无效",
                    path=logical_path,
                    line=error.lineno,
                )
            )
            continue
        python_files.append(source.root / logical_path)
    if diagnostics:
        raise PackageCompileError(tuple(diagnostics))
    return tuple(python_files)


def _sha256(content: bytes) -> str:
    """计算一个文件内容的稳定 SHA-256 身份。

    参数：``content`` 是本次固定观察到的文件字节。
    返回：带 ``sha256:`` 前缀的小写十六进制摘要。
    异常：无。
    """

    return "sha256:" + hashlib.sha256(content).hexdigest()


def _content_digest(items: list[tuple[str, bytes]]) -> str:
    """计算带路径和长度分隔的软件包完整内容摘要。

    参数：``items`` 是逻辑路径与内容字节对；路径必须在调用前稳定排序或唯一。
    返回：不依赖绝对路径和修改时间的 SHA-256 摘要。
    异常：路径不是 UTF-8 可编码文本时传播编码异常。
    """

    digest = hashlib.sha256()
    for logical_path, content in sorted(items):
        path_bytes = logical_path.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return "sha256:" + digest.hexdigest()


__all__ = ["compile_package_source"]
