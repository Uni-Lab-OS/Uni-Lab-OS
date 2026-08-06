"""软件包命令行（Package CLI）的公共分派入口。"""

from __future__ import annotations

import argparse
from typing import Any

from unilabos.utils.banner_print import print_status

from .dependency_lock import PackageDependencyManager
from .errors import PackageCLIError
from .inspection import inspect_package
from .publication import upload_package


def register_package_subcommands(subparsers: Any) -> None:
    """把软件包命令行（Package CLI）完整注册到公共 ``unilab`` 解析器。

    参数：``subparsers`` 是应用组合根（Composition Root）创建的顶层 argparse
    子解析器集合。
    返回：无；注册 ``package``/``pkg`` 及 inspect、upload、add、update、remove。
    异常：重复注册或传入对象不兼容 argparse 时传播原始异常。依赖管理动作都
    支持末尾 ``--workspace [PATH]``，省略 PATH 时使用当前目录。
    """

    package_parser = subparsers.add_parser(
        "package",
        aliases=["pkg"],
        help="Community package inspect, upload, and explicit dependency tools",
    )
    actions = package_parser.add_subparsers(
        title="package actions",
        dest="package_action",
    )
    for action_name in ("inspect", "upload"):
        action_parser = actions.add_parser(
            action_name,
            help=(
                "Compile one package and write package catalog artifacts"
                if action_name == "inspect"
                else "Inspect and upload one package artifact"
            ),
        )
        action_parser.add_argument(
            "--path",
            dest="package_path",
            type=str,
            required=True,
            help="Package workspace containing pyproject.toml",
        )
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
                "--download-url",
                dest="download_url",
                default="",
                help="Explicit reachable artifact URL",
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


def cmd_package(args_dict: dict[str, Any], http_client: Any = None) -> None:
    """分派一次软件包子命令。

    参数：``args_dict`` 是公共命令行解析结果；``http_client`` 是仅发布
    动作需要的可选鉴权 HTTP 适配器。
    返回：无；成功结果由具体子命令输出。
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
            "`unilab package inspect|upload|add|update|remove`"
        )
    if action in {"add", "update", "remove"}:
        try:
            manager = PackageDependencyManager(args_dict.get("workspace") or ".")
            if action == "add":
                result = manager.add(args_dict.get("dependency_source") or "")
            elif action == "update":
                replacement_source = args_dict.get("dependency_source") or None
                result = manager.update(
                    args_dict.get("dependency_identity") or "",
                    replacement_source,
                )
            else:
                result = manager.remove(
                    args_dict.get("dependency_identity") or ""
                )
        except (RuntimeError, TypeError, ValueError) as error:
            raise PackageCLIError(str(error)) from error
        # ``result`` 是命令成功切换后的完整软件包依赖锁代际。
        print_status(
            f"package {action} 完成：锁定 {len(result.packages)} 个显式外部包",
            "info",
        )
        return
    if not package_path:
        raise PackageCLIError("缺少 --path（社区软件包工作区路径）")
    if action == "inspect":
        inspect_package(
            package_path,
            namespace=namespace,
            out_dir=output_directory,
        )
        return
    if action == "upload":
        upload_package(
            package_path,
            http_client=http_client,
            namespace=namespace,
            out_dir=output_directory,
            download_url=args_dict.get("download_url", "") or "",
        )
        return
    raise PackageCLIError(f"未知 package 子动作：{action}")


__all__ = ["PackageCLIError", "cmd_package", "register_package_subcommands"]
