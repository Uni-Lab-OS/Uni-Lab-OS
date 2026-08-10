"""遗留启动期社区包到统一可信 acquisition 的组合桥。"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from unilabos.package_manager.package_distribution import (
    LegacyTemplateBackendAdapter,
    PackageCache,
    PackageDownloadRequest,
    acquire_package,
)
from unilabos.package_manager.package_distribution.environment import (
    resolve_package_environment,
)
from unilabos.package_manager.workspace_runtime import compile_package_source
from unilabos.utils import logger
from unilabos.utils.banner_print import print_status


class CommunityAcquisitionError(RuntimeError):
    """表示启动期社区包无法通过统一可信获取链准备。"""


def acquire_community_workspace(
    package_info: dict[str, Any],
    *,
    working_dir: str | Path,
    namespace: str,
    version: str,
    http_client: Any,
) -> tuple[Path, dict[str, Any]]:
    """通过统一远端获取深模块原子更新遗留启动期工作区缓存。

    参数：``package_info`` 给出包名；``working_dir`` 是受管目录；``namespace``
    和 ``version`` 是目标身份；``http_client`` 只提供当前产品 Backend API 根。
    返回：最终派生工作区与稳定 acquisition 结果。
    异常：缺少环境/身份、三摘要校验、源码导出或原子替换失败时抛出
    ``CommunityAcquisitionError``；旧目录在新代完整就绪前保持不变。
    """

    distribution = package_info.get("name")
    base_url = getattr(http_client, "remote_addr", "")
    if not isinstance(distribution, str) or not distribution or version == "unknown":
        raise CommunityAcquisitionError("远端社区包缺少发行名或版本")
    if not isinstance(base_url, str) or not base_url:
        raise CommunityAcquisitionError("远端社区包可信获取缺少 Backend API 根")
    environment = resolve_package_environment(base_url)
    normalized = _normalize_package_dir_name(namespace)
    target_root = Path(working_dir) / "community_devices" / normalized / version
    target_root.parent.mkdir(parents=True, exist_ok=True)
    if target_root.is_symlink():
        raise CommunityAcquisitionError("community package 受管目标不得是符号链接")
    incoming_root = target_root.parent / f".{version}.incoming-{uuid4().hex}"
    backup_root = target_root.parent / f".{version}.previous-{uuid4().hex}"
    adapter = LegacyTemplateBackendAdapter(environment.base_url)
    previous_moved = False

    try:
        print_status(f"下载 community 设备包 {namespace}@{version}", "info")
        acquisition = acquire_package(
            PackageDownloadRequest(package_name=distribution, version=version),
            port=adapter,
            cache=PackageCache(Path(working_dir) / "package-cache" / "v1"),
            environment=environment.name,
            compile_catalog=compile_package_source,
            extract_source=incoming_root / "package",
        )
        if target_root.exists():
            target_root.replace(backup_root)
            previous_moved = True
        try:
            incoming_root.replace(target_root)
        except Exception:
            if previous_moved and not target_root.exists():
                backup_root.replace(target_root)
                previous_moved = False
            raise
        if previous_moved:
            shutil.rmtree(backup_root, ignore_errors=True)
            previous_moved = False
        package_dir = target_root / "package"
        logger.trace(
            f"[CommunityPackage] 可信 acquisition 已提交: {namespace}@{version} "
            f"cache_key={acquisition['cache_key']} dir={package_dir}"
        )
        return package_dir, acquisition
    finally:
        try:
            adapter.close()
        finally:
            shutil.rmtree(incoming_root, ignore_errors=True)
            if previous_moved and backup_root.exists() and not target_root.exists():
                backup_root.replace(target_root)


def safe_package_info(
    package_info: dict[str, Any],
    acquisition: dict[str, Any],
) -> dict[str, Any]:
    """生成不持久化签名 URL 或对象键的启动期包身份快照。

    参数：``package_info`` 是 resolve 投影；``acquisition`` 是可信下载结果。
    返回：仅含发行身份、依赖和三摘要的新字典。
    异常：无；缺失的可选展示字段被省略。
    """

    allowed = {
        "name",
        "normalized_name",
        "class_namespace",
        "dependencies",
    }
    result = {key: package_info[key] for key in allowed if key in package_info}
    result.update(
        {
            "version": acquisition["version"],
            "artifact_digest": acquisition["artifact_digest"],
            "catalog_digest": acquisition["catalog_digest"],
            "content_digest": acquisition["content_digest"],
        }
    )
    return result


def _normalize_package_dir_name(namespace: str) -> str:
    """把社区命名空间转为迁移期受管目录名。

    参数：``namespace`` 是 ``community.*`` 命名空间。
    返回：移除前缀并把点、下划线改为连字符的目录名。
    异常：无。
    """

    return namespace.replace("community.", "", 1).replace(".", "-").replace("_", "-")


__all__ = [
    "CommunityAcquisitionError",
    "acquire_community_workspace",
    "safe_package_info",
]
