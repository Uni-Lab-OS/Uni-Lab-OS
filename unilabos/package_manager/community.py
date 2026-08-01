"""Resolve Graph community references into explicit cached-wheel Sources."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from collections.abc import Iterable
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


class CommunityResolvePort(Protocol):
    def resolve(
        self,
        classes: list[str],
        current_packages: list[dict[str, str]],
    ) -> list[dict[str, Any]]: ...

    def download(self, url: str, destination: Path) -> None: ...


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


@dataclass(frozen=True)
class CommunityPackageResolution:
    sources: tuple[CachedArchiveSource, ...] = ()
    catalogs: tuple[PackageCatalog, ...] = ()
    classes: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()


def resolve_graph_packages(
    graph_data: dict[str, Any] | None,
    *,
    working_dir: str | Path,
    port: CommunityResolvePort | None = None,
    available_catalogs: Iterable[PackageCatalog] = (),
) -> CommunityPackageResolution:
    classes = tuple(extract_community_classes(graph_data))
    if not classes:
        return CommunityPackageResolution()
    available = _catalogs_by_namespace(available_catalogs)
    unresolved_classes: list[str] = []
    for class_name in classes:
        namespace = _community_namespace(class_name)
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
        return CommunityPackageResolution(classes=classes)

    namespaces = sorted({_community_namespace(item) for item in unresolved_classes})
    cache_root = Path(working_dir).resolve() / _CACHE_DIR
    cache_root.mkdir(parents=True, exist_ok=True)
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

    sources: list[CachedArchiveSource] = []
    catalogs: list[PackageCatalog] = []
    dependencies: list[str] = []
    for namespace in namespaces:
        item = by_namespace.get(namespace)
        if item is not None:
            source, package_dependencies = _cache_remote_item(
                namespace,
                item,
                cache_root=cache_root,
                port=port,
            )
            index.setdefault("packages", {})[namespace] = {
                "version": _item_version(item),
                "artifact_digest": source.expected_digest,
                "wheel": source.wheel.resolve().relative_to(cache_root).as_posix(),
                "dependencies": package_dependencies,
            }
        else:
            source, package_dependencies = _cached_source(
                namespace, index, cache_root=cache_root
            )
        catalog = compile_package_source(source)
        if catalog.namespace != namespace:
            raise CommunityPackageError(
                f"community namespace 与 wheel Catalog 不一致: "
                f"{namespace} != {catalog.namespace}"
            )
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


def _cache_remote_item(
    namespace: str,
    item: dict[str, Any],
    *,
    cache_root: Path,
    port: CommunityResolvePort | None,
) -> tuple[CachedArchiveSource, list[str]]:
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
            return source, list(package_info.get("dependencies") or [])
        except (PackageCompileError, ValueError):
            target.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(dir=cache_root, prefix="download-") as temporary:
        downloaded = Path(temporary) / "package.whl"
        port.download(url, downloaded)
        source = CachedArchiveSource(downloaded, digest)
        compile_package_source(source)
        shutil.copy2(downloaded, target)
    return (
        CachedArchiveSource(target, digest),
        list(package_info.get("dependencies") or []),
    )


def _cached_source(
    namespace: str,
    index: dict[str, Any],
    *,
    cache_root: Path,
) -> tuple[CachedArchiveSource, list[str]]:
    _validate_namespace(namespace)
    item = index.get("packages", {}).get(namespace)
    if not isinstance(item, dict):
        raise CommunityPackageError(
            f"无法加载 community package {namespace}：远端未返回且无缓存"
        )
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
    return source, list(item.get("dependencies") or [])


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
    "CommunityPackageError",
    "CommunityPackageResolution",
    "CommunityResolvePort",
    "HttpClientCommunityAdapter",
    "extract_community_classes",
    "resolve_graph_packages",
]
