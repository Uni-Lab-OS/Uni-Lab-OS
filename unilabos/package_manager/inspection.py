"""软件包检查（Package Inspect）与遗留注册表投影适配。"""

import hashlib
import json
import re
import tarfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from unilabos.registry.init_enforce import validate_init_param_enforce
from unilabos.utils import logger
from unilabos.utils.banner_print import print_status

from .catalog import PackageCatalog, PackageCompileError
from .compiler import compile_package_source
from .errors import PackageCLIError
from .project_metadata import parse_project_metadata, project_to_legacy_dict
from .sources import WorkspaceSource

COMMUNITY_PREFIX = "community."
DEFAULT_SOURCE_TYPE = "community"
ARCHIVE_EXCLUDE_DIRS = {
    "__pycache__",
    ".git",
    ".idea",
    ".vscode",
    "dist",
    "build",
    ".pytest_cache",
    "unilabos_data",
    ".venv",
    "venv",
    "node_modules",
}
ARCHIVE_EXCLUDE_SUFFIXES = {".pyc", ".pyo"}


def normalize_name(name: str) -> str:
    """归一化包名：小写，并把连续的非字母数字分隔符折叠为下划线。"""
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def resolve_class_namespace(project_name: str, namespace: str | None) -> str:
    """确定 class_namespace：显式 --namespace 优先，否则 community.<归一化包名>。"""
    if namespace:
        ns = namespace.strip()
        if not ns.startswith(COMMUNITY_PREFIX):
            ns = COMMUNITY_PREFIX + ns
        return ns
    return COMMUNITY_PREFIX + normalize_name(project_name)


def discover_registry_paths_from_project(project_root: Path | str) -> list[Path]:
    """从包根推导目录化注册表路径。

    ``[tool.unilabos.registry].paths`` 相对包含 ``pyproject.toml`` 的包根解析；
    未声明时回退到包根下的 ``unilabos_registry/``。
    """
    root = Path(project_root).resolve()
    pyproject_paths = _read_pyproject_registry_paths(root)
    if pyproject_paths:
        return pyproject_paths

    fallback = root / "unilabos_registry"
    if fallback.is_dir():
        return [fallback]
    return []


def _read_pyproject_registry_paths(project_root: Path) -> list[Path]:
    """通过统一项目解析器解析显式注册表目录。

    参数：``project_root`` 是包含 ``pyproject.toml`` 的软件包根。
    返回：实际存在的规范绝对注册表（Registry）目录列表。
    异常：元数据或文件不可读时记录警告并返回空列表。
    """

    pyproject = project_root / "pyproject.toml"
    if not pyproject.is_file():
        return []
    try:
        # ``project`` 与工作区（Workspace）启动、目录编译共用一套解析。
        project = parse_project_metadata(pyproject.read_bytes())
    except (OSError, TypeError, ValueError) as error:
        logger.warning(f"[package] 解析注册表路径失败: {error}")
        return []

    paths: list[Path] = []
    for raw_path in project.registry_paths:
        registry_path = (project_root / raw_path).resolve()
        if registry_path.is_dir():
            paths.append(registry_path)
    return paths


def read_pyproject(pkg_dir: Path) -> dict[str, Any]:
    """通过统一项目解析器读取遗留上传投影。

    参数：``pkg_dir`` 是包含 ``pyproject.toml`` 的软件包根目录。
    返回：遗留 ``package_info`` 构造器需要的项目字段字典。
    异常：文件缺失或项目元数据无效时抛出 ``PackageCLIError``。
    """

    pyproject_path = pkg_dir / "pyproject.toml"
    if not pyproject_path.is_file():
        raise PackageCLIError(f"未找到 pyproject.toml：{pyproject_path}")
    try:
        # ``project`` 是工作区（Workspace）、注册表（Registry）和包工具共享的项目事实。
        project = parse_project_metadata(pyproject_path.read_bytes())
    except (OSError, TypeError, ValueError) as error:
        raise PackageCLIError("pyproject.toml [project] 元数据无效") from error
    return project_to_legacy_dict(project)


def scan_package_devices(pkg_dir: Path) -> dict[str, dict[str, Any]]:
    """纯 AST 扫描包目录下的 @device 注册表，返回 {device_id: meta}。"""
    from unilabos.registry.ast_registry_scanner import scan_directory

    py_files = [
        f
        for f in pkg_dir.rglob("*.py")
        if not f.name.startswith("__")
        and not (set(f.relative_to(pkg_dir).parts) & ARCHIVE_EXCLUDE_DIRS)
    ]
    executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="PackageInspect")
    try:
        result = scan_directory(pkg_dir, executor=executor, include_files=py_files)
    finally:
        executor.shutdown(wait=True)
    devices = result.get("devices", {})
    return {did: meta for did, meta in devices.items() if isinstance(meta, dict)}


def read_registry_yaml_devices(pkg_dir: Path) -> dict[str, dict[str, Any]]:
    """读取包目录下 registry.yaml/*.yaml 里的设备注册表条目，返回 {device_id: entry}。

    community_drivers 标准布局（driver.py + registry.yaml + startup.json）使用 YAML 注册表，
    其条目天然含 class.action_value_mappings/schema，是最完整的 source_registry。
    仅采纳 resource_type=device 或带 class.action_value_mappings 的条目。
    """
    try:
        import yaml
    except ModuleNotFoundError:
        logger.warning("[package] 未安装 pyyaml，跳过 registry.yaml 读取")
        return {}

    entries: dict[str, dict[str, Any]] = {}
    for yaml_path in sorted(list(pkg_dir.glob("*.yaml")) + list(pkg_dir.glob("*.yml"))):
        try:
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            logger.warning(f"[package] 解析 {yaml_path} 失败: {exc}")
            continue
        if not isinstance(data, dict):
            continue
        for device_id, entry in data.items():
            if not isinstance(entry, dict):
                continue
            cls = entry.get("class") if isinstance(entry.get("class"), dict) else {}
            is_device = entry.get("resource_type") == "device" or bool(
                cls.get("action_value_mappings")
            )
            if is_device:
                entries[str(device_id)] = entry
    return entries


def read_external_registry_devices(pkg_dir: Path) -> dict[str, dict[str, Any]]:
    """读取包内"文件夹式"外部注册表的设备条目，返回 {device_id: entry}。

    遵循 Plan 09 外部包注册表约定（与运行时 Registry.load_device_types 同构）：
    - 注册表根来自 pyproject ``[tool.unilabos.registry] paths``，否则回退 ``unilabos_registry/``；
    - 每个根下的 ``devices/*.yaml`` 即设备文件；
    - 逐文件用 ``resolve_yaml_refs`` 展开跨文件 ``$ref``（共享 contracts），与运行时一致。

    与根目录 ``registry.yaml`` 互补：不要求把条目摊平到包根，目录化注册表即可被纳管。
    """
    try:
        import yaml
    except ModuleNotFoundError:
        logger.warning("[package] 未安装 pyyaml，跳过外部注册表读取")
        return {}

    from unilabos.registry.yaml_ref import resolve_yaml_refs

    registry_roots = discover_registry_paths_from_project(pkg_dir)
    if not registry_roots:
        return {}

    entries: dict[str, dict[str, Any]] = {}
    for root in registry_roots:
        devices_dir = root / "devices"
        if not devices_dir.is_dir():
            continue
        for yaml_path in sorted(
            list(devices_dir.glob("*.yaml")) + list(devices_dir.glob("*.yml"))
        ):
            try:
                raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
                data = resolve_yaml_refs(raw, base_file=yaml_path)
            except (
                IndexError,
                KeyError,
                OSError,
                TypeError,
                UnicodeError,
                ValueError,
                yaml.YAMLError,
            ) as exc:
                logger.warning(f"[package] 解析外部注册表 {yaml_path} 失败: {exc}")
                continue
            if not isinstance(data, dict):
                continue
            for device_id, entry in data.items():
                if not isinstance(entry, dict):
                    continue
                cls = entry.get("class") if isinstance(entry.get("class"), dict) else {}
                # devices/ 目录下条目天然是设备；接受带 class.module 或显式 resource_type=device 的条目
                is_device = (
                    bool(cls.get("module")) or entry.get("resource_type") == "device"
                )
                if is_device:
                    entries[str(device_id)] = entry
    return entries


def build_archive(pkg_dir: Path, archive_path: Path) -> str:
    """把包目录打包为 tar.gz，跳过缓存/版本控制目录，返回 "sha256:<hex>"。"""
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    arc_root = pkg_dir.name

    def _filter(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
        """过滤不应进入发布归档的本地产物。

        参数：``tarinfo`` 是归档库当前候选成员。
        返回：可保留成员原值；缓存、工作目录或字节码返回 ``None``。
        异常：无。
        """

        parts = set(Path(tarinfo.name).parts)
        if parts & ARCHIVE_EXCLUDE_DIRS:
            return None
        if Path(tarinfo.name).suffix in ARCHIVE_EXCLUDE_SUFFIXES:
            return None
        return tarinfo

    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(str(pkg_dir), arcname=arc_root, filter=_filter)

    return "sha256:" + _sha256_file(archive_path)


_PY_TO_JSON_SCHEMA_TYPE = {
    "float": "number",
    "int": "integer",
    "str": "string",
    "bool": "boolean",
    "dict": "object",
    "list": "array",
    "Dict": "object",
    "List": "array",
    "Any": "string",
}


def _json_schema_type(py_type: str) -> str:
    """把 Python 类型注解字符串归一化为 JSON Schema type（取裸类型名，未知回退 string）。"""
    base = (py_type or "").strip().split("[")[0].split(".")[-1]
    return _PY_TO_JSON_SCHEMA_TYPE.get(base, "string")


def build_action_value_mappings(actions: dict[str, Any]) -> dict[str, Any]:
    """把 AST 扫描的原始 action（params/return_type）转换成前后端期望的"""
    result: dict[str, Any] = {}
    for name, meta in actions.items():
        if not isinstance(meta, dict):
            continue
        params = meta.get("params") if isinstance(meta.get("params"), list) else []
        goal_props: dict[str, Any] = {}
        required: list[str] = []
        goal_default: dict[str, Any] = {}
        for param in params:
            if not isinstance(param, dict):
                continue
            pname = str(param.get("name") or "").strip()
            if not pname:
                continue
            goal_props[pname] = {
                "type": _json_schema_type(str(param.get("type", ""))),
                "title": pname,
            }
            if param.get("required"):
                required.append(pname)
            if param.get("default") is not None:
                goal_default[pname] = param.get("default")
        goal_schema: dict[str, Any] = {"type": "object", "properties": goal_props}
        if required:
            goal_schema["required"] = required
        action_args = (
            meta.get("action_args") if isinstance(meta.get("action_args"), dict) else {}
        )
        action_type_raw = action_args.get("action_type")
        action_type = "UniLabJsonCommand"
        if isinstance(action_type_raw, str) and action_type_raw.strip():
            action_type = action_type_raw.strip().split(":")[-1].split(".")[-1]
        entry: dict[str, Any] = {
            "type": action_type,
            "goal": goal_schema,
            "result": {"type": "object", "properties": {}},
            "feedback": {"type": "object", "properties": {}},
            "description": str(
                meta.get("docstring") or action_args.get("description") or ""
            ),
        }
        if goal_default:
            entry["goal_default"] = goal_default
        result[name] = entry
    return result


def build_resources(
    devices: dict[str, dict[str, Any]], package_info: dict[str, Any]
) -> list[dict[str, Any]]:
    """把扫描出的设备 meta 映射为 /lab/resource 的 resources 项，并附 resource 级 source_registry。"""
    resources: list[dict[str, Any]] = []
    for device_id, meta in devices.items():
        actions = meta.get("actions") if isinstance(meta.get("actions"), dict) else {}
        action_value_mappings = build_action_value_mappings(actions)
        status_props = (
            meta.get("status_properties")
            if isinstance(meta.get("status_properties"), dict)
            else {}
        )
        handles = meta.get("handles") if isinstance(meta.get("handles"), list) else []

        reg_class = {
            "module": meta.get("module", ""),
            "type": meta.get("device_type", "python"),
            "action_value_mappings": action_value_mappings,
            "status_types": status_props,
        }
        # source_registry：保存设备原始注册表，供后端 BuildEffectiveTemplate 读取 class.action_value_mappings
        source_registry = {
            "class": reg_class,
            "handles": handles,
            "device_id": device_id,
            "version": meta.get("version", package_info.get("version", "")),
            "description": meta.get("description", ""),
            "displayname": meta.get("displayname") or device_id,
            "icon": meta.get("icon", ""),
        }
        category = (
            meta.get("category") if isinstance(meta.get("category"), list) else []
        )
        resources.append(
            {
                "id": device_id,
                "registry_type": "device",
                "version": meta.get("version", package_info.get("version", "0.0.1")),
                "description": meta.get("description", ""),
                "displayname": meta.get("displayname") or device_id,
                "icon": meta.get("icon", ""),
                "class": reg_class,
                "category": category,
                "handles": _map_handles(handles),
                "package_info": package_info,
                "source_registry": source_registry,
            }
        )
    return resources


def build_resources_from_registry(
    entries: dict[str, dict[str, Any]],
    package_info: dict[str, Any],
) -> list[dict[str, Any]]:
    """把 registry.yaml 设备条目映射为 /lab/resource 的 resources 项。

    条目本身已含 class.action_value_mappings/schema，直接作为 source_registry，
    后端 BuildEffectiveTemplate 可据此构造 effective_template。
    """
    resources: list[dict[str, Any]] = []
    for device_id, entry in entries.items():
        cls = entry.get("class") if isinstance(entry.get("class"), dict) else {}
        init_schema = (
            entry.get("init_param_schema")
            if isinstance(entry.get("init_param_schema"), dict)
            else None
        )
        init_enforce = validate_init_param_enforce(
            device_id,
            init_schema,
            entry.get("init_param_enforce"),
            error_factory=PackageCLIError,
        )
        category = entry.get("category") or entry.get("tags") or []
        if isinstance(category, str):
            category = [category]
        resource: dict[str, Any] = {
            "id": device_id,
            "registry_type": str(
                entry.get("registry_type", entry.get("resource_type", "device"))
            ),
            "version": str(entry.get("version", package_info.get("version", "0.0.1"))),
            "description": entry.get("description", ""),
            "icon": entry.get("icon", ""),
            "class": {
                "module": cls.get("module", ""),
                "type": cls.get("type", "python"),
                "action_value_mappings": cls.get("action_value_mappings", {}),
                "status_types": cls.get("status_types", {}),
            },
            "handles": [],
            "category": category if isinstance(category, list) else [],
            "manufacturer": str(entry.get("manufacturer", "")),
            "model": entry.get("model"),
            "scene": entry.get("scene"),
            "device_params": entry.get("device_params"),
            "package_info": package_info,
            # source_registry：直接保存 YAML 原始条目（含 class.action_value_mappings）
            "source_registry": entry,
        }
        if init_schema is not None:
            resource["init_param_schema"] = init_schema
        resource["init_param_enforce"] = init_enforce
        resources.append(resource)
    return resources


def build_package_info(
    project: dict[str, Any],
    class_namespace: str,
    sha256: str,
    download_url: str = "",
    oss_object_key: str = "",
) -> dict[str, Any]:
    """根据 pyproject 元信息 + 命名空间 + 归档指纹构造 package_info（后端/Edge 共同消费的字段）。"""
    name = project["name"]
    info: dict[str, Any] = {
        "name": name,
        "version": project["version"],
        "class_namespace": class_namespace,
        "module_prefix": class_namespace.split(".")[0]
        if class_namespace
        else "community",
        "normalized_name": normalize_name(name),
        "source_type": DEFAULT_SOURCE_TYPE,
        "install_spec": f"{name}=={project['version']}"
        if project.get("version")
        else name,
        "summary": project.get("summary", ""),
        "license": project.get("license", ""),
        "homepage": project.get("homepage", ""),
        # pyproject [project].dependencies：Edge 消费侧据此安装运行依赖（不安装包体本身）
        "dependencies": list(project.get("dependencies") or []),
        "sha256": sha256,
        "download_url": download_url,
    }
    if oss_object_key:
        info["oss_object_key"] = oss_object_key
    return info


def inspect_package(
    path: str,
    namespace: str | None = None,
    out_dir: str | None = None,
) -> dict[str, Any]:
    """编译并打包一个本地软件包。

    参数：``path`` 是软件包根；``namespace`` 是仅供遗留包使用的
    可选社区命名空间；``out_dir`` 是可选产物目录。
    返回：包含软件包目录（PackageCatalog）摘要、兼容上传投影和归档路径的字典。
    异常：路径、项目、静态定义或显式命名空间与规范目录冲突时
    抛出 ``PackageCLIError``；不发布部分编译结果。
    """

    pkg_dir = Path(path).resolve()
    if not pkg_dir.is_dir():
        raise PackageCLIError(f"包目录不存在：{pkg_dir}")

    project = read_pyproject(pkg_dir)
    # ``canonical_package`` 用来区分规范工作区与仅含 YAML 的遗留软件包。
    canonical_package = pkg_dir / normalize_name(project["name"])
    catalog: PackageCatalog | None = None
    if canonical_package.joinpath("__init__.py").is_file():
        try:
            # ``catalog`` 是这次检查唯一的规范静态编译结果。
            catalog = compile_package_source(WorkspaceSource(pkg_dir))
        except PackageCompileError as error:
            diagnostic_codes = ", ".join(item.code for item in error.diagnostics)
            raise PackageCLIError(f"软件包目录编译失败：{diagnostic_codes}") from error
        except (TypeError, ValueError) as error:
            raise PackageCLIError("软件包目录编译失败") from error

    class_namespace = (
        catalog.namespace
        if catalog is not None
        else resolve_class_namespace(project["name"], namespace)
    )
    if catalog is not None and namespace is not None:
        requested_namespace = resolve_class_namespace(project["name"], namespace)
        if requested_namespace != catalog.namespace:
            raise PackageCLIError(
                "规范工作区命名空间由项目身份决定，不能用 --namespace 覆盖"
            )

    out_path = Path(out_dir).resolve() if out_dir else (pkg_dir.parent / "dist")
    out_path.mkdir(parents=True, exist_ok=True)
    archive_name = f"{normalize_name(project['name'])}-{project['version']}.tar.gz"
    archive_path = out_path / archive_name
    sha256 = build_archive(pkg_dir, archive_path)

    package_info = build_package_info(project, class_namespace, sha256)

    if catalog is not None:
        # ``catalog_document`` 是兼容投影与落盘共用的解冻规范目录。
        catalog_document = catalog.to_dict()
        # ``registry_entries`` 仅是遗留上传 DTO，不成为第二个定义权威。
        registry_entries = {
            item["id"]: item["details"]["registry_entry"]
            for definition_kind in ("devices", "resources")
            for item in catalog_document["definitions"][definition_kind]
        }
        device_source = "PackageCatalog"
        device_ids = [item["id"] for item in catalog_document["definitions"]["devices"]]
        resources = build_resources_from_registry(registry_entries, package_info)
        catalog_path = out_path / "package.catalog.json"
        catalog_path.write_bytes(catalog.to_canonical_bytes())
    else:
        # 设备来源优先级：根目录 YAML > 目录式外部注册表 > 遗留 AST。
        yaml_entries = read_registry_yaml_devices(pkg_dir)
        if not yaml_entries:
            yaml_entries = read_external_registry_devices(pkg_dir)
            registry_source = "unilabos_registry/"
        else:
            registry_source = "registry.yaml"
        if yaml_entries:
            device_source = registry_source
            device_ids = sorted(yaml_entries)
            resources = build_resources_from_registry(yaml_entries, package_info)
        else:
            device_source = "@device AST"
            ast_devices = scan_package_devices(pkg_dir)
            device_ids = sorted(ast_devices)
            resources = build_resources(ast_devices, package_info)
        catalog_path = None
    devices = {rid: None for rid in device_ids}
    if not resources:
        print_status(
            f"警告：{pkg_dir} 未发现 registry.yaml / unilabos_registry/ 或 @device 设备，仅生成 package_info",
            "warning",
        )

    package_info_path = out_path / "package_info.json"
    resources_path = out_path / "resources.json"
    package_info_path.write_text(
        json.dumps(package_info, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    resources_path.write_text(
        json.dumps(resources, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print_status(
        f"package inspect 完成：{project['name']}@{project['version']}", "info"
    )
    print_status(f"  class_namespace : {class_namespace}", "info")
    print_status(f"  设备来源        : {device_source}", "info")
    print_status(
        f"  设备数          : {len(resources)} ({', '.join(device_ids) or '无'})",
        "info",
    )
    print_status(f"  归档            : {archive_path} ({sha256})", "info")
    print_status(f"  package_info    : {package_info_path}", "info")
    print_status(f"  resources       : {resources_path}", "info")

    return {
        "project": project,
        "class_namespace": class_namespace,
        "devices": devices,
        "archive_path": str(archive_path),
        "sha256": sha256,
        "package_info": package_info,
        "resources": resources,
        "package_info_path": str(package_info_path),
        "resources_path": str(resources_path),
        "catalog_digest": catalog.catalog_digest if catalog is not None else None,
        "catalog_path": str(catalog_path) if catalog_path is not None else None,
    }


def _map_handles(handles: list[Any]) -> list[dict[str, Any]]:
    """把扫描出的 handles 列表映射为后端 RegHandle 友好结构（缺字段留空，不阻断上传）。"""
    mapped: list[dict[str, Any]] = []
    for handle in handles:
        if isinstance(handle, dict):
            mapped.append(
                {
                    "data_key": str(handle.get("data_key", "")),
                    "data_source": str(handle.get("data_source", "")),
                    "data_type": str(handle.get("data_type", "")),
                    "description": str(handle.get("description", "")),
                    "handler_key": str(handle.get("handler_key", "")),
                    "io_type": str(handle.get("io_type", "")),
                    "label": str(handle.get("label", "")),
                    "side": str(handle.get("side", "")),
                }
            )
    return mapped


def _sha256_file(path: Path) -> str:
    """分块计算归档文件的 SHA-256 摘要。

    参数：``path`` 是已完成写入的归档路径。
    返回：不带前缀的小写十六进制摘要。
    异常：文件不可读时传播原始 IO 异常。
    """

    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
