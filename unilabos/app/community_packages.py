"""物理图遗留社区包引用的解析与可信缓存兼容层。"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from unilabos.app.community_package_acquisition import (
    acquire_community_workspace,
    safe_package_info,
)
from unilabos.utils import logger
from unilabos.utils.banner_print import print_status

COMMUNITY_PREFIX = "community."
COMMUNITY_CACHE_DIR = "community_devices"
MANIFEST_FILENAME = "manifest.json"


class CommunityPackageError(RuntimeError):
    """物理图引用的社区包无法安全加载时抛出的准备错误。"""


@dataclass
class CommunityPackagePrepareResult:
    # ``devices_dirs`` 是本轮新增且可交给注册表（Registry）扫描的远端缓存目录。
    devices_dirs: List[str] = field(default_factory=list)
    # ``aliases`` 把物理图社区类名映射到注册表内的实际类身份。
    aliases: Dict[str, str] = field(default_factory=dict)
    # ``classes`` 是物理图中去重并稳定排序后的社区类名全集。
    classes: List[str] = field(default_factory=list)
    # ``dependencies`` 是远端社区包声明且去重后的 Python 运行依赖。
    dependencies: List[str] = field(default_factory=list)
    # ``namespaces`` 把规范包目录映射到社区命名空间，并以本地工作区映射为权威。
    namespaces: Dict[str, str] = field(default_factory=dict)


def extract_community_classes(graph_data: Optional[Dict[str, Any]]) -> List[str]:
    if not graph_data:
        return []

    result: List[str] = []
    for node in graph_data.get("nodes", []):
        if not isinstance(node, dict):
            continue
        class_name = node.get("class")
        if isinstance(class_name, str) and class_name.startswith(COMMUNITY_PREFIX):
            result.append(class_name)
    return sorted(set(result))


def community_namespace(class_name: str) -> str:
    parts = class_name.split(".")
    if len(parts) < 2 or parts[0] != "community":
        raise ValueError(f"Invalid community class: {class_name}")
    return ".".join(parts[:2])


def infer_alias_target(class_name: str) -> str:
    namespace = community_namespace(class_name)
    prefix = namespace + "."
    if class_name.startswith(prefix) and len(class_name) > len(prefix):
        return class_name[len(prefix):]
    return class_name.rsplit(".", 1)[-1]


def load_manifest(working_dir: str | Path) -> Dict[str, Any]:
    manifest_path = _manifest_path(working_dir)
    if not manifest_path.is_file():
        return {"packages": {}}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("packages", {})
            return data
    except Exception as exc:
        logger.warning(f"[CommunityPackage] manifest 读取失败: {exc}")
    return {"packages": {}}


def save_manifest(working_dir: str | Path, manifest: Dict[str, Any]) -> None:
    manifest_path = _manifest_path(working_dir)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = manifest_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(manifest_path)


def prepare_community_packages(
    graph_data: Optional[Dict[str, Any]],
    working_dir: str | Path,
    http_client: Any = None,
    available_namespaces: Optional[Dict[str, str]] = None,
) -> CommunityPackagePrepareResult:
    """准备物理图尚未由本地工作区提供的社区设备包。

    参数：``graph_data`` 是物理图 JSON；``working_dir`` 是社区包缓存目录；
    ``http_client`` 是可选远端解析端口；``available_namespaces`` 把已经由显式
    工作区提供的设备扫描目录映射到 ``community.<package>`` 命名空间。
    返回：同时保留本地命名空间和新解析远端包的准备结果。
    异常：图引用的任一社区命名空间既未本地提供也不能从缓存/远端解析时抛出
    ``CommunityPackageError``，禁止无定义继续启动。
    """

    # ``provided_namespaces`` 是显式工作区已提供的目录到命名空间映射。
    provided_namespaces = dict(available_namespaces or {})
    # ``provided_namespace_values`` 是不得由缓存或远端响应覆盖的本地命名空间集合。
    provided_namespace_values = set(provided_namespaces.values())
    classes = extract_community_classes(graph_data)
    if not classes:
        return CommunityPackagePrepareResult(namespaces=provided_namespaces)

    print_status(f"发现 community 设备引用: {', '.join(classes)}", "info")
    manifest = load_manifest(working_dir)
    packages = manifest.setdefault("packages", {})
    logger.trace(
        f"[CommunityPackage] 准备开始: classes={classes} working_dir={working_dir} "
        f"manifest 已缓存包={list(packages.keys())}"
    )
    # ``remote_classes`` 只包含尚未由本地工作区提供的社区类名。
    remote_classes = [
        class_name
        for class_name in classes
        if community_namespace(class_name) not in provided_namespace_values
    ]
    # ``remote_items`` 只能回答确实缺失的命名空间，不能参与本地包发现。
    remote_items = (
        _resolve_remote_packages(remote_classes, manifest, http_client)
        if remote_classes
        else []
    )

    devices_dirs: List[str] = []
    aliases: Dict[str, str] = {}
    dependencies: List[str] = []
    namespaces: Dict[str, str] = provided_namespaces
    # ``missing_namespaces`` 是仍需缓存或远端社区包满足的命名空间。
    missing_namespaces = {
        community_namespace(class_name) for class_name in classes
    } - provided_namespace_values

    for item in remote_items:
        # ``namespace`` 是远端项目声称提供的社区包身份，必须先检查权威冲突。
        namespace = item.get("class_namespace") or (item.get("package_info") or {}).get(
            "class_namespace"
        )
        if namespace in provided_namespace_values:
            raise CommunityPackageError(
                f"远端社区包不得覆盖本地工作区命名空间: {namespace}"
            )
        package_dir = _ensure_remote_item_cached(
            item,
            working_dir,
            manifest,
            http_client=http_client,
        )
        if package_dir:
            devices_dirs.append(str(package_dir))

        if namespace:
            missing_namespaces.discard(namespace)
            if package_dir:
                namespaces[str(Path(package_dir).resolve())] = namespace
            # 依赖直接取自 resolve 响应（命中与否都携带），避免旧 manifest 缺字段导致丢依赖
            dependencies.extend(
                (item.get("package_info") or {}).get("dependencies") or []
            )
        aliases.update(_normalize_aliases(item, classes))

    for namespace in list(missing_namespaces):
        cached = packages.get(namespace)
        if not cached:
            continue
        package_dir = Path(cached.get("package_dir", ""))
        if package_dir.is_dir():
            devices_dirs.append(str(package_dir))
            namespaces[str(package_dir.resolve())] = namespace
            missing_namespaces.discard(namespace)
            cached_aliases = cached.get("aliases") or {}
            aliases.update({str(k): str(v) for k, v in cached_aliases.items()})
            dependencies.extend(cached.get("dependencies") or [])
            logger.trace(
                f"[CommunityPackage] 离线缓存命中(resolve 未覆盖): {namespace}@{cached.get('version')} "
                f"dir={package_dir} dependencies={cached.get('dependencies') or []}"
            )

    for class_name in classes:
        aliases.setdefault(class_name, infer_alias_target(class_name))

    if missing_namespaces:
        raise CommunityPackageError(
            "无法加载 community 设备包: "
            + ", ".join(sorted(missing_namespaces))
            + "。请检查网络、后端 resolve 接口或本地缓存。"
        )

    devices_dirs = _dedupe_existing_dirs(devices_dirs)
    if devices_dirs:
        print_status(f"community 设备包挂载目录: {', '.join(devices_dirs)}", "info")

    save_manifest(working_dir, manifest)
    result = CommunityPackagePrepareResult(
        devices_dirs=devices_dirs,
        aliases=aliases,
        classes=classes,
        dependencies=_dedupe_preserve_order(dependencies),
        namespaces=namespaces,
    )
    logger.trace(
        "[CommunityPackage] 准备完成: "
        f"devices_dirs={result.devices_dirs} namespaces={result.namespaces} "
        f"dependencies={result.dependencies}"
    )
    return result


def _resolve_remote_packages(classes: List[str], manifest: Dict[str, Any], http_client: Any) -> List[Dict[str, Any]]:
    if http_client is None:
        logger.trace("[CommunityPackage] 未提供 http_client，跳过远端 resolve，仅用本地缓存")
        return []
    try:
        current_packages = []
        for namespace, info in (manifest.get("packages") or {}).items():
            current_packages.append(
                {
                    "class_namespace": namespace,
                    "version": info.get("version"),
                    "sha256": info.get("sha256"),
                }
            )

        local_cache_fingerprint = [f"{p['class_namespace']}@{p['version']}" for p in current_packages]
        logger.trace(
            f"[CommunityPackage] resolve 请求: classes={classes} local_cache={local_cache_fingerprint}"
        )
        response = http_client.resolve_community_packages(classes, current_packages=current_packages)
        data = response.get("data", response) if isinstance(response, dict) else []
        if isinstance(data, list):
            items = [item for item in data if isinstance(item, dict)]
            for item in items:
                pkg = item.get("package_info") or {}
                logger.trace(
                    "[CommunityPackage] resolve 结果: "
                    f"namespace={item.get('class_namespace') or pkg.get('class_namespace')} "
                    f"status={item.get('status')} name={pkg.get('name')} version={pkg.get('version')} "
                    f"sha256={pkg.get('sha256')} install_spec={pkg.get('install_spec')} "
                    f"dependencies={pkg.get('dependencies')} aliases={item.get('aliases')} "
                    f"download_url={pkg.get('download_url')}"
                )
            logger.trace(f"[CommunityPackage] resolve 返回 {len(items)} 个包")
            return items
    except Exception as exc:
        logger.warning(f"[CommunityPackage] 远端 resolve 失败，将尝试本地缓存: {exc}")
    return []


def _ensure_remote_item_cached(
    item: Dict[str, Any],
    working_dir: str | Path,
    manifest: Dict[str, Any],
    http_client: Any = None,
) -> Optional[Path]:
    """把一个远端解析结果收敛到统一 acquisition/cache 与派生工作区。

    参数：``item`` 是遗留 resolve 投影；``working_dir`` 是受管运行目录；
    ``manifest`` 是迁移期社区缓存索引；``http_client`` 只提供同环境 Backend 根。
    返回：已验证派生工作区目录；项目没有命名空间时返回 ``None``。
    异常：远端身份、可信获取或工作区导出失败时抛出 ``CommunityPackageError``；
    不再接受 resolve 响应中的任意 ``download_url`` 直接解压。
    """

    package_info = item.get("package_info") or item
    namespace = item.get("class_namespace") or package_info.get("class_namespace")
    if not namespace:
        return None

    packages = manifest.setdefault("packages", {})
    cached = packages.get(namespace) or {}
    version = str(package_info.get("version") or cached.get("version") or "unknown")
    remote_digest = str(
        package_info.get("artifact_digest") or package_info.get("sha256") or ""
    )
    cached_digest = str(
        cached.get("artifact_digest") or cached.get("sha256") or ""
    )
    cached_dir = Path(cached.get("package_dir", ""))
    if (
        cached.get("acquisition") == "package-cache/v1"
        and cached_dir.is_dir()
        and cached.get("version") == version
        and remote_digest
        and cached_digest == remote_digest
    ):
        logger.trace(
            f"[CommunityPackage] 缓存命中(版本/指纹一致): {namespace}@{version} "
            f"artifact_digest={remote_digest} dir={cached_dir}"
        )
        return cached_dir

    logger.trace(
        f"[CommunityPackage] 缓存未命中/需更新: {namespace} "
        f"目标 version={version} artifact_digest={remote_digest}; "
        f"本地 version={cached.get('version')} artifact_digest={cached_digest} "
        f"dir_exists={cached_dir.is_dir()}"
    )

    try:
        package_dir, acquisition = acquire_community_workspace(
            package_info,
            working_dir=working_dir,
            namespace=namespace,
            version=version,
            http_client=http_client,
        )
    except Exception as error:
        if cached_dir.is_dir() and package_info.get("allow_cached_fallback"):
            logger.warning(
                f"[CommunityPackage] {namespace} 可信获取失败，使用显式允许的旧缓存"
            )
            return cached_dir
        if isinstance(error, CommunityPackageError):
            raise
        raise CommunityPackageError(
            f"community package {namespace}@{version} 可信获取失败: {error}"
        ) from error

    pyproject = _find_pyproject(package_dir)
    pyproject_meta = read_pyproject_metadata(pyproject)
    aliases = _normalize_aliases(item, [])
    # pyproject [project].dependencies 由 producer 写入 package_info；持久化以便离线缓存复用
    dependencies = _dedupe_preserve_order(package_info.get("dependencies") or [])
    logger.trace(
        f"[CommunityPackage] 已缓存: {namespace}@{version} dir={package_dir} "
        f"pyproject={pyproject_meta} dependencies={dependencies} aliases={aliases}"
    )

    packages[namespace] = {
        "class_namespace": namespace,
        "version": acquisition["version"],
        "sha256": acquisition["artifact_digest"],
        "artifact_digest": acquisition["artifact_digest"],
        "catalog_digest": acquisition["catalog_digest"],
        "content_digest": acquisition["content_digest"],
        "cache_key": acquisition["cache_key"],
        "acquisition": "package-cache/v1",
        "package_dir": str(package_dir),
        "pyproject": pyproject_meta,
        "aliases": aliases,
        "dependencies": dependencies,
    }
    (package_dir / "package_info.json").write_text(
        json.dumps(
            safe_package_info(package_info, acquisition),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return package_dir


def _normalize_aliases(item: Dict[str, Any], classes: Iterable[str]) -> Dict[str, str]:
    raw_aliases = item.get("aliases") or {}
    aliases = {str(k): str(v) for k, v in raw_aliases.items()} if isinstance(raw_aliases, dict) else {}

    namespace = item.get("class_namespace") or (item.get("package_info") or {}).get("class_namespace")
    if namespace:
        for class_name in classes:
            if class_name.startswith(namespace + "."):
                aliases.setdefault(class_name, infer_alias_target(class_name))
    return aliases


def read_pyproject_metadata(pyproject_path: Path) -> Dict[str, str]:
    text = pyproject_path.read_text(encoding="utf-8")
    result: Dict[str, str] = {}
    in_project = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_project = line == "[project]"
            continue
        if not in_project or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key in {"name", "version"}:
            result[key] = value
    return result


def _manifest_path(working_dir: str | Path) -> Path:
    return _cache_root(working_dir) / MANIFEST_FILENAME


def _cache_root(working_dir: str | Path) -> Path:
    root = Path(working_dir) / COMMUNITY_CACHE_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def _normalize_package_dir_name(namespace: str) -> str:
    return namespace.replace(COMMUNITY_PREFIX, "", 1).replace(".", "-").replace("_", "-")


def _dedupe_existing_dirs(paths: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for path in paths:
        resolved = str(Path(path).resolve())
        if resolved in seen or not Path(resolved).is_dir():
            continue
        seen.add(resolved)
        result.append(resolved)
    return result


def _dedupe_preserve_order(items: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for item in items:
        value = str(item).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _find_pyproject(root: Path) -> Path:
    candidates = sorted(root.rglob("pyproject.toml"))
    if not candidates:
        raise CommunityPackageError(f"community package 解压后未找到 pyproject.toml: {root}")
    return candidates[0]
