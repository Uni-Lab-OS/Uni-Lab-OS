"""设备软件包远端命令的环境、凭据和缓存组合根。"""

from __future__ import annotations

import base64
import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unilabos.client import SessionManager
from unilabos.package_manager.package_distribution.adapters.legacy_backend import (
    LegacyTemplateBackendAdapter,
)
from unilabos.package_manager.package_distribution.environment import (
    PackageCloudEnvironment,
    resolve_package_environment,
)
from unilabos.package_manager.package_distribution.errors import PackageTransferError


@dataclass(frozen=True, slots=True)
class PackageCloudCommandContext:
    """应用组合根交给包管理深模块的固定远端上下文。"""

    environment: PackageCloudEnvironment
    adapter: LegacyTemplateBackendAdapter
    cache_root: Path


@contextmanager
def package_cloud_command_context(
    args: dict[str, Any],
    *,
    require_auth: bool,
) -> Iterator[PackageCloudCommandContext]:
    """解析一次环境和凭据并管理短生命周期 HTTP Adapter。

    参数：``args`` 是 argparse 结果；``require_auth`` 在上传时为 ``True``。
    返回：上下文期间提供固定环境、Adapter 和缓存根。
    异常：环境、stdin、会话或凭据无效时抛出稳定 ``PackageTransferError``；
    ``--auth-stdin`` 会在任何 Python 本地配置加载前处理。
    """

    environment = resolve_package_environment(args.get("addr"))
    auth_secret = _resolve_package_auth(
        args,
        environment=environment,
        required=require_auth,
    )
    adapter = LegacyTemplateBackendAdapter(
        environment.base_url,
        auth_secret=auth_secret,
    )
    try:
        yield PackageCloudCommandContext(
            environment=environment,
            adapter=adapter,
            cache_root=_resolve_cache_root(args),
        )
    finally:
        adapter.close()


def _resolve_package_auth(
    args: dict[str, Any],
    *,
    environment: PackageCloudEnvironment,
    required: bool,
) -> str:
    """按 stdin、argv、session、本地配置顺序解析一对 AK/SK。

    参数：``args`` 是命令行结果；``environment`` 是本次固定环境；``required``
    控制无凭据是否失败。
    返回：短生命周期 ``base64(ak:sk)``，公开下载无凭据时为空。
    异常：凭据不成对、来源环境不匹配或必需凭据缺失时抛出稳定错误。
    """

    if args.get("auth_stdin"):
        ak, sk = _read_auth_stdin()
        return _encode_auth(ak, sk)
    cli_ak = str(args.get("ak") or "")
    cli_sk = str(args.get("sk") or "")
    if bool(cli_ak) != bool(cli_sk):
        raise PackageTransferError(
            "authentication_invalid",
            "显式 AK/SK 必须成对提供",
            retryable=False,
        )
    if cli_ak:
        return _encode_auth(cli_ak, cli_sk)
    if not required:
        # 当前遗留包列表、模板详情和 302 下载路由均是公开只读接口；下载命令不为
        # 无需使用的凭据创建或改写会话文件。
        return ""

    session_root = _resolve_session_root(args)
    if session_root.joinpath("session.json").is_file():
        manager = SessionManager(working_dir=str(session_root))
        with manager:
            state = manager.get_state()
            if state.auth.ak and state.auth.sk:
                session_environment = resolve_package_environment(state.base_url)
                if session_environment.base_url != environment.base_url:
                    raise PackageTransferError(
                        "authentication_environment_mismatch",
                        "login session 与本次 --addr 环境不一致",
                        retryable=False,
                    )
                return _encode_auth(state.auth.ak, state.auth.sk)

    from unilabos.app.cli.auth_resolver import _try_load_local_config

    config = _try_load_local_config(str(session_root)) or {}
    config_ak = str(config.get("ak") or "")
    config_sk = str(config.get("sk") or "")
    if bool(config_ak) != bool(config_sk):
        raise PackageTransferError(
            "authentication_invalid",
            "local_config.py 的 AK/SK 必须成对配置",
            retryable=False,
        )
    if config_ak:
        configured_url = str(config.get("base_url") or environment.base_url)
        configured_environment = resolve_package_environment(configured_url)
        if configured_environment.base_url != environment.base_url:
            raise PackageTransferError(
                "authentication_environment_mismatch",
                "local_config.py 凭据环境与本次 --addr 不一致",
                retryable=False,
            )
        return _encode_auth(config_ak, config_sk)
    if required:
        raise PackageTransferError(
            "authentication_required",
            "package upload 需要 login session、--auth-stdin 或显式 AK/SK",
            retryable=False,
        )
    return ""


def _read_auth_stdin() -> tuple[str, str]:
    """从关闭式 stdin JSON 合同读取一对上传凭据。

    参数：无。
    返回：AK 和 SK。
    异常：输入过大、JSON、schema 或字段无效时抛出稳定错误；不回显原文。
    """

    payload = sys.stdin.buffer.read(16 * 1024 + 1)
    if len(payload) > 16 * 1024:
        raise PackageTransferError(
            "authentication_invalid",
            "--auth-stdin 输入超过大小上限",
            retryable=False,
        )
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise PackageTransferError(
            "authentication_invalid",
            "--auth-stdin 必须是合法 UTF-8 JSON",
            retryable=False,
        ) from error
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "ak",
        "sk",
    }:
        raise PackageTransferError(
            "authentication_invalid",
            "--auth-stdin 字段集合无效",
            retryable=False,
        )
    if document["schema_version"] != "unilab-package-upload-auth/v1":
        raise PackageTransferError(
            "authentication_invalid",
            "--auth-stdin schema_version 不受支持",
            retryable=False,
        )
    ak, sk = document["ak"], document["sk"]
    if not isinstance(ak, str) or not ak or not isinstance(sk, str) or not sk:
        raise PackageTransferError(
            "authentication_invalid",
            "--auth-stdin AK/SK 必须是非空字符串",
            retryable=False,
        )
    return ak, sk


def _encode_auth(ak: str, sk: str) -> str:
    """短生命周期编码一对 Lab 凭据。

    参数：``ak`` 和 ``sk`` 是完整凭据。
    返回：Base64 文本；调用方不得持久化或记录。
    异常：编码失败时传播标准库异常。
    """

    return base64.b64encode(f"{ak}:{sk}".encode()).decode("ascii")


def _resolve_session_root(args: dict[str, Any]) -> Path:
    """选择与现有 login 命令兼容的会话目录。

    参数：``args`` 是命令行结果。
    返回：显式工作目录，或当前目录下既有 ``unilabos_data``/当前目录。
    异常：无；返回绝对路径但不主动解析不存在的符号链接成员。
    """

    if args.get("working_dir"):
        return Path(str(args["working_dir"])).expanduser().absolute()
    current = Path.cwd().absolute()
    legacy = current / "unilabos_data"
    return legacy if legacy.is_dir() else current


def _resolve_cache_root(args: dict[str, Any]) -> Path:
    """选择本次命令的受管 ``package-cache/v1`` 根。

    参数：``args`` 是命令行结果。
    返回：显式工作目录或当前目录 ``.unilabos`` 下的缓存根。
    异常：路径规范化失败时传播原始异常。
    """

    if args.get("working_dir"):
        managed = Path(str(args["working_dir"])).expanduser().resolve()
    else:
        managed = Path(os.getcwd()).resolve() / ".unilabos"
    return managed / "package-cache" / "v1"


__all__ = ["PackageCloudCommandContext", "package_cloud_command_context"]
