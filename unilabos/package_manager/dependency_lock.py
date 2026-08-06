"""显式软件包依赖锁（Package Dependency Lock）的事务管理深模块。"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any, Literal

import rfc8785
import yaml

from .catalog import PackageCatalog
from .compiler import compile_package_source
from .registry_snapshot import compile_registry_snapshot
from .sources import WorkspaceSource

DEPENDENCY_DECLARATION_FILE = "unilabos.packages.yaml"
DEPENDENCY_LOCK_FILE = "unilabos.packages.lock.json"
_DEPENDENCY_MUTATION_GUARD = ".unilabos.packages.mutation.lock"


class PackageDependencyError(RuntimeError):
    """表示显式软件包依赖不能被安全解析、验证或发布。"""


def _serialized_dependency_mutation(operation: Any) -> Any:
    """让依赖变更在跨进程工作区互斥中执行。

    参数：``operation`` 是 ``PackageDependencyManager`` 上的一次 add、update 或
    remove 方法。
    返回：保留原方法元数据、但在文件锁保护内执行的包装方法。
    异常：互斥文件无法打开或底层操作失败时传播原异常；锁总在退出时释放。
    """

    @wraps(operation)
    def serialized(manager: Any, *args: Any, **kwargs: Any) -> Any:
        """持有当前主工作区的唯一依赖变更锁并调用原操作。

        参数：``manager`` 是依赖管理器；``args`` 与 ``kwargs`` 是原方法参数。
        返回：原 add、update 或 remove 方法的完整锁结果。
        异常：文件锁或原方法异常原样传播，且不会遗留进程级互斥。
        """

        # ``guard_path`` 只协调声明和锁的写权威，不进入软件包目录或运行时依赖。
        guard_path = manager._workspace.root / _DEPENDENCY_MUTATION_GUARD
        try:
            with guard_path.open("a+b") as guard_handle:
                fcntl.flock(guard_handle.fileno(), fcntl.LOCK_EX)
                try:
                    return operation(manager, *args, **kwargs)
                finally:
                    fcntl.flock(guard_handle.fileno(), fcntl.LOCK_UN)
        except OSError as error:
            raise PackageDependencyError("无法取得软件包依赖变更互斥锁") from error

    return serialized


@dataclass(frozen=True, slots=True)
class LockedPackage:
    """一项已解析且可重放校验的软件包依赖事实。"""

    distribution_name: str
    normalized_name: str
    namespace: str
    version: str
    source: str
    source_kind: Literal["workspace"]
    catalog_digest: str
    content_digest: str
    definition_fqids: tuple[str, ...] = ()

    @classmethod
    def from_catalog(
        cls,
        *,
        catalog: PackageCatalog,
        source: str,
    ) -> LockedPackage:
        """从已完整编译的目录建立一项锁定依赖。

        参数：``catalog`` 是已验证的软件包目录（PackageCatalog）；``source`` 是
        相对主工作区保存的显式来源路径。
        返回：包含发行身份、目录摘要和完整定义身份集合的不可变锁条目。
        异常：无；目录字段已由统一静态编译器关闭式验证。
        """

        # ``definition_fqids`` 证明锁定目录覆盖完整定义，而不是部署图的有限子集。
        definition_fqids = tuple(
            sorted(
                item.fqid
                for item in (
                    *catalog.definitions.devices,
                    *catalog.definitions.resources,
                    *catalog.definitions.workflows,
                )
            )
        )
        return cls(
            distribution_name=catalog.distribution.name,
            normalized_name=catalog.distribution.normalized_name,
            namespace=catalog.namespace,
            version=catalog.distribution.version,
            source=source,
            source_kind="workspace",
            catalog_digest=catalog.catalog_digest,
            content_digest=catalog.content_digest,
            definition_fqids=definition_fqids,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> LockedPackage:
        """解析并验证一个锁文件条目。

        参数：``value`` 是 JSON 解码后的单条依赖对象。
        返回：形状与值域均已验证的不可变锁条目。
        异常：字段缺失、类型错误或来源种类不支持时抛出
        ``PackageDependencyError``，禁止把损坏锁解释成环境发现请求。
        """

        required_strings = (
            "distribution_name",
            "normalized_name",
            "namespace",
            "version",
            "source",
            "source_kind",
            "catalog_digest",
            "content_digest",
        )
        if any(
            not isinstance(value.get(key), str) or not str(value[key]).strip()
            for key in required_strings
        ):
            raise PackageDependencyError("软件包依赖锁条目缺少非空字符串字段")
        if value["source_kind"] != "workspace":
            raise PackageDependencyError("当前只接受显式 workspace 软件包来源")
        raw_fqids = value.get("definition_fqids", [])
        if not isinstance(raw_fqids, list) or any(
            not isinstance(item, str) or not item for item in raw_fqids
        ):
            raise PackageDependencyError("锁定定义身份必须是字符串数组")
        return cls(
            distribution_name=value["distribution_name"],
            normalized_name=value["normalized_name"],
            namespace=value["namespace"],
            version=value["version"],
            source=value["source"],
            source_kind="workspace",
            catalog_digest=value["catalog_digest"],
            content_digest=value["content_digest"],
            definition_fqids=tuple(sorted(raw_fqids)),
        )

    def to_dict(self) -> dict[str, Any]:
        """返回可规范序列化的锁条目。

        参数：无。
        返回：不共享内部容器的普通 JSON 字典。
        异常：无。
        """

        return {
            "catalog_digest": self.catalog_digest,
            "content_digest": self.content_digest,
            "definition_fqids": list(self.definition_fqids),
            "distribution_name": self.distribution_name,
            "namespace": self.namespace,
            "normalized_name": self.normalized_name,
            "source": self.source,
            "source_kind": self.source_kind,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class PackageDependencyLock:
    """主工作区当前完整、不可变的软件包依赖代际。"""

    schema_version: Literal["1"] = "1"
    packages: tuple[LockedPackage, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        """按规范命名空间排序并拒绝重复依赖身份。

        参数：无；读取构造字段。
        返回：无；将 ``packages`` 替换为稳定排序元组。
        异常：发行规范身份或命名空间重复时抛出 ``PackageDependencyError``。
        """

        ordered = tuple(
            sorted(self.packages, key=lambda item: (item.namespace, item.source))
        )
        if len({item.normalized_name for item in ordered}) != len(ordered):
            raise PackageDependencyError("软件包依赖发行身份重复")
        if len({item.namespace for item in ordered}) != len(ordered):
            raise PackageDependencyError("软件包依赖命名空间重复")
        object.__setattr__(self, "packages", ordered)

    @classmethod
    def from_bytes(cls, raw: bytes) -> PackageDependencyLock:
        """从规范 JSON 读取软件包依赖锁。

        参数：``raw`` 是 ``unilabos.packages.lock.json`` 原始字节。
        返回：已完成字段和身份校验的不可变依赖代际。
        异常：编码、JSON、版本或条目无效时抛出 ``PackageDependencyError``。
        """

        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise PackageDependencyError("软件包依赖锁不是合法 UTF-8 JSON") from error
        if not isinstance(value, dict) or value.get("schema_version") != "1":
            raise PackageDependencyError("软件包依赖锁版本无效")
        raw_packages = value.get("packages")
        if not isinstance(raw_packages, list) or any(
            not isinstance(item, dict) for item in raw_packages
        ):
            raise PackageDependencyError("软件包依赖锁 packages 必须是对象数组")
        return cls(
            packages=tuple(LockedPackage.from_dict(item) for item in raw_packages)
        )

    def to_canonical_bytes(self) -> bytes:
        """输出稳定的软件包依赖锁 JSON。

        参数：无。
        返回：按 RFC 8785 规范化且末尾带换行的 UTF-8 字节。
        异常：无；内部字段已限制为 JSON 值。
        """

        return rfc8785.dumps(
            {
                "packages": [item.to_dict() for item in self.packages],
                "schema_version": self.schema_version,
            }
        ) + b"\n"


class PackageDependencyManager:
    """在一个小接口后完成依赖解析、全局校验和双文件事务发布。"""

    def __init__(self, workspace: str | Path) -> None:
        """固定主工作区并恢复其当前显式依赖代际。

        参数：``workspace`` 是当前可编辑主工作区根。
        返回：无；构造函数只验证根目录，不写文件或发现环境软件包。
        异常：工作区缺失、包含符号链接或不可访问时传播 ``WorkspaceSource`` 的
        ``ValueError``。
        """

        self._workspace = WorkspaceSource(workspace)

    @_serialized_dependency_mutation
    def add(self, source: str | Path) -> PackageDependencyLock:
        """新增一个显式外部软件包并发布新的依赖锁。

        参数：``source`` 是相对主工作区或绝对的外部软件包工作区路径。
        返回：成功发布后的完整软件包依赖锁（Package Dependency Lock）。
        异常：来源无效、身份已存在、完整静态编译或聚合注册表校验失败时抛出；
        所有验证在写文件前完成，失败保持原声明和锁不变。
        """

        declarations, current_lock = _load_dependency_state(self._workspace.root)
        source_path, portable_source = _resolve_dependency_source(
            self._workspace.root,
            source,
        )
        catalog = _compile_dependency_catalog(WorkspaceSource(source_path))
        if any(
            item.normalized_name == catalog.distribution.normalized_name
            for item in current_lock.packages
        ):
            raise PackageDependencyError(
                f"软件包依赖已存在，请使用 update: {catalog.distribution.name}"
            )
        new_entry = LockedPackage.from_catalog(
            catalog=catalog,
            source=portable_source,
        )
        next_lock = PackageDependencyLock(
            packages=(*current_lock.packages, new_entry)
        )
        next_declarations = {
            **declarations,
            new_entry.normalized_name: (
                new_entry.distribution_name,
                new_entry.source,
            ),
        }
        _validate_complete_generation(
            workspace=self._workspace,
            dependency_catalogs=tuple(
                _catalog_for_entry(
                    workspace_root=self._workspace.root,
                    entry=item,
                    verify_lock=item.normalized_name != new_entry.normalized_name,
                )
                if item.normalized_name != new_entry.normalized_name
                else catalog
                for item in next_lock.packages
            ),
        )
        _publish_dependency_state(
            workspace_root=self._workspace.root,
            declarations=next_declarations,
            dependency_lock=next_lock,
        )
        return next_lock

    @_serialized_dependency_mutation
    def update(
        self,
        identity: str,
        source: str | Path | None = None,
    ) -> PackageDependencyLock:
        """重新解析并锁定一个既有显式外部软件包。

        参数：``identity`` 是发行名、规范化发行名或社区命名空间；``source`` 是
        可选新来源，省略时继续使用既有显式路径。
        返回：内容摘要已推进、依赖身份不变的完整软件包依赖锁。
        异常：目标不存在、新来源发行身份变化、任一其他依赖漂移或聚合校验失败
        时关闭式抛出；验证失败不修改声明和锁。
        """

        declarations, current_lock = _load_dependency_state(self._workspace.root)
        current_entry = _find_locked_package(current_lock, identity)
        selected_source = source if source is not None else current_entry.source
        source_path, portable_source = _resolve_dependency_source(
            self._workspace.root,
            selected_source,
        )
        catalog = _compile_dependency_catalog(WorkspaceSource(source_path))
        next_entry = LockedPackage.from_catalog(
            catalog=catalog,
            source=portable_source,
        )
        if next_entry.normalized_name != current_entry.normalized_name:
            raise PackageDependencyError(
                "软件包 update 不得改变发行身份: "
                f"{current_entry.distribution_name} -> {next_entry.distribution_name}"
            )
        next_lock = PackageDependencyLock(
            packages=tuple(
                next_entry
                if item.normalized_name == current_entry.normalized_name
                else item
                for item in current_lock.packages
            )
        )
        next_declarations = {
            **declarations,
            next_entry.normalized_name: (
                next_entry.distribution_name,
                next_entry.source,
            ),
        }
        dependency_catalogs = tuple(
            catalog
            if item.normalized_name == next_entry.normalized_name
            else _catalog_for_entry(
                workspace_root=self._workspace.root,
                entry=item,
                verify_lock=True,
            )
            for item in next_lock.packages
        )
        _validate_complete_generation(
            workspace=self._workspace,
            dependency_catalogs=dependency_catalogs,
        )
        _publish_dependency_state(
            workspace_root=self._workspace.root,
            declarations=next_declarations,
            dependency_lock=next_lock,
        )
        return next_lock

    @_serialized_dependency_mutation
    def remove(self, identity: str) -> PackageDependencyLock:
        """删除一个显式外部软件包并重新验证剩余完整代际。

        参数：``identity`` 是发行名、规范化发行名或社区命名空间。
        返回：不再包含目标、但仍显式保存为空也合法的完整依赖锁。
        异常：目标不存在、其他依赖已经漂移或剩余聚合校验失败时关闭式抛出；
        失败不会切换到 ambient site-packages。
        """

        declarations, current_lock = _load_dependency_state(self._workspace.root)
        current_entry = _find_locked_package(current_lock, identity)
        next_lock = PackageDependencyLock(
            packages=tuple(
                item
                for item in current_lock.packages
                if item.normalized_name != current_entry.normalized_name
            )
        )
        next_declarations = {
            key: value
            for key, value in declarations.items()
            if key != current_entry.normalized_name
        }
        remaining_catalogs = tuple(
            _catalog_for_entry(
                workspace_root=self._workspace.root,
                entry=item,
                verify_lock=True,
            )
            for item in next_lock.packages
        )
        _validate_complete_generation(
            workspace=self._workspace,
            dependency_catalogs=remaining_catalogs,
        )
        _publish_dependency_state(
            workspace_root=self._workspace.root,
            declarations=next_declarations,
            dependency_lock=next_lock,
        )
        return next_lock


def load_locked_package_catalogs(
    workspace: str | Path,
) -> tuple[PackageCatalog, ...]:
    """只从显式声明和锁文件加载外部软件包目录。

    参数：``workspace`` 是主工作区根；函数从不扫描 ``sys.path`` 或 ambient
    site-packages。
    返回：按命名空间排序、重新完整编译且摘要与锁一致的软件包目录元组。
    异常：声明/锁缺一、身份或摘要漂移、来源无效、跨包冲突时抛出
    ``PackageDependencyError``，不返回部分集合。
    """

    workspace_source = WorkspaceSource(workspace)
    _declarations, dependency_lock = _load_dependency_state(workspace_source.root)
    catalogs = tuple(
        _catalog_for_entry(
            workspace_root=workspace_source.root,
            entry=item,
            verify_lock=True,
        )
        for item in dependency_lock.packages
    )
    _validate_complete_generation(
        workspace=workspace_source,
        dependency_catalogs=catalogs,
    )
    return tuple(sorted(catalogs, key=lambda item: item.namespace))


def _resolve_dependency_source(
    workspace_root: Path,
    source: str | Path,
) -> tuple[Path, str]:
    """解析外部软件包来源并生成可移植声明路径。

    参数：``workspace_root`` 是主工作区；``source`` 是绝对路径或相对主工作区
    的路径。
    返回：经过 ``WorkspaceSource`` 校验的规范绝对路径及 POSIX 相对声明。
    异常：来源为空、指回主工作区或不安全时抛出 ``PackageDependencyError``。
    """

    if not str(source).strip():
        raise PackageDependencyError("软件包依赖来源不能为空")
    selected = Path(source).expanduser()
    if not selected.is_absolute():
        selected = workspace_root / selected
    try:
        source_root = WorkspaceSource(selected).root
    except ValueError as error:
        raise PackageDependencyError("软件包依赖来源不是安全工作区") from error
    if source_root == workspace_root:
        raise PackageDependencyError("主工作区不能依赖自身")
    portable_source = Path(os.path.relpath(source_root, workspace_root)).as_posix()
    return source_root, portable_source


def _find_locked_package(
    dependency_lock: PackageDependencyLock,
    identity: str,
) -> LockedPackage:
    """按三种稳定外部身份定位唯一锁条目。

    参数：``dependency_lock`` 是当前完整锁；``identity`` 是原发行名、规范化发行
    名或社区命名空间。
    返回：唯一匹配的既有锁条目。
    异常：身份为空、不存在或因损坏数据出现多重匹配时抛出
    ``PackageDependencyError``。
    """

    normalized_identity = identity.strip() if isinstance(identity, str) else ""
    if not normalized_identity:
        raise PackageDependencyError("软件包依赖身份不能为空")
    matches = tuple(
        item
        for item in dependency_lock.packages
        if normalized_identity
        in {
            item.distribution_name,
            item.normalized_name,
            item.namespace,
        }
    )
    if len(matches) != 1:
        raise PackageDependencyError(
            f"软件包依赖不存在或身份不唯一: {normalized_identity}"
        )
    return matches[0]


def _load_dependency_state(
    workspace_root: Path,
) -> tuple[dict[str, tuple[str, str]], PackageDependencyLock]:
    """读取必须成对出现的显式依赖声明和锁。

    参数：``workspace_root`` 是主工作区规范根。
    返回：以规范发行身份索引的声明和不可变锁；两文件均不存在时返回空代际。
    异常：只存在一个文件、YAML/JSON 形状无效或声明身份重复时抛出
    ``PackageDependencyError``。
    """

    declaration_path = workspace_root / DEPENDENCY_DECLARATION_FILE
    lock_path = workspace_root / DEPENDENCY_LOCK_FILE
    if not declaration_path.exists() and not lock_path.exists():
        return {}, PackageDependencyLock()
    if not declaration_path.is_file() or not lock_path.is_file():
        raise PackageDependencyError("软件包依赖声明和锁必须成对存在")
    if declaration_path.is_symlink() or lock_path.is_symlink():
        raise PackageDependencyError("软件包依赖声明和锁不得是符号链接")
    try:
        document = yaml.safe_load(declaration_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise PackageDependencyError("软件包依赖声明不是合法 YAML") from error
    if not isinstance(document, dict) or document.get("schema_version") != "1":
        raise PackageDependencyError("软件包依赖声明版本无效")
    raw_dependencies = document.get("dependencies")
    if not isinstance(raw_dependencies, list) or any(
        not isinstance(item, dict) for item in raw_dependencies
    ):
        raise PackageDependencyError("软件包依赖声明 dependencies 必须是对象数组")
    declarations: dict[str, tuple[str, str]] = {}
    for item in raw_dependencies:
        distribution_name = item.get("name")
        source = item.get("source")
        normalized_name = item.get("normalized_name")
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (distribution_name, normalized_name, source)
        ):
            raise PackageDependencyError("软件包依赖声明字段无效")
        if normalized_name in declarations:
            raise PackageDependencyError("软件包依赖声明身份重复")
        declarations[normalized_name] = (distribution_name, source)
    try:
        dependency_lock = PackageDependencyLock.from_bytes(lock_path.read_bytes())
    except OSError as error:
        raise PackageDependencyError("软件包依赖锁不可读取") from error
    locked_declarations = {
        item.normalized_name: (item.distribution_name, item.source)
        for item in dependency_lock.packages
    }
    if locked_declarations != declarations:
        raise PackageDependencyError("软件包依赖声明与锁不一致")
    return declarations, dependency_lock


def _catalog_for_entry(
    *,
    workspace_root: Path,
    entry: LockedPackage,
    verify_lock: bool,
) -> PackageCatalog:
    """重新编译一项锁定来源并按需核对全部身份摘要。

    参数：``workspace_root`` 是主工作区；``entry`` 是锁条目；``verify_lock``
    决定是否要求重编译目录与现有锁完全一致。
    返回：只从显式路径观察得到的软件包目录（PackageCatalog）。
    异常：来源越界、编译失败或任一锁字段漂移时抛出
    ``PackageDependencyError``。
    """

    source_path, portable_source = _resolve_dependency_source(
        workspace_root,
        entry.source,
    )
    catalog = _compile_dependency_catalog(WorkspaceSource(source_path))
    rebuilt = LockedPackage.from_catalog(catalog=catalog, source=portable_source)
    if verify_lock and rebuilt != entry:
        raise PackageDependencyError(
            f"软件包依赖内容与锁不一致，请先 update: {entry.distribution_name}"
        )
    return catalog


def _validate_complete_generation(
    *,
    workspace: WorkspaceSource,
    dependency_catalogs: tuple[PackageCatalog, ...],
) -> None:
    """完整校验主包与全部依赖的聚合注册表代际。

    参数：``workspace`` 是主工作区来源；``dependency_catalogs`` 是候选依赖完整
    目录集合。
    返回：无；全部规范身份和别名关系合法时完成。
    异常：主包编译或跨包注册表冲突时传播原始关闭式异常；调用者尚未写文件。
    """

    # ``root_catalog`` 让外部定义不能与当前产品工作区共享规范命名空间。
    root_catalog = _compile_dependency_catalog(workspace)
    try:
        compile_registry_snapshot((root_catalog, *dependency_catalogs))
    except (TypeError, ValueError, RuntimeError) as error:
        raise PackageDependencyError("软件包依赖聚合注册表校验失败") from error


def _compile_dependency_catalog(source: WorkspaceSource) -> PackageCatalog:
    """通过唯一静态编译器规范化依赖错误边界。

    参数：``source`` 是主工作区或显式外部工作区来源。
    返回：完整、不可变且没有导入作者模块的软件包目录（PackageCatalog）。
    异常：来源、语法、动作合同（Action Contract）或身份无效时统一抛出
    ``PackageDependencyError``，保留原异常作为诊断链。
    """

    try:
        return compile_package_source(source)
    except (TypeError, ValueError, RuntimeError) as error:
        raise PackageDependencyError(
            f"软件包依赖完整静态编译失败: {source.root}"
        ) from error


def _declaration_bytes(
    declarations: Mapping[str, tuple[str, str]],
) -> bytes:
    """生成稳定、独立于原 YAML 格式的显式依赖声明。

    参数：``declarations`` 以规范发行身份索引名称和来源。
    返回：字段稳定排序且末尾带换行的 UTF-8 YAML。
    异常：无；调用前字段已验证。
    """

    payload = {
        "schema_version": "1",
        "dependencies": [
            {
                "name": declarations[key][0],
                "normalized_name": key,
                "source": declarations[key][1],
            }
            for key in sorted(declarations)
        ],
    }
    return yaml.safe_dump(
        payload,
        allow_unicode=True,
        sort_keys=False,
    ).encode("utf-8")


def _publish_dependency_state(
    *,
    workspace_root: Path,
    declarations: Mapping[str, tuple[str, str]],
    dependency_lock: PackageDependencyLock,
) -> None:
    """用可回滚的同目录替换发布声明和锁文件。

    参数：``workspace_root`` 是两个权威文件的共同目录；``declarations`` 和
    ``dependency_lock`` 是已完整校验的下一代事实。
    返回：无；两个目标均替换成功后才完成。
    异常：临时文件写入或替换失败时恢复两个旧文件并抛出
    ``PackageDependencyError``；不会留下已知的单文件新代际。
    """

    targets = (
        (
            workspace_root / DEPENDENCY_DECLARATION_FILE,
            _declaration_bytes(declarations),
        ),
        (workspace_root / DEPENDENCY_LOCK_FILE, dependency_lock.to_canonical_bytes()),
    )
    originals = {
        target: target.read_bytes() if target.is_file() else None
        for target, _content in targets
    }
    temporary_paths: list[Path] = []
    try:
        for target, content in targets:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=workspace_root,
                prefix=f".{target.name}.",
                suffix=".tmp",
            )
            temporary_path = Path(temporary_name)
            temporary_paths.append(temporary_path)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        for (target, _content), temporary_path in zip(
            targets,
            temporary_paths,
            strict=True,
        ):
            os.replace(temporary_path, target)
    except OSError as error:
        _restore_dependency_files(originals)
        raise PackageDependencyError("软件包依赖声明和锁发布失败") from error
    finally:
        for temporary_path in temporary_paths:
            temporary_path.unlink(missing_ok=True)


def _restore_dependency_files(originals: Mapping[Path, bytes | None]) -> None:
    """尽最大安全努力恢复一次失败发布前的两个依赖文件。

    参数：``originals`` 是每个目标的旧字节；``None`` 表示目标原本不存在。
    返回：无；每个恢复写使用同目录原子替换。
    异常：恢复本身失败时抛出 ``PackageDependencyError``，要求人工检查工作区；
    不能静默声明事务已恢复。
    """

    try:
        for target, content in originals.items():
            if content is None:
                target.unlink(missing_ok=True)
                continue
            descriptor, temporary_name = tempfile.mkstemp(
                dir=target.parent,
                prefix=f".{target.name}.restore.",
                suffix=".tmp",
            )
            temporary_path = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_path, target)
            finally:
                temporary_path.unlink(missing_ok=True)
    except OSError as error:
        raise PackageDependencyError(
            "软件包依赖发布回滚失败，需要人工检查声明和锁"
        ) from error


__all__ = [
    "DEPENDENCY_DECLARATION_FILE",
    "DEPENDENCY_LOCK_FILE",
    "LockedPackage",
    "PackageDependencyError",
    "PackageDependencyLock",
    "PackageDependencyManager",
    "load_locked_package_catalogs",
]
