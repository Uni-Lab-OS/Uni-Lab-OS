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
    """把发行包名归一化为稳定的文件与身份片段。

    参数：``name`` 是项目声明的发行包名。
    返回：全小写、连续非字母数字字符折叠为单个下划线且首尾无下划线的字符串。
    异常：无；空白或只有分隔符的输入返回空字符串，由调用方决定是否拒绝。
    """

    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def resolve_class_namespace(project_name: str, namespace: str | None) -> str:
    """解析遗留上传投影使用的社区类命名空间。

    参数：``project_name`` 是发行包名；``namespace`` 是可选显式命名空间。
    返回：以 ``community.`` 开头的类命名空间；未显式提供时使用归一化发行包名。
    异常：无；本函数只处理遗留投影，规范包目录（PackageCatalog）的命名空间
    仍由统一编译器决定，调用方必须拒绝冲突覆盖。
    """

    if namespace:
        # ``ns`` 是遗留上传投影最终使用的规范社区类命名空间身份。
        ns = namespace.strip()
        if not ns.startswith(COMMUNITY_PREFIX):
            ns = COMMUNITY_PREFIX + ns
        return ns
    return COMMUNITY_PREFIX + normalize_name(project_name)


def discover_registry_paths_from_project(project_root: Path | str) -> list[Path]:
    """从包根推导目录化注册表路径。

    参数：``project_root`` 是包含项目元数据的软件包根。
    返回：按项目声明顺序排列且实际存在的注册表（Registry）绝对目录；未声明时
    仅在默认 ``unilabos_registry`` 存在时返回该目录，否则返回空列表。
    异常：路径解析错误直接传播；项目元数据读取或解析失败由内部解析器记录警告并
    关闭式返回空列表，不扫描其他目录。

    ``[tool.unilabos.registry].paths`` 相对包含 ``pyproject.toml`` 的包根解析；
    未声明时回退到包根下的 ``unilabos_registry/``。
    """
    # ``root`` 是当前软件包检查唯一允许解析注册表（Registry）路径的包根。
    root = Path(project_root).resolve()
    # ``pyproject_paths`` 是项目显式授权的注册表（Registry）目录集合。
    pyproject_paths = _read_pyproject_registry_paths(root)
    if pyproject_paths:
        return pyproject_paths

    # ``fallback`` 是未显式声明时唯一允许采用的包内注册表（Registry）目录。
    fallback = root / "unilabos_registry"
    if fallback.is_dir():
        return [fallback]
    return []


def _read_pyproject_registry_paths(project_root: Path) -> list[Path]:
    """通过统一项目解析器解析显式注册表目录。

    参数：``project_root`` 是包含 ``pyproject.toml`` 的软件包根。
    返回：项目显式声明且实际存在的规范绝对注册表（Registry）目录列表。
    异常：元数据或文件不可读时记录警告并返回空列表；不得回退扫描 ``sys.path``
    或环境中可导入的软件包。
    """

    # ``pyproject`` 是当前包身份与注册表（Registry）路径声明的唯一元数据文件。
    pyproject = project_root / "pyproject.toml"
    if not pyproject.is_file():
        return []
    try:
        # ``project`` 与工作区（Workspace）启动、目录编译共用一套解析。
        project = parse_project_metadata(pyproject.read_bytes())
    except (OSError, TypeError, ValueError) as error:
        logger.warning(f"[package] 解析注册表路径失败: {error}")
        return []

    # ``paths`` 聚合实际存在且由项目显式授权的注册表（Registry）绝对目录。
    paths: list[Path] = []
    for raw_path in project.registry_paths:
        # ``registry_path`` 是一个声明路径相对包根解析后的规范位置。
        registry_path = (project_root / raw_path).resolve()
        if registry_path.is_dir():
            paths.append(registry_path)
    return paths


def read_pyproject(pkg_dir: Path) -> dict[str, Any]:
    """通过统一项目解析器读取遗留上传投影。

    参数：``pkg_dir`` 是包含 ``pyproject.toml`` 的软件包根目录。
    返回：遗留 ``package_info`` 上传投影构造器需要的项目字段字典。
    异常：文件缺失或项目元数据无效时抛出 ``PackageCLIError``。
    """

    # ``pyproject_path`` 是遗留上传投影读取的项目身份文件。
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
    """通过 AST 扫描遗留包内声明的设备定义。

    参数：``pkg_dir`` 是已验证存在的软件包根。
    返回：以稳定设备定义身份为键、扫描元数据为值的字典；缓存、构建、虚拟环境
    和版本控制目录中的 Python 文件不会进入结果。
    异常：扫描器或文件系统异常直接传播；线程池无论成功失败都必须关闭。本函数只
    生成遗留注册表（Registry）投影，不导入作者模块。
    """

    from unilabos.registry.ast_registry_scanner import scan_directory

    # ``py_files`` 是当前包根内允许进入遗留 AST 注册表（Registry）扫描的文件集。
    py_files = [
        f
        for f in pkg_dir.rglob("*.py")
        if not f.name.startswith("__")
        and not (set(f.relative_to(pkg_dir).parts) & ARCHIVE_EXCLUDE_DIRS)
    ]
    # ``executor`` 只并行执行本次显式包根内的 AST 扫描，不承担常驻发现职责。
    executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="PackageInspect")
    try:
        # ``result`` 是扫描器对本次固定文件集合生成的完整遗留定义投影。
        result = scan_directory(pkg_dir, executor=executor, include_files=py_files)
    finally:
        executor.shutdown(wait=True)
    # ``devices`` 是按稳定设备定义身份索引的 AST 扫描候选集合。
    devices = result.get("devices", {})
    return {did: meta for did, meta in devices.items() if isinstance(meta, dict)}


def read_registry_yaml_devices(pkg_dir: Path) -> dict[str, dict[str, Any]]:
    """读取包根 YAML 文件中的遗留设备注册表条目。

    参数：``pkg_dir`` 是已验证存在的软件包根。
    返回：以设备定义身份为键、原始 YAML 注册表（Registry）条目为值的字典；只
    接受显式设备类型或带动作映射的条目。
    异常：缺少 PyYAML、单个文件不可读或 YAML 无效时记录警告并跳过，不发布部分
    文件内部条目以外的推断；本函数不递归扫描目录。

    community_drivers 标准布局（driver.py + registry.yaml + startup.json）使用 YAML 注册表，
    其条目天然含 class.action_value_mappings/schema，是最完整的 source_registry。
    仅采纳 resource_type=device 或带 class.action_value_mappings 的条目。
    """
    try:
        import yaml
    except ModuleNotFoundError:
        logger.warning("[package] 未安装 pyyaml，跳过 registry.yaml 读取")
        return {}

    # ``entries`` 聚合以稳定设备定义身份索引的根目录 YAML 注册表（Registry）条目。
    entries: dict[str, dict[str, Any]] = {}
    for yaml_path in sorted(list(pkg_dir.glob("*.yaml")) + list(pkg_dir.glob("*.yml"))):
        try:
            # ``data`` 是单个根目录 YAML 文件的完整解析结果。
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            logger.warning(f"[package] 解析 {yaml_path} 失败: {exc}")
            continue
        if not isinstance(data, dict):
            continue
        for device_id, entry in data.items():
            if not isinstance(entry, dict):
                continue
            # ``cls`` 是当前设备条目的驱动与动作（Action）注册定义。
            cls = entry.get("class") if isinstance(entry.get("class"), dict) else {}
            # ``is_device`` 关闭式判断当前条目是否属于设备定义。
            is_device = entry.get("resource_type") == "device" or bool(
                cls.get("action_value_mappings")
            )
            if is_device:
                entries[str(device_id)] = entry
    return entries


def read_external_registry_devices(pkg_dir: Path) -> dict[str, dict[str, Any]]:
    """读取包内目录式外部注册表（Registry）的设备条目。

    参数：``pkg_dir`` 是包含项目声明的显式软件包根。
    返回：以设备定义身份为键、已展开本地 ``$ref`` 的注册表条目为值的字典；只
    读取声明目录下 ``devices`` 的直接 YAML 文件。
    异常：缺少 PyYAML、单个文件不可读、引用无效或 YAML 无效时记录警告并跳过；
    不扫描环境或猜测外部来源。

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

    # ``registry_roots`` 是项目显式声明或包内默认的注册表（Registry）根集合。
    registry_roots = discover_registry_paths_from_project(pkg_dir)
    if not registry_roots:
        return {}

    # ``entries`` 聚合以稳定设备定义身份索引的目录式注册表（Registry）条目。
    entries: dict[str, dict[str, Any]] = {}
    for root in registry_roots:
        # ``devices_dir`` 是当前注册表根中唯一允许读取设备定义的子目录。
        devices_dir = root / "devices"
        if not devices_dir.is_dir():
            continue
        for yaml_path in sorted(
            list(devices_dir.glob("*.yaml")) + list(devices_dir.glob("*.yml"))
        ):
            try:
                # ``raw`` 保留单文件解析形状，``data`` 是本地引用展开后的完整条目集合。
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
                # ``cls`` 是当前候选设备定义的驱动注册信息。
                cls = entry.get("class") if isinstance(entry.get("class"), dict) else {}
                # devices/ 目录下条目天然是设备；接受带 class.module 或显式 resource_type=device 的条目
                # ``is_device`` 关闭式判断条目是否具备可用设备定义身份。
                is_device = (
                    bool(cls.get("module")) or entry.get("resource_type") == "device"
                )
                if is_device:
                    entries[str(device_id)] = entry
    return entries


def build_archive(pkg_dir: Path, archive_path: Path) -> str:
    """生成排除本地产物的软件包发布归档及内容摘要。

    参数：``pkg_dir`` 是待归档软件包根；``archive_path`` 是目标 ``tar.gz`` 路径。
    返回：带 ``sha256:`` 前缀的小写归档内容摘要。
    异常：目录创建、归档读取或写入失败时传播原始异常；缓存、版本控制目录、工作
    数据和字节码不得进入归档。
    """

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    # ``arc_root`` 是归档内唯一的软件包顶层目录名。
    arc_root = pkg_dir.name

    def _filter(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
        """过滤不应进入发布归档的本地产物。

        参数：``tarinfo`` 是归档库当前候选成员。
        返回：可保留成员原值；缓存、工作目录或字节码返回 ``None``。
        异常：无。
        """

        # ``parts`` 用于判断候选归档成员是否落入任何禁止发布的目录。
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
    """把遗留 Python 类型注解映射为 JSON Schema 基础类型。

    参数：``py_type`` 是 AST 扫描得到的类型注解文本。
    返回：支持类型对应的 JSON Schema ``type``；泛型参数被忽略，未知类型稳定
    回退为 ``string``。
    异常：无；该宽松回退仅服务遗留上传投影，不能替代规范动作（Action）Schema
    编译和完整校验。
    """

    # ``base`` 是去除模块限定与泛型参数后的遗留类型名。
    base = (py_type or "").strip().split("[")[0].split(".")[-1]
    return _PY_TO_JSON_SCHEMA_TYPE.get(base, "string")


def build_action_value_mappings(actions: dict[str, Any]) -> dict[str, Any]:
    """把遗留 AST 动作元数据转换为上传接口需要的动作映射。

    参数：``actions`` 是按动作（Action）名索引的扫描元数据，可能包含参数、默认值、
    文档和动作类型。
    返回：按动作名索引的 ``action_value_mappings`` 兼容投影，包含 goal/result/
    feedback Schema 与描述。
    异常：无；形状无效的动作或参数被跳过，未知参数类型使用遗留字符串回退。本函数
    不编译或改变规范包目录（PackageCatalog）。
    """

    # ``result`` 是按稳定动作（Action）名索引的后端兼容映射。
    result: dict[str, Any] = {}
    for name, meta in actions.items():
        if not isinstance(meta, dict):
            continue
        # 以下集合共同描述当前动作（Action）的目标参数 Schema 与默认值。
        params = meta.get("params") if isinstance(meta.get("params"), list) else []
        goal_props: dict[str, Any] = {}
        required: list[str] = []
        goal_default: dict[str, Any] = {}
        for param in params:
            if not isinstance(param, dict):
                continue
            # ``pname`` 是当前动作（Action）参数在接口上的稳定字段名。
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
        # ``goal_schema`` 是当前动作（Action）的遗留输入 Schema 投影。
        goal_schema: dict[str, Any] = {"type": "object", "properties": goal_props}
        if required:
            goal_schema["required"] = required
        # ``action_args`` 保存装饰器显式声明，``action_type`` 是最终传输动作类型。
        action_args = (
            meta.get("action_args") if isinstance(meta.get("action_args"), dict) else {}
        )
        action_type_raw = action_args.get("action_type")
        action_type = "UniLabJsonCommand"
        if isinstance(action_type_raw, str) and action_type_raw.strip():
            action_type = action_type_raw.strip().split(":")[-1].split(".")[-1]
        # ``entry`` 是当前动作（Action）完整的后端兼容映射条目。
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
    """把遗留 AST 设备扫描结果转换为后端资源上传投影。

    参数：``devices`` 是按设备定义身份索引的 AST 元数据；``package_info`` 是同次
    软件包检查生成的不可分割上传元信息。
    返回：``/lab/resource`` 接口消费的资源 DTO 列表，每项保留对应原始
    ``source_registry`` 兼容投影。
    异常：无；无效的动作或 Handle 由各兼容转换器关闭式忽略。本函数不创建具体
    物料（Material），也不成为注册表（Registry）定义权威。
    """

    # ``resources`` 是本次遗留 AST 定义生成的后端资源上传投影。
    resources: list[dict[str, Any]] = []
    for device_id, meta in devices.items():
        # 以下局部值共同描述一个稳定设备定义及其动作（Action）、状态和 Handle。
        actions = meta.get("actions") if isinstance(meta.get("actions"), dict) else {}
        action_value_mappings = build_action_value_mappings(actions)
        status_props = (
            meta.get("status_properties")
            if isinstance(meta.get("status_properties"), dict)
            else {}
        )
        handles = meta.get("handles") if isinstance(meta.get("handles"), list) else []

        # ``reg_class`` 是后端兼容的设备驱动注册投影。
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
        # ``category`` 是当前设备定义的查询分类，不参与身份或运行时分配。
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
    """把 YAML 注册表（Registry）条目转换为后端资源上传投影。

    参数：``entries`` 是按设备定义身份索引的已解析 YAML 条目；``package_info``
    是同次软件包检查生成的上传元信息。
    返回：``/lab/resource`` 接口消费的资源 DTO 列表，每项完整保留原始条目作为
    ``source_registry``。
    异常：初始化参数强制策略无效时抛出 ``PackageCLIError``；不得降级丢弃安全
    约束或创建具体物料（Material）。

    条目本身已含 class.action_value_mappings/schema，直接作为 source_registry，
    后端 BuildEffectiveTemplate 可据此构造 effective_template。
    """
    # ``resources`` 是本次 YAML 定义生成的后端资源上传投影。
    resources: list[dict[str, Any]] = []
    for device_id, entry in entries.items():
        # ``cls`` 是当前设备定义的驱动与动作（Action）注册信息。
        cls = entry.get("class") if isinstance(entry.get("class"), dict) else {}
        # ``init_schema`` 与 ``init_enforce`` 共同表达设备初始化安全合同。
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
        # ``category`` 是查询分类兼容投影，不参与设备定义身份。
        category = entry.get("category") or entry.get("tags") or []
        if isinstance(category, str):
            category = [category]
        # ``resource`` 是当前设备定义的完整后端资源上传 DTO。
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
    """构造后端与 Edge 共同消费的遗留 ``package_info`` 上传投影。

    参数：``project`` 是统一项目解析结果；``class_namespace`` 是本次编译采用的
    类命名空间；``sha256`` 是归档内容摘要；``download_url`` 是可选可达下载地址；
    ``oss_object_key`` 是可选对象存储身份。
    返回：包含发行身份、版本、安装声明、依赖、摘要和远端定位字段的字典。
    异常：项目缺少必须的 ``name`` 或 ``version`` 时传播 ``KeyError``；该投影不
    替代包目录（PackageCatalog）的规范身份。
    """

    # ``name`` 是项目声明的稳定发行身份。
    name = project["name"]
    # ``info`` 是本次归档摘要绑定的完整遗留上传投影。
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
    返回：包含包目录（PackageCatalog）摘要、兼容上传投影和归档路径的字典。
    异常：路径、项目、静态定义或显式命名空间与规范目录冲突时
    抛出 ``PackageCLIError``；文件系统归档/写入异常直接传播。本函数不读取产品
    配置、不启动 ROS 或设备，并且不发布部分规范编译结果。
    """

    # ``pkg_dir`` 是本次检查唯一授权的软件包根。
    pkg_dir = Path(path).resolve()
    if not pkg_dir.is_dir():
        raise PackageCLIError(f"包目录不存在：{pkg_dir}")

    # ``project`` 是软件包身份、版本和依赖的统一项目事实。
    project = read_pyproject(pkg_dir)
    # ``canonical_package`` 用来区分规范工作区与仅含 YAML 的遗留软件包。
    canonical_package = pkg_dir / normalize_name(project["name"])
    catalog: PackageCatalog | None = None
    if canonical_package.joinpath("__init__.py").is_file():
        try:
            # ``catalog`` 是这次检查唯一的规范静态编译结果。
            catalog = compile_package_source(WorkspaceSource(pkg_dir))
        except PackageCompileError as error:
            # ``diagnostic_codes`` 是完整失败诊断的稳定代码摘要，不含部分目录。
            diagnostic_codes = ", ".join(item.code for item in error.diagnostics)
            raise PackageCLIError(f"包目录（PackageCatalog）编译失败：{diagnostic_codes}") from error
        except (TypeError, ValueError) as error:
            raise PackageCLIError("包目录（PackageCatalog）编译失败") from error

    # ``class_namespace`` 是规范目录或遗留投影最终采用的类命名空间身份。
    class_namespace = (
        catalog.namespace
        if catalog is not None
        else resolve_class_namespace(project["name"], namespace)
    )
    if catalog is not None and namespace is not None:
        # ``requested_namespace`` 是用户请求的遗留覆盖，用于与规范身份比对。
        requested_namespace = resolve_class_namespace(project["name"], namespace)
        if requested_namespace != catalog.namespace:
            raise PackageCLIError(
                "规范工作区命名空间由项目身份决定，不能用 --namespace 覆盖"
            )

    # 以下路径共同标识本次检查的产物代与不可变归档内容。
    out_path = Path(out_dir).resolve() if out_dir else (pkg_dir.parent / "dist")
    out_path.mkdir(parents=True, exist_ok=True)
    archive_name = f"{normalize_name(project['name'])}-{project['version']}.tar.gz"
    archive_path = out_path / archive_name
    # ``sha256`` 是后续上传投影绑定的归档内容指纹。
    sha256 = build_archive(pkg_dir, archive_path)

    # ``package_info`` 是与当前归档指纹绑定的遗留上传元信息。
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
        # ``device_source`` 记录上传投影采用的定义来源，``device_ids`` 是稳定身份集合。
        device_source = "包目录（PackageCatalog）"
        device_ids = [item["id"] for item in catalog_document["definitions"]["devices"]]
        resources = build_resources_from_registry(registry_entries, package_info)
        catalog_path = out_path / "package.catalog.json"
        catalog_path.write_bytes(catalog.to_canonical_bytes())
    else:
        # 设备来源优先级：根目录 YAML > 目录式外部注册表 > 遗留 AST。
        # ``yaml_entries`` 是遗留 YAML 定义来源；仅在缺失时回退目录式注册表。
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
            # ``ast_devices`` 是两种 YAML 来源均为空时的最终遗留扫描候选。
            ast_devices = scan_package_devices(pkg_dir)
            device_ids = sorted(ast_devices)
            resources = build_resources(ast_devices, package_info)
        catalog_path = None
    # ``devices`` 保留旧调用方只查询设备身份集合的兼容返回形状。
    devices = {rid: None for rid in device_ids}
    if not resources:
        print_status(
            f"警告：{pkg_dir} 未发现 registry.yaml / unilabos_registry/ 或 @device 设备，仅生成 package_info",
            "warning",
        )

    # 两个 JSON 路径是后端遗留上传接口消费的确定性兼容产物。
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
    """把遗留 Handle 扫描结果转换为后端兼容字段集合。

    参数：``handles`` 是扫描器产生的任意 Handle 候选列表。
    返回：只包含后端 ``RegHandle`` 已知字段的字典列表；非字典项被跳过，缺失字段
    以空字符串保持遗留上传兼容。
    异常：无；本函数不校验工作流（Workflow）连接语义，也不改变规范包目录
    （PackageCatalog）中的 Handle 定义。
    """

    # ``mapped`` 是保持输入顺序的后端 Handle 兼容投影。
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
    异常：文件不可读时传播原始 IO 异常；按固定分块读取，不改变文件位置或内容。
    """

    # ``digest`` 累积完整文件字节，形成归档内容指纹。
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
