"""软件包命令行（Package CLI）的公共分派入口。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from unilabos.utils.banner_print import print_status

from .package_catalog import PackageCompileError
from .package_distribution import (
    PackageBuildArtifact,
    PackageBuildError,
    PackageDependencyManager,
    build_workspace_package,
)
from .package_distribution.acquisition import acquire_package
from .package_distribution.cache import PackageCache
from .package_distribution.errors import PackageCLIError, PackageTransferError
from .package_distribution.inspection import inspect_package as _inspect_package
from .package_distribution.publication import publish_package_artifact
from .package_distribution.transfer_models import PackageDownloadRequest
from .workspace_runtime.discovery import compile_package_source


def inspect_package(
    path: str,
    namespace: str | None = None,
    out_dir: str | None = None,
) -> dict[str, Any]:
    """用产品统一编译器检查并归档一个软件包。

    参数：``path`` 是软件包根；``namespace`` 是仅供遗留包使用的类命名空间；
    ``out_dir`` 是可选产物目录。
    返回：包含规范包目录（PackageCatalog）摘要、遗留 DTO 和归档路径的结果。
    异常：路径、静态编译、归档或遗留投影失败时保持 ``PackageCLIError`` 和既有
    文件系统异常语义；不执行作者驱动代码。
    """

    return _inspect_package(
        path,
        namespace=namespace,
        out_dir=out_dir,
        compile_catalog=compile_package_source,
    )


def build_package(
    path: str,
    out_dir: str | None = None,
    *,
    emit_status: bool = True,
) -> PackageBuildArtifact:
    """构建并自审计一个规范软件包工作区（Package Workspace）。

    参数：``path`` 是软件包根；``out_dir`` 是可选发布产物目录，默认使用工作区
    同级 ``dist``；``emit_status`` 控制人类进度输出。
    返回：已通过 wheel 来源重编译和闭包校验的软件包构建产物。
    异常：工作区、标准构建或自审计失败时统一抛出 ``PackageCLIError``；作者源码
    不会被写入生成目录。
    """

    workspace_root = Path(path).expanduser().resolve()
    output_root = (
        Path(out_dir).expanduser().resolve()
        if out_dir
        else workspace_root.parent / "dist"
    )
    try:
        artifact = build_workspace_package(
            workspace_root,
            output_root,
            compile_catalog=compile_package_source,
        )
    except PackageCompileError as error:
        diagnostic_codes = ", ".join(item.code for item in error.diagnostics)
        raise PackageCLIError(
            f"包目录（PackageCatalog）编译失败：{diagnostic_codes}"
        ) from error
    except (PackageBuildError, TypeError, ValueError) as error:
        raise PackageCLIError(str(error)) from error
    if emit_status:
        print_status(
            "package build 完成："
            f"{artifact.catalog.distribution.name}@"
            f"{artifact.catalog.distribution.version}",
            "info",
        )
        print_status(f"  wheel           : {artifact.wheel}", "info")
        print_status(f"  catalog_digest  : {artifact.catalog.catalog_digest}", "info")
        print_status(f"  artifact_digest : {artifact.artifact_digest}", "info")
    return artifact


def upload_package(
    path: str,
    transfer_port: Any,
    out_dir: str | None = None,
    *,
    environment: str,
) -> dict[str, Any]:
    """构建一次软件包并通过云端 Adapter 发布既有兼容投影。

    参数：``path`` 是软件包根；``transfer_port`` 是 Backend 无关发布 Interface；
    ``out_dir`` 是产物目录；``environment`` 是本次固定环境标签。
    返回：完成设备广场对账的稳定命令结果。
    异常：构建、鉴权、上传、版本冲突或广场对账失败时抛出稳定错误。
    """

    try:
        artifact = build_package(path, out_dir=out_dir, emit_status=False)
    except (PackageCLIError, OSError) as error:
        raise PackageTransferError(
            "package_build_failed",
            f"设备软件包构建失败：{error}",
            retryable=False,
        ) from error
    return publish_package_artifact(
        artifact,
        port=transfer_port,
        environment=environment,
    )


def download_package(
    request: PackageDownloadRequest,
    transfer_port: Any,
    *,
    cache_root: str | Path,
    environment: str,
    out_dir: str | None = None,
    extract_source: str | None = None,
) -> dict[str, Any]:
    """解析、验证并缓存一个远端设备软件包。

    参数：``request`` 是互斥选择器；``transfer_port`` 是远端获取 Interface；
    ``cache_root`` 是内容寻址缓存；``environment`` 是固定环境；``out_dir`` 是可选
    wheel 副本目录；``extract_source`` 是可选派生工作区目标。
    返回：稳定 ``package.download`` 命令结果。
    异常：能力、远端描述、归档、缓存或导出失败时抛出稳定错误；不安装软件包。
    """

    try:
        return acquire_package(
            request,
            port=transfer_port,
            cache=PackageCache(cache_root),
            environment=environment,
            compile_catalog=compile_package_source,
            output_dir=out_dir,
            extract_source=extract_source,
        )
    except PackageTransferError:
        raise
    except OSError as error:
        raise PackageTransferError(
            "package_cache_failed",
            "设备软件包缓存目录不可用",
            retryable=False,
        ) from error


def register_package_subcommands(subparsers: Any) -> None:
    """把软件包命令行（Package CLI）完整注册到公共 ``unilab`` 解析器。

    参数：``subparsers`` 是应用组合根创建的顶层 argparse 子解析器集合。
    返回：无；注册 inspect、build、upload、download 和依赖管理动作。
    异常：重复注册或传入对象不兼容 argparse 时传播原始异常。
    """

    package_parser = subparsers.add_parser(
        "package",
        aliases=["pkg"],
        help="Community package inspect, build, upload, download, and dependency tools",
    )
    actions = package_parser.add_subparsers(
        title="package actions",
        dest="package_action",
    )
    for action_name in ("inspect", "build", "upload"):
        action_parser = actions.add_parser(
            action_name,
            help=(
                "Compile one package and write package catalog artifacts"
                if action_name == "inspect"
                else (
                    "Build and audit one Catalog-embedded wheel"
                    if action_name == "build"
                    else "Build, audit, and upload one package wheel"
                )
            ),
        )
        action_parser.add_argument(
            "--path",
            dest="package_path",
            type=str,
            required=True,
            help="Package workspace containing pyproject.toml",
        )
        if action_name == "inspect":
            action_parser.add_argument(
                "--namespace",
                default=None,
                help="Legacy class namespace override",
            )
        action_parser.add_argument(
            "--out",
            default=None,
            help="Artifact output directory",
        )
        if action_name == "upload":
            action_parser.add_argument(
                "--auth-stdin",
                action="store_true",
                help="Read upload AK/SK from the closed JSON stdin contract",
            )
            action_parser.add_argument(
                "--json",
                action="store_true",
                help="Write one stable JSON document to stdout",
            )

    download_parser = actions.add_parser(
        "download",
        help="Resolve, verify, and cache one package wheel without installing it",
    )
    selector = download_parser.add_mutually_exclusive_group(required=True)
    selector.add_argument(
        "--template-uuid",
        help="Exact device square template UUID",
    )
    selector.add_argument(
        "--package",
        dest="package_name",
        help="Package distribution name in the device square",
    )
    download_parser.add_argument(
        "--version",
        default=None,
        help="Exact version used together with --package",
    )
    download_parser.add_argument(
        "--out",
        default=None,
        help="Optional directory receiving a verified wheel copy",
    )
    download_parser.add_argument(
        "--extract-source",
        default=None,
        help="Optional new directory receiving a derived Package Workspace",
    )
    download_parser.add_argument(
        "--json",
        action="store_true",
        help="Write one stable JSON document to stdout",
    )

    add_parser = actions.add_parser(
        "add",
        help="Add and lock an explicit external package workspace",
    )
    add_parser.add_argument(
        "dependency_source",
        help="External Uni-Lab package workspace path",
    )
    _add_dependency_workspace_flag(add_parser)

    update_parser = actions.add_parser(
        "update",
        help="Recompile and relock one explicit package dependency",
    )
    update_parser.add_argument(
        "dependency_identity",
        help="Distribution name, normalized name, or community namespace",
    )
    update_parser.add_argument(
        "dependency_source",
        nargs="?",
        default="",
        help="Optional replacement package workspace path",
    )
    _add_dependency_workspace_flag(update_parser)

    remove_parser = actions.add_parser(
        "remove",
        help="Remove one explicit package dependency",
    )
    remove_parser.add_argument(
        "dependency_identity",
        help="Distribution name, normalized name, or community namespace",
    )
    _add_dependency_workspace_flag(remove_parser)


def _add_dependency_workspace_flag(parser: argparse.ArgumentParser) -> None:
    """为一个依赖管理子命令增加当前路径缺省的工作区选择。

    参数：``parser`` 是 add、update 或 remove 子解析器。
    返回：无；增加与顶层 ``--workspace`` 相同目标字段的可选参数。
    异常：解析器字段冲突时传播 argparse 原始异常。
    """

    parser.add_argument(
        "--workspace",
        nargs="?",
        const=".",
        default=argparse.SUPPRESS,
        help="Main workspace root (default: current directory)",
    )


def cmd_package(
    args_dict: dict[str, Any],
    transfer_port: Any = None,
    *,
    environment: str = "",
    cache_root: str | Path | None = None,
) -> dict[str, Any] | None:
    """分派一次软件包子命令。

    参数：``args_dict`` 是公共命令行解析结果；``transfer_port`` 是远端动作使用的
    Adapter；``environment`` 与 ``cache_root`` 由应用组合根固定。
    返回：远端动作返回稳定结果字典，本地动作返回 ``None``。
    异常：动作、路径、显式依赖或具体操作无效时抛出 ``PackageCLIError``；
    依赖管理绝不扫描或安装 ambient site-packages。
    """

    action = args_dict.get("package_action")
    package_path = args_dict.get("package_path")
    namespace = args_dict.get("namespace")
    output_directory = args_dict.get("out")

    if not action:
        raise PackageCLIError(
            "缺少 package 子动作，请使用 "
            "`unilab package inspect|build|upload|download|add|update|remove`"
        )
    if action in {"add", "update", "remove"}:
        try:
            manager = PackageDependencyManager(
                args_dict.get("workspace") or ".",
                compile_catalog=compile_package_source,
            )
            if action == "add":
                result = manager.add(args_dict.get("dependency_source") or "")
            elif action == "update":
                result = manager.update(
                    args_dict.get("dependency_identity") or "",
                    args_dict.get("dependency_source") or None,
                )
            else:
                result = manager.remove(args_dict.get("dependency_identity") or "")
        except (RuntimeError, TypeError, ValueError) as error:
            raise PackageCLIError(str(error)) from error
        print_status(
            f"package {action} 完成：锁定 {len(result.packages)} 个显式外部包",
            "info",
        )
        return None
    if action == "download":
        if transfer_port is None or cache_root is None or not environment:
            raise PackageTransferError(
                "command_context_missing",
                "package download 缺少固定远端命令上下文",
                retryable=False,
            )
        try:
            request = PackageDownloadRequest(
                template_uuid=args_dict.get("template_uuid"),
                package_name=args_dict.get("package_name"),
                version=args_dict.get("version"),
            )
        except ValueError as error:
            raise PackageTransferError(
                "invalid_selector",
                str(error),
                retryable=False,
            ) from error
        remote_result = download_package(
            request,
            transfer_port,
            cache_root=cache_root,
            environment=environment,
            out_dir=output_directory,
            extract_source=args_dict.get("extract_source"),
        )
        _emit_remote_result(remote_result, json_output=bool(args_dict.get("json")))
        return remote_result
    if not package_path:
        raise PackageCLIError("缺少 --path（社区软件包工作区路径）")
    if action == "inspect":
        inspect_package(package_path, namespace=namespace, out_dir=output_directory)
        return None
    if action == "build":
        build_package(package_path, out_dir=output_directory)
        return None
    if action == "upload":
        if transfer_port is None or not environment:
            raise PackageTransferError(
                "command_context_missing",
                "package upload 缺少固定远端命令上下文",
                retryable=False,
            )
        remote_result = upload_package(
            package_path,
            transfer_port,
            out_dir=output_directory,
            environment=environment,
        )
        _emit_remote_result(remote_result, json_output=bool(args_dict.get("json")))
        return remote_result
    raise PackageCLIError(f"未知 package 子动作：{action}")


def _emit_remote_result(result: dict[str, Any], *, json_output: bool) -> None:
    """输出一次上传或下载的最终结果。

    参数：``result`` 是稳定命令字典；``json_output`` 控制机器或人类模式。
    返回：无。
    异常：stdout 不可写时传播原始 IO 异常。
    """

    if json_output:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return
    print_status(
        f"{result['command']} 完成：{result['distribution']}@{result['version']} "
        f"({result['status']})",
        "info",
    )
    print_status(f"  environment     : {result['environment']}", "info")
    print_status(f"  artifact_digest : {result['artifact_digest']}", "info")


__all__ = [
    "PackageCLIError",
    "build_package",
    "cmd_package",
    "download_package",
    "inspect_package",
    "register_package_subcommands",
    "upload_package",
]
