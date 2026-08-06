"""设备包上传的一次性 stdin 凭据合同。"""

from __future__ import annotations

import base64
import json
import sys
from dataclasses import dataclass
from typing import Any, TextIO


UPLOAD_AUTH_SCHEMA = "unilab-package-upload-auth/v1"


class PackageUploadAuthError(ValueError):
    """表示上传凭据缺失或不符合封闭 stdin 合同。"""


@dataclass(frozen=True)
class PackageUploadCredentials:
    """仅在一次 CLI 进程内存活的 Lab AK/SK。"""

    ak: str
    sk: str

    def auth_secret(self) -> str:
        """生成现有 Lab Authorization 使用的可逆 Base64 载荷。

        返回值是 ``base64(ak:sk)``，只允许交给 HTTP Authorization 头；它不是
        加密结果，不得写入日志、文件或命令参数。
        """

        payload = f"{self.ak}:{self.sk}".encode("utf-8")
        return base64.b64encode(payload).decode("ascii")


def read_package_upload_credentials(
    input_stream: TextIO | None = None,
) -> PackageUploadCredentials:
    """从 stdin 读取并校验一次性设备包上传凭据。

    参数 ``input_stream`` 是 Electron Main 写入的单个 JSON 文档；省略时读取进程
    stdin。返回值包含当前上传唯一可用的 AK/SK。JSON 非法、字段缺失、出现额外
    字段或凭据为空时抛出 :class:`PackageUploadAuthError`，错误正文绝不回显输入。
    """

    source = input_stream or sys.stdin
    try:
        payload: Any = json.load(source)
    except (json.JSONDecodeError, OSError, UnicodeError) as exc:
        raise PackageUploadAuthError("上传凭据 stdin 不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise PackageUploadAuthError("上传凭据 stdin 必须是 JSON object")
    expected_fields = {"schema_version", "ak", "sk"}
    if set(payload) != expected_fields:
        raise PackageUploadAuthError("上传凭据 stdin 字段合同无效")
    if payload.get("schema_version") != UPLOAD_AUTH_SCHEMA:
        raise PackageUploadAuthError("上传凭据 stdin schema_version 不受支持")
    ak = _credential(payload.get("ak"), "AK")
    sk = _credential(payload.get("sk"), "SK")
    return PackageUploadCredentials(ak=ak, sk=sk)


def _credential(value: Any, label: str) -> str:
    """校验一个不回显原值的非空上传凭据字段。

    参数 ``value`` 是未知 JSON 值，``label`` 只用于安全错误名称。返回去除首尾
    空白的凭据；类型错误、空值或超过 1024 字符时抛出
    :class:`PackageUploadAuthError`。
    """

    if not isinstance(value, str) or not value.strip() or len(value) > 1024:
        raise PackageUploadAuthError(f"上传凭据 {label} 无效")
    return value.strip()


__all__ = [
    "PackageUploadAuthError",
    "PackageUploadCredentials",
    "UPLOAD_AUTH_SCHEMA",
    "read_package_upload_credentials",
]
