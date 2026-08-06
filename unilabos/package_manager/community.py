"""Resolve Graph community references into explicit cached-wheel Sources."""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .catalog import PackageCatalog, PackageCompileError
from .compiler import compile_package_source
from .sources import CachedArchiveSource

COMMUNITY_PREFIX = "community."
_CACHE_DIR = "community_packages"
_INDEX_NAME = "cache-index.json"
_NAMESPACE = re.compile(r"^community\.[a-z_][a-z0-9_]*$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024


class CommunityPackageError(RuntimeError):
    """A Graph-selected community namespace could not produce a valid Catalog."""


class CommunityDownloadPort(Protocol):
    """把远端设备包字节写入调用方指定临时文件的下载端口。"""

    def download(self, url: str, destination: Path) -> None:
        """下载 URL 到目标文件；失败时抛出异常且不得伪造成功文件。"""


class CommunityResolvePort(CommunityDownloadPort, Protocol):
    """解析 Graph 包引用并下载命中 Artifact 的云端端口。"""

    def resolve(
        self,
        classes: list[str],
        current_packages: list[dict[str, str]],
    ) -> list[dict[str, Any]]: ...


class HttpClientCommunityAdapter:
    def __init__(self, http_client: Any) -> None:
        self._http_client = http_client

    def resolve(
        self,
        classes: list[str],
        current_packages: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        response = self._http_client.resolve_community_packages(
            classes,
            current_packages=current_packages,
        )
        data = response.get("data", response) if isinstance(response, dict) else []
        return (
            [item for item in data if isinstance(item, dict)]
            if isinstance(data, list)
            else []
        )

    def download(self, url: str, destination: Path) -> None:
        import requests

        requester = getattr(self._http_client, "_session", None) or requests
        with requester.get(url, stream=True, timeout=(5, 120)) as response:
            response.raise_for_status()
            total = 0
            with destination.open("wb") as stream:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        total += len(chunk)
                        if total > _MAX_DOWNLOAD_BYTES:
                            raise CommunityPackageError(
                                "community wheel 超过下载大小上限"
                            )
                        stream.write(chunk)


class RequestsCommunityDownloadAdapter:
    """使用 requests 跟随公开下载重定向的无鉴权设备包下载适配器。"""

    def __init__(self, requester: Any = None) -> None:
        """保存可注入的 requests 兼容客户端，便于 CLI 与测试复用。"""

        self._requester = requester

    def download(self, url: str, destination: Path) -> None:
        """流式下载 wheel，并在超过大小上限或网络失败时关闭失败。"""

        import requests

        requester = self._requester or requests
        try:
            with requester.get(url, stream=True, timeout=(5, 120)) as response:
                response.raise_for_status()
                total = 0
                with destination.open("wb") as stream:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > _MAX_DOWNLOAD_BYTES:
                            raise CommunityPackageError(
                                "community wheel 超过下载大小上限"
                            )
                        stream.write(chunk)
        except requests.RequestException as exc:
            raise CommunityPackageError(f"设备包下载失败: {exc}") from exc


@dataclass(frozen=True)
class CommunityPackageResolution:
    sources: tuple[CachedArchiveSource, ...] = ()
    catalogs: tuple[PackageCatalog, ...] = ()
    classes: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()


@dataclass(frozen=True)
class CommunityPackageAcquisition:
    """一个经过 Artifact 与 Catalog 双重校验的受管缓存结果。"""

    source: CachedArchiveSource
    catalog: PackageCatalog
    cache_hit: bool


def resolve_graph_packages(
    graph_data: dict[str, Any] | None,
    *,
    working_dir: str | Path,
    port: CommunityResolvePort | None = None,
    available_catalogs: Iterable[PackageCatalog] = (),
) -> CommunityPackageResolution:
    """解析 Graph 中的 community class，并返回可激活的显式包来源。"""

    classes = tuple(extract_community_classes(graph_data))
    if not classes:
        return CommunityPackageResolution()
    pinned_cache_keys = _graph_package_cache_keys(graph_data)
    available = _catalogs_by_namespace(available_catalogs)
    sources: list[CachedArchiveSource] = []
    catalogs: list[PackageCatalog] = []
    dependencies: list[str] = []
    for namespace, cache_key in sorted(pinned_cache_keys.items()):
        acquisition = load_cached_community_package(
            cache_key=cache_key,
            working_dir=working_dir,
        )
        _validate_graph_classes(
            acquisition.catalog,
            (item for item in classes if _community_namespace(item) == namespace),
        )
        sources.append(acquisition.source)
        catalogs.append(acquisition.catalog)
        dependencies.extend(acquisition.catalog.distribution.dependencies)
    unresolved_classes: list[str] = []
    for class_name in classes:
        namespace = _community_namespace(class_name)
        if namespace in pinned_cache_keys:
            continue
        catalog = available.get(namespace)
        if catalog is None:
            unresolved_classes.append(class_name)
            continue
        definitions = {
            definition.fqid
            for collection in (
                catalog.definitions.devices,
                catalog.definitions.resources,
            )
            for definition in collection
        }
        if class_name not in definitions:
            raise CommunityPackageError(
                f"Graph class {class_name} 在已加载的 {namespace} Catalog 中不存在"
            )
    if not unresolved_classes:
        return CommunityPackageResolution(
            sources=tuple(sources),
            catalogs=tuple(catalogs),
            classes=classes,
            dependencies=tuple(dict.fromkeys(dependencies)),
        )

    namespaces = sorted({_community_namespace(item) for item in unresolved_classes})
    cache_root = _prepare_cache_root(working_dir)
    index = _load_index(cache_root)
    current = []
    for namespace, info in sorted(index.get("packages", {}).items()):
        if not isinstance(info, dict) or not _NAMESPACE.fullmatch(str(namespace)):
            continue
        current.append(
            {
                "class_namespace": namespace,
                "version": str(info.get("version") or ""),
                "sha256": str(info.get("artifact_digest") or ""),
            }
        )
    remote_items = port.resolve(unresolved_classes, current) if port is not None else []
    by_namespace: dict[str, dict[str, Any]] = {}
    for item in remote_items:
        namespace = _item_namespace(item)
        if not namespace:
            continue
        _validate_namespace(namespace)
        if namespace in by_namespace:
            raise CommunityPackageError(
                f"远端返回重复 community namespace: {namespace}"
            )
        by_namespace[namespace] = item

    for namespace in namespaces:
        item = by_namespace.get(namespace)
        if item is not None:
            source = _cache_remote_item(
                namespace,
                item,
                cache_root=cache_root,
                port=port,
            )
        else:
            source = _cached_source(namespace, index, cache_root=cache_root)
        catalog = compile_package_source(source)
        if catalog.namespace != namespace:
            raise CommunityPackageError(
                f"community namespace 与 wheel Catalog 不一致: "
                f"{namespace} != {catalog.namespace}"
            )
        package_dependencies = list(catalog.distribution.dependencies)
        if item is not None:
            cache_item = {
                "version": _item_version(item),
                "artifact_digest": source.expected_digest,
                "wheel": source.wheel.resolve().relative_to(cache_root).as_posix(),
                "dependencies": package_dependencies,
            }
            index.setdefault("packages", {})[namespace] = cache_item
            _record_cache_release(index, namespace, cache_item)
        sources.append(source)
        catalogs.append(catalog)
        dependencies.extend(package_dependencies)

    _save_index(cache_root, index)
    return CommunityPackageResolution(
        sources=tuple(sources),
        catalogs=tuple(catalogs),
        classes=classes,
        dependencies=tuple(dict.fromkeys(dependencies)),
    )


def acquire_community_package(
    *,
    namespace: str,
    artifact_digest: str,
    download_url: str,
    working_dir: str | Path,
    port: CommunityDownloadPort,
    catalog_validator: Callable[[PackageCatalog], None] | None = None,
) -> CommunityPackageAcquisition:
    """下载并发布一个 community wheel 到现有受管缓存。

    参数中的 namespace 与摘要来自已重新读取的云端设备详情；函数会先尝试
    命中同摘要缓存，再下载到缓存内临时目录，验证 Artifact、嵌入式 Catalog
    和 namespace 后按 Catalog 版本原子发布。任何校验失败都不会更新索引。
    """

    _validate_namespace(namespace)
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", artifact_digest):
        raise CommunityPackageError("artifact_digest 必须是小写 sha256 摘要")
    if not download_url:
        raise CommunityPackageError("设备包下载 URL 不能为空")

    cache_root = _prepare_cache_root(working_dir)
    index = _load_index(cache_root)
    cached = index.get("packages", {}).get(namespace)
    if isinstance(cached, dict) and cached.get("artifact_digest") == artifact_digest:
        try:
            source = _cached_source(namespace, index, cache_root=cache_root)
            catalog = compile_package_source(source)
            _validate_catalog_namespace(catalog, namespace)
            if catalog_validator is not None:
                catalog_validator(catalog)
            _record_cache_release(
                index,
                namespace,
                _cache_item(cache_root, source, catalog),
            )
            _save_index(cache_root, index)
            return CommunityPackageAcquisition(source, catalog, True)
        except (CommunityPackageError, PackageCompileError, ValueError, OSError):
            pass

    with tempfile.TemporaryDirectory(dir=cache_root, prefix="download-") as temporary:
        downloaded = Path(temporary) / "package.whl"
        port.download(download_url, downloaded)
        temporary_source = CachedArchiveSource(downloaded, artifact_digest)
        catalog = compile_package_source(temporary_source)
        _validate_catalog_namespace(catalog, namespace)
        if catalog_validator is not None:
            catalog_validator(catalog)
        version = catalog.distribution.version
        _validate_version(version)
        target_dir = (
            cache_root / namespace.removeprefix(COMMUNITY_PREFIX) / version
        ).resolve()
        if not target_dir.is_relative_to(cache_root):
            raise CommunityPackageError("community package cache 路径逃逸")
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / (
            f"{namespace.removeprefix(COMMUNITY_PREFIX)}-{version}.whl"
        )
        downloaded.replace(target)

    source = CachedArchiveSource(target, artifact_digest)
    cache_item = _cache_item(cache_root, source, catalog)
    index.setdefault("packages", {})[namespace] = cache_item
    _record_cache_release(index, namespace, cache_item)
    _save_index(cache_root, index)
    return CommunityPackageAcquisition(source, catalog, False)


def load_cached_community_package(
    *,
    cache_key: str,
    working_dir: str | Path,
) -> CommunityPackageAcquisition:
    """按稳定缓存身份重新打开并校验一个已下载的 community 设备包。

    参数 ``cache_key`` 必须同时绑定 namespace、版本和 Artifact 摘要；参数
    ``working_dir`` 是当前 OS 的受管工作目录。返回值包含重新编译并校验过的
    PackageCatalog。缓存身份、索引或 wheel 任一不一致时抛出
    :class:`CommunityPackageError`，不得退回网络或猜测其他版本。
    """

    match = re.fullmatch(
        r"(community\.[a-z_][a-z0-9_]*)@([^#]+)#(sha256:[0-9a-f]{64})",
        cache_key,
    )
    if match is None:
        raise CommunityPackageError("community package cache_key 无效")
    namespace, version, artifact_digest = match.groups()
    _validate_namespace(namespace)
    _validate_version(version)
    cache_root = _prepare_cache_root(working_dir)
    index = _load_index(cache_root)
    item = index.get("releases", {}).get(cache_key)
    if not isinstance(item, dict):
        item = index.get("packages", {}).get(namespace)
    if not isinstance(item, dict):
        raise CommunityPackageError(f"受管缓存不存在设备包 {namespace}")
    if str(item.get("version") or "") != version:
        raise CommunityPackageError("cache_key 版本与受管缓存索引不一致")
    if str(item.get("artifact_digest") or "") != artifact_digest:
        raise CommunityPackageError("cache_key 摘要与受管缓存索引不一致")
    source = _cached_source_item(namespace, item, cache_root=cache_root)
    catalog = compile_package_source(source)
    _validate_catalog_namespace(catalog, namespace)
    if catalog.distribution.version != version:
        raise CommunityPackageError("cache_key 版本与 wheel Catalog 不一致")
    return CommunityPackageAcquisition(source, catalog, True)


def _prepare_cache_root(working_dir: str | Path) -> Path:
    """创建并校验受管缓存根目录，拒绝 symlink 与路径逃逸。"""

    working_root = Path(working_dir).resolve()
    cache_root = working_root / _CACHE_DIR
    if cache_root.is_symlink():
        raise CommunityPackageError("community package cache root 不得是 symlink")
    cache_root.mkdir(parents=True, exist_ok=True)
    if cache_root.resolve().parent != working_root:
        raise CommunityPackageError("community package cache root 路径逃逸")
    return cache_root.resolve()


def _validate_catalog_namespace(catalog: PackageCatalog, namespace: str) -> None:
    """确保受信 Catalog 的 namespace 与调用方选择完全一致。"""

    if catalog.namespace != namespace:
        raise CommunityPackageError(
            "community namespace 与 wheel Catalog 不一致: "
            f"{namespace} != {catalog.namespace}"
        )


def _catalogs_by_namespace(
    catalogs: Iterable[PackageCatalog],
) -> dict[str, PackageCatalog]:
    result: dict[str, PackageCatalog] = {}
    for catalog in catalogs:
        if catalog.namespace in result:
            raise CommunityPackageError(
                f"已加载多个相同 namespace 的 Catalog: {catalog.namespace}"
            )
        result[catalog.namespace] = catalog
    return result


def extract_community_classes(graph_data: dict[str, Any] | None) -> list[str]:
    if not graph_data:
        return []
    result = {
        value
        for node in graph_data.get("nodes", [])
        if isinstance(node, dict)
        and isinstance((value := node.get("class")), str)
        and value.startswith(COMMUNITY_PREFIX)
    }
    return sorted(result)


def _graph_package_cache_keys(
    graph_data: dict[str, Any] | None,
) -> dict[str, str]:
    """读取设备图中的精确包来源并拒绝同 namespace 多版本并存。"""

    result: dict[str, str] = {}
    for node in (graph_data or {}).get("nodes", []):
        if not isinstance(node, dict):
            continue
        class_name = node.get("class")
        if not isinstance(class_name, str) or not class_name.startswith(COMMUNITY_PREFIX):
            continue
        extra = node.get("extra")
        unilab = extra.get("unilab") if isinstance(extra, dict) else None
        if not isinstance(unilab, dict) or "package_cache_key" not in unilab:
            continue
        cache_key = unilab.get("package_cache_key")
        if not isinstance(cache_key, str) or not cache_key:
            raise CommunityPackageError("Graph package_cache_key 必须是非空字符串")
        namespace = _community_namespace(class_name)
        if not cache_key.startswith(f"{namespace}@"):
            raise CommunityPackageError(
                f"Graph class {class_name} 与 package_cache_key namespace 不一致"
            )
        previous = result.get(namespace)
        if previous is not None and previous != cache_key:
            raise CommunityPackageError(
                f"Graph 不支持同时加载 {namespace} 的多个设备包版本"
            )
        result[namespace] = cache_key
    return result


def _validate_graph_classes(
    catalog: PackageCatalog,
    classes: Iterable[str],
) -> None:
    """确认精确缓存 Catalog 覆盖设备图引用的全部设备或资源定义。"""

    definitions = {
        definition.fqid
        for collection in (
            catalog.definitions.devices,
            catalog.definitions.resources,
        )
        for definition in collection
    }
    for class_name in classes:
        if class_name not in definitions:
            raise CommunityPackageError(
                f"Graph class {class_name} 在固定 {catalog.namespace} Catalog 中不存在"
            )


def _cache_remote_item(
    namespace: str,
    item: dict[str, Any],
    *,
    cache_root: Path,
    port: CommunityResolvePort | None,
) -> CachedArchiveSource:
    package_info = item.get("package_info") or item
    digest = str(
        package_info.get("artifact_digest") or package_info.get("sha256") or ""
    )
    version = _item_version(item)
    _validate_namespace(namespace)
    _validate_version(version)
    url = str(package_info.get("download_url") or "")
    if not digest or not url or port is None:
        raise CommunityPackageError(
            f"community package {namespace} 缺少 wheel URL/digest 或下载 port"
        )
    cache_root = cache_root.resolve()
    target_dir = (
        cache_root / namespace.removeprefix(COMMUNITY_PREFIX) / version
    ).resolve()
    if not target_dir.is_relative_to(cache_root):
        raise CommunityPackageError("community package cache 路径逃逸")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{namespace.removeprefix(COMMUNITY_PREFIX)}-{version}.whl"
    if target.is_file():
        source = CachedArchiveSource(target, digest)
        try:
            compile_package_source(source)
            return source
        except (PackageCompileError, ValueError):
            target.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(dir=cache_root, prefix="download-") as temporary:
        downloaded = Path(temporary) / "package.whl"
        port.download(url, downloaded)
        source = CachedArchiveSource(downloaded, digest)
        compile_package_source(source)
        downloaded.replace(target)
    return CachedArchiveSource(target, digest)


def _cached_source(
    namespace: str,
    index: dict[str, Any],
    *,
    cache_root: Path,
) -> CachedArchiveSource:
    _validate_namespace(namespace)
    item = index.get("packages", {}).get(namespace)
    if not isinstance(item, dict):
        raise CommunityPackageError(
            f"无法加载 community package {namespace}：远端未返回且无缓存"
        )
    return _cached_source_item(namespace, item, cache_root=cache_root)


def _cached_source_item(
    namespace: str,
    item: dict[str, Any],
    *,
    cache_root: Path,
) -> CachedArchiveSource:
    """从已经选定的 current/release 索引条目安全解析 wheel 来源。"""

    _validate_namespace(namespace)
    version = str(item.get("version") or "")
    _validate_version(version)
    raw_wheel = Path(str(item.get("wheel") or ""))
    wheel = raw_wheel if raw_wheel.is_absolute() else cache_root / raw_wheel
    cache_root = cache_root.resolve()
    wheel = wheel.resolve()
    if not wheel.is_relative_to(cache_root):
        raise CommunityPackageError(
            f"community package {namespace} 的 cache index 路径逃逸"
        )
    source = CachedArchiveSource(wheel, str(item.get("artifact_digest") or ""))
    return source


def _cache_item(
    cache_root: Path,
    source: CachedArchiveSource,
    catalog: PackageCatalog,
) -> dict[str, Any]:
    """把已验证来源投影为 current 与 release 共用的稳定索引条目。"""

    return {
        "version": catalog.distribution.version,
        "artifact_digest": source.expected_digest,
        "wheel": source.wheel.resolve().relative_to(cache_root).as_posix(),
        "dependencies": list(catalog.distribution.dependencies),
    }


def _record_cache_release(
    index: dict[str, Any],
    namespace: str,
    item: dict[str, Any],
) -> None:
    """保留按 cache_key 寻址的历史发布，同时兼容现有 namespace current 索引。"""

    version = str(item.get("version") or "")
    artifact_digest = str(item.get("artifact_digest") or "")
    _validate_namespace(namespace)
    _validate_version(version)
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", artifact_digest):
        raise CommunityPackageError("community release artifact_digest 无效")
    releases = index.setdefault("releases", {})
    if not isinstance(releases, dict):
        releases = {}
        index["releases"] = releases
    releases[f"{namespace}@{version}#{artifact_digest}"] = dict(item)


def _community_namespace(class_name: str) -> str:
    parts = class_name.split(".")
    if (
        len(parts) != 3
        or parts[0] != "community"
        or not _NAMESPACE.fullmatch(".".join(parts[:2]))
        or not parts[2]
        or not parts[2].replace("_", "a").isalnum()
    ):
        raise CommunityPackageError(f"community class 无效: {class_name}")
    return ".".join(parts[:2])


def _item_namespace(item: dict[str, Any]) -> str:
    package_info = item.get("package_info") or {}
    return str(item.get("class_namespace") or package_info.get("class_namespace") or "")


def _item_version(item: dict[str, Any]) -> str:
    package_info = item.get("package_info") or item
    return str(package_info.get("version") or "unknown")


def _validate_namespace(namespace: str) -> None:
    if not _NAMESPACE.fullmatch(namespace):
        raise CommunityPackageError(f"community namespace 无效: {namespace}")


def _validate_version(version: str) -> None:
    if not _VERSION.fullmatch(version) or ".." in version:
        raise CommunityPackageError(f"community package version 无效: {version}")


def _load_index(cache_root: Path) -> dict[str, Any]:
    path = cache_root / _INDEX_NAME
    if not path.is_file():
        return {"schema_version": 1, "packages": {}}
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 1, "packages": {}}
    return result if isinstance(result, dict) else {"schema_version": 1, "packages": {}}


def _save_index(cache_root: Path, index: dict[str, Any]) -> None:
    path = cache_root / _INDEX_NAME
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


__all__ = [
    "CommunityDownloadPort",
    "CommunityPackageAcquisition",
    "CommunityPackageError",
    "CommunityPackageResolution",
    "CommunityResolvePort",
    "HttpClientCommunityAdapter",
    "RequestsCommunityDownloadAdapter",
    "acquire_community_package",
    "extract_community_classes",
    "load_cached_community_package",
    "resolve_graph_packages",
]
