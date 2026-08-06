"""软件包命令行（Package CLI）的公共分派入口。"""

from __future__ import annotations

from typing import Any

from .errors import PackageCLIError
from .inspection import inspect_package
from .installation import install_package
from .publication import upload_package


def cmd_package(args_dict: dict[str, Any], http_client: Any = None) -> None:
    """分派一次软件包子命令。

    参数：``args_dict`` 是公共命令行解析结果；``http_client`` 是仅发布
    动作需要的可选鉴权 HTTP 适配器。
    返回：无；成功结果由具体子命令输出。
    异常：动作、路径或具体操作无效时抛出 ``PackageCLIError``。
    """

    action = args_dict.get("package_action")
    package_path = args_dict.get("package_path")
    namespace = args_dict.get("namespace")
    output_directory = args_dict.get("out")

    if not action:
        raise PackageCLIError(
            "缺少 package 子动作，请使用 `unilab package inspect|upload|install`"
        )
    if action == "install":
        install_package(
            args_dict.get("install_spec", "") or "",
            run_inspect=not args_dict.get("no_inspect", False),
        )
        return
    if not package_path:
        raise PackageCLIError("缺少 --path（社区软件包目录）")
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


__all__ = ["PackageCLIError", "cmd_package"]
