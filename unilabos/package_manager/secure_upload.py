"""不加载 Runtime 或 Python 配置的设备包安全上传入口。"""

from __future__ import annotations

import sys
from typing import Any, TextIO

from .cli import (
    PackageCommandError,
    resolve_package_remote_addr,
    resolve_package_working_dir,
    run_package_command,
)
from .publication_http import PackagePublicationHttpClient
from .upload_auth import PackageUploadAuthError, read_package_upload_credentials


def run_secure_package_upload(
    args: dict[str, Any],
    *,
    input_stream: TextIO | None = None,
) -> None:
    """通过一次性 stdin AK/SK 执行设备包发布且不加载 Python 配置。

    参数 ``args`` 是根 argparse 产生的命令投影，其中云端地址和受管工作目录是
    非秘密参数；``input_stream`` 是 Electron Main 写入的凭据 JSON，省略时读取
    stdin。函数成功时无返回值；凭据、地址、构建或发布失败时向 stderr 输出不含
    秘密的诊断并以退出码 2 终止。短生命周期 HTTP client 始终在结束时关闭。
    """

    client: PackagePublicationHttpClient | None = None
    try:
        credentials = read_package_upload_credentials(input_stream or sys.stdin)
        client = PackagePublicationHttpClient(
            base_url=resolve_package_remote_addr(args.get("addr")),
            auth_secret=credentials.auth_secret(),
            working_dir=resolve_package_working_dir(args.get("working_dir")),
        )
        run_package_command(args, http_client=client)
    except (
        OSError,
        PackageCommandError,
        PackageUploadAuthError,
        ValueError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
    finally:
        if client is not None:
            client.close()


__all__ = ["run_secure_package_upload"]
