"""设备软件包命令的一次性云端环境解析。"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from .errors import PackageTransferError

PRODUCTION_API_ROOT = "https://leap-lab.bohrium.com/api/v1"
TEST_API_ROOT = "https://leap-lab.test.bohrium.com/api/v1"
UAT_API_ROOT = "https://leap-lab.uat.bohrium.com/api/v1"
LOCAL_API_ROOT = "http://127.0.0.1:48197/api/v1"

_ALIASES = {
    "test": ("test", TEST_API_ROOT),
    "uat": ("uat", UAT_API_ROOT),
    "prod": ("prod", PRODUCTION_API_ROOT),
    "production": ("prod", PRODUCTION_API_ROOT),
    "local": ("local", LOCAL_API_ROOT),
}
_KNOWN_URLS = {url: label for label, url in _ALIASES.values()}


@dataclass(frozen=True, slots=True)
class PackageCloudEnvironment:
    """一次命令冻结的环境标签和 Backend API 根。"""

    name: str
    base_url: str


def resolve_package_environment(value: str | None) -> PackageCloudEnvironment:
    """解析环境别名或完整 API 根且绝不跨环境回退。

    参数：``value`` 是全局 ``--addr``；省略或空值固定为正式环境。
    返回：固定环境标签和地址。
    异常：完整 URL 含 userinfo/query/fragment 或缺少 ``/api/v1`` 时抛出
    ``PackageTransferError``；不会猜测 Cloud Web 根。
    """

    selected = (value or "prod").strip()
    if selected in _ALIASES:
        name, url = _ALIASES[selected]
        return PackageCloudEnvironment(name=name, base_url=url)
    normalized = selected.rstrip("/")
    if normalized in _KNOWN_URLS:
        return PackageCloudEnvironment(
            name=_KNOWN_URLS[normalized],
            base_url=normalized,
        )
    parsed = urlsplit(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.path.endswith("/api/v1")
    ):
        raise PackageTransferError(
            "invalid_environment",
            "--addr 必须是 test/uat/prod/production/local 或完整 /api/v1 根",
            retryable=False,
        )
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise PackageTransferError(
            "invalid_environment",
            "只有显式本地测试地址允许 HTTP",
            retryable=False,
        )
    return PackageCloudEnvironment(name="local" if parsed.scheme == "http" else "custom", base_url=normalized)


__all__ = [
    "LOCAL_API_ROOT",
    "PRODUCTION_API_ROOT",
    "TEST_API_ROOT",
    "UAT_API_ROOT",
    "PackageCloudEnvironment",
    "resolve_package_environment",
]
