"""把显式 editable package 声明适配为持久 Authoring source 注册。"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

import yaml
from yaml.constructor import ConstructorError
from yaml.events import (
    AliasEvent,
    CollectionEndEvent,
    DocumentStartEvent,
    MappingStartEvent,
    ScalarEvent,
    SequenceStartEvent,
)
from yaml.nodes import MappingNode

from unilabos.workflow.models import validate_uuid
from unilabos.workflow.service import WorkflowConflict, WorkflowError

_PACKAGE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_MANIFEST_BYTE_LIMIT = 1024 * 1024
_SOURCE_BYTE_LIMIT = 8 * 1024 * 1024
_YAML_DEPTH_LIMIT = 32
_WORKFLOW_ENTRY_LIMIT = 1024
_YAML_SCALAR_BYTE_LIMIT = 1024 * 1024
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
_ERROR_MESSAGES = {
    "invalid_package_root": "editable package 根目录无效",
    "invalid_manifest": "package.yaml 声明格式不正确",
    "invalid_package": "package 声明无效",
    "invalid_workflow_source": "Workflow source 声明无效",
    "duplicate_workflow_source": "Workflow source 声明存在重复身份",
}


class SourceDeclarationError(RuntimeError):
    """不泄漏不可信 YAML 内容的稳定 package declaration 错误。"""

    def __init__(self, code: str):
        self.code = code
        super().__init__(_ERROR_MESSAGES.get(code, _ERROR_MESSAGES["invalid_manifest"]))


@dataclass(frozen=True)
class _WorkflowSourceDeclaration:
    workflow_uuid: str
    relative_path: str


@dataclass(frozen=True)
class _EditablePackageManifest:
    package_id: str
    package_root: Path
    workflows: tuple[_WorkflowSourceDeclaration, ...]


class _EditableSourceRegistrar(Protocol):
    def register_editable_source(
        self,
        *,
        workflow_uuid: str,
        package_id: str,
        package_root: str | Path,
        relative_path: str,
    ) -> dict[str, Any]: ...


class _ClosedSafeLoader(yaml.SafeLoader):
    """SafeLoader 加 duplicate mapping-key 拒绝。"""

    def construct_mapping(
        self,
        node: MappingNode,
        deep: bool = False,
    ) -> dict[Any, Any]:
        if not isinstance(node, MappingNode):
            raise ConstructorError(
                None, None, "expected a mapping node", node.start_mark
            )
        seen: set[Any] = set()
        for key_node, _value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in seen
            except TypeError as error:
                raise ConstructorError(
                    None,
                    None,
                    "unhashable mapping key",
                    key_node.start_mark,
                ) from error
            if duplicate:
                raise ConstructorError(
                    None,
                    None,
                    "duplicate mapping key",
                    key_node.start_mark,
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _contains_symlink(path: Path) -> bool:
    """静态快速拒绝；真正的 identity/竞态保护由 directory FD 提供。"""

    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            return True
    return False


def _open_directory_chain(path: Path) -> int:
    absolute = Path(os.path.abspath(path))
    current = -1
    try:
        current = os.open(absolute.anchor, _DIRECTORY_FLAGS)
        for part in absolute.parts[1:]:
            next_descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = next_descriptor
        return current
    except (OSError, TypeError, ValueError):
        if current >= 0:
            os.close(current)
        raise SourceDeclarationError("invalid_package_root") from None


def _read_regular_at(
    parent_fd: int,
    name: str,
    *,
    byte_limit: int,
    missing_ok: bool,
    error_code: str,
) -> bytes | None:
    descriptor = -1
    try:
        descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise SourceDeclarationError(error_code) from None
    except (OSError, TypeError, ValueError):
        raise SourceDeclarationError(error_code) from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > byte_limit:
            raise SourceDeclarationError(error_code)
        chunks = bytearray()
        while len(chunks) <= byte_limit:
            chunk = os.read(descriptor, min(64 * 1024, byte_limit + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        if len(chunks) > byte_limit:
            raise SourceDeclarationError(error_code)
        return bytes(chunks)
    except SourceDeclarationError:
        raise
    except (OSError, OverflowError, ValueError):
        raise SourceDeclarationError(error_code) from None
    finally:
        os.close(descriptor)


def _load_closed_yaml(raw: bytes) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
        document_count = 0
        depth = 0
        node_count = 0
        for event in yaml.parse(text):
            if isinstance(event, AliasEvent):
                raise SourceDeclarationError("invalid_manifest")
            if isinstance(event, (MappingStartEvent, SequenceStartEvent)):
                depth += 1
                node_count += 1
                if depth > _YAML_DEPTH_LIMIT:
                    raise SourceDeclarationError("invalid_manifest")
            elif isinstance(event, CollectionEndEvent):
                depth -= 1
            elif isinstance(event, ScalarEvent):
                node_count += 1
                if len(event.value.encode("utf-8")) > _YAML_SCALAR_BYTE_LIMIT:
                    raise SourceDeclarationError("invalid_manifest")
            if isinstance(
                event, (MappingStartEvent, SequenceStartEvent, ScalarEvent)
            ) and (event.anchor is not None or event.tag is not None):
                raise SourceDeclarationError("invalid_manifest")
            if isinstance(event, DocumentStartEvent):
                document_count += 1
            if node_count > (_WORKFLOW_ENTRY_LIMIT * 8 + 32):
                raise SourceDeclarationError("invalid_manifest")
        if document_count != 1 or depth != 0:
            raise SourceDeclarationError("invalid_manifest")
        value = yaml.load(text, Loader=_ClosedSafeLoader)
    except SourceDeclarationError:
        raise
    except (
        MemoryError,
        OverflowError,
        RecursionError,
        UnicodeError,
        ValueError,
        yaml.YAMLError,
    ):
        raise SourceDeclarationError("invalid_manifest") from None
    if not isinstance(value, dict):
        raise SourceDeclarationError("invalid_manifest")
    return value


def _source_declaration(
    raw: Any,
    *,
    package_id: str,
) -> _WorkflowSourceDeclaration:
    if not isinstance(raw, dict) or set(raw) != {"workflow_uuid", "source"}:
        raise SourceDeclarationError("invalid_workflow_source")
    raw_uuid = raw["workflow_uuid"]
    source = raw["source"]
    if not isinstance(raw_uuid, str) or not isinstance(source, str):
        raise SourceDeclarationError("invalid_workflow_source")
    try:
        workflow_uuid = validate_uuid(raw_uuid)
    except (TypeError, ValueError):
        raise SourceDeclarationError("invalid_workflow_source") from None
    if workflow_uuid != raw_uuid or "\\" in source or "\x00" in source:
        raise SourceDeclarationError("invalid_workflow_source")
    path = PurePosixPath(source)
    if (
        path.is_absolute()
        or len(path.parts) != 3
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.parts[0] != package_id
        or path.parts[1] != "workflows"
        or path.suffix != ".py"
        or not path.stem
    ):
        raise SourceDeclarationError("invalid_workflow_source")
    return _WorkflowSourceDeclaration(
        workflow_uuid=workflow_uuid,
        relative_path=PurePosixPath(*path.parts[1:]).as_posix(),
    )


def _open_child_directory(parent_fd: int, name: str, *, missing_ok: bool) -> int | None:
    try:
        return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise SourceDeclarationError("invalid_package_root") from None
    except (OSError, TypeError, ValueError):
        raise SourceDeclarationError("invalid_package_root") from None


def load_editable_package_manifest(
    package_root: str | Path,
) -> _EditablePackageManifest:
    """从固定 directory identity 读取并验证一个 editable package。"""

    selected_root = Path(os.path.abspath(package_root))
    try:
        selected_identity = selected_root.lstat()
    except (OSError, TypeError, ValueError):
        raise SourceDeclarationError("invalid_package_root") from None
    if not stat.S_ISDIR(selected_identity.st_mode) or _contains_symlink(selected_root):
        raise SourceDeclarationError("invalid_package_root")

    root_fd = _open_directory_chain(selected_root)
    try:
        opened_identity = os.fstat(root_fd)
        if (selected_identity.st_dev, selected_identity.st_ino) != (
            opened_identity.st_dev,
            opened_identity.st_ino,
        ):
            raise SourceDeclarationError("invalid_package_root")
        raw = _read_regular_at(
            root_fd,
            "package.yaml",
            byte_limit=_MANIFEST_BYTE_LIMIT,
            missing_ok=False,
            error_code="invalid_manifest",
        )
        assert raw is not None
        manifest = _load_closed_yaml(raw)
        if set(manifest) != {"package", "workflows"}:
            raise SourceDeclarationError("invalid_manifest")
        package = manifest["package"]
        workflows = manifest["workflows"]
        if not isinstance(package, dict) or set(package) != {"name"}:
            raise SourceDeclarationError("invalid_package")
        package_id = package["name"]
        if (
            not isinstance(package_id, str)
            or _PACKAGE_NAME.fullmatch(package_id) is None
        ):
            raise SourceDeclarationError("invalid_package")
        if (
            not isinstance(workflows, list)
            or not workflows
            or len(workflows) > _WORKFLOW_ENTRY_LIMIT
        ):
            raise SourceDeclarationError("invalid_manifest")

        source_root_fd = _open_child_directory(root_fd, package_id, missing_ok=False)
        assert source_root_fd is not None
        try:
            declarations = tuple(
                _source_declaration(item, package_id=package_id) for item in workflows
            )
            workflow_ids = [item.workflow_uuid for item in declarations]
            relative_paths = [item.relative_path for item in declarations]
            if len(workflow_ids) != len(set(workflow_ids)) or len(
                relative_paths
            ) != len(set(relative_paths)):
                raise SourceDeclarationError("duplicate_workflow_source")

            workflows_fd = _open_child_directory(
                source_root_fd,
                "workflows",
                missing_ok=True,
            )
            if workflows_fd is not None:
                try:
                    for declaration in declarations:
                        filename = PurePosixPath(declaration.relative_path).name
                        content = _read_regular_at(
                            workflows_fd,
                            filename,
                            byte_limit=_SOURCE_BYTE_LIMIT,
                            missing_ok=True,
                            error_code="invalid_workflow_source",
                        )
                        if content is not None:
                            try:
                                content.decode("utf-8")
                            except UnicodeError:
                                raise SourceDeclarationError(
                                    "invalid_workflow_source"
                                ) from None
                finally:
                    os.close(workflows_fd)
        finally:
            os.close(source_root_fd)
    except SourceDeclarationError:
        raise
    except (MemoryError, OSError, OverflowError, RecursionError, TypeError, ValueError):
        raise SourceDeclarationError("invalid_manifest") from None
    finally:
        os.close(root_fd)

    return _EditablePackageManifest(
        package_id=package_id,
        package_root=selected_root / package_id,
        workflows=declarations,
    )


def _manifest_roots(
    package_root: str | Path | tuple[str | Path, ...],
) -> tuple[str | Path, ...]:
    if isinstance(package_root, (str, Path)):
        return (package_root,)
    return tuple(package_root)


def _registration_rows(
    manifests: tuple[_EditablePackageManifest, ...],
) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "workflow_uuid": declaration.workflow_uuid,
            "package_id": manifest.package_id,
            "package_root": manifest.package_root,
            "relative_path": declaration.relative_path,
        }
        for manifest in manifests
        for declaration in manifest.workflows
    )


def _preflight_existing_identity(
    service: Any,
    rows: tuple[dict[str, Any], ...],
) -> None:
    list_sources = getattr(service, "list_registered_sources", None)
    get_workflow = getattr(service, "get_workflow", None)
    if not callable(list_sources) or not callable(get_workflow):
        return

    existing = list_sources()
    existing_by_workflow = {item["workflow_uuid"]: item for item in existing}
    physical_owner = {
        (Path(item["package_root"]), item["relative_path"]): item["workflow_uuid"]
        for item in existing
    }
    uri_owner = {item["source_uri"]: item["workflow_uuid"] for item in existing}
    package_roots: dict[str, Path] = {}
    for item in existing:
        root = Path(item["package_root"])
        prior = package_roots.setdefault(item["package_id"], root)
        if prior != root:
            raise WorkflowConflict("invalid_input")

    new_workflow_ids: set[str] = set()
    new_physical: set[tuple[Path, str]] = set()
    new_uris: set[str] = set()
    for row in rows:
        workflow_uuid = row["workflow_uuid"]
        package_id = row["package_id"]
        package_root = Path(row["package_root"])
        relative_path = row["relative_path"]
        physical = (package_root, relative_path)
        source_uri = f"package://{package_id}/{relative_path}"
        if (
            workflow_uuid in new_workflow_ids
            or physical in new_physical
            or source_uri in new_uris
        ):
            raise WorkflowConflict("invalid_input")
        new_workflow_ids.add(workflow_uuid)
        new_physical.add(physical)
        new_uris.add(source_uri)

        try:
            get_workflow(workflow_uuid)
        except WorkflowError as error:
            if error.code == "not_found":
                raise WorkflowError("workflow_not_found") from None
            raise
        current = existing_by_workflow.get(workflow_uuid)
        if current is not None and (
            current["package_id"] != package_id
            or Path(current["package_root"]) != package_root
            or current["relative_path"] != relative_path
        ):
            raise WorkflowConflict("invalid_input")
        if physical_owner.get(physical, workflow_uuid) != workflow_uuid:
            raise WorkflowConflict("invalid_input")
        if uri_owner.get(source_uri, workflow_uuid) != workflow_uuid:
            raise WorkflowConflict("invalid_input")
        prior_root = package_roots.setdefault(package_id, package_root)
        if prior_root != package_root:
            raise WorkflowConflict("invalid_input")


def register_editable_package_sources(
    service: _EditableSourceRegistrar,
    package_root: str | Path | tuple[str | Path, ...],
) -> tuple[dict[str, Any], ...]:
    """全量预检一个或多个 package 后，以单批次注册声明。"""

    manifests = tuple(
        load_editable_package_manifest(root) for root in _manifest_roots(package_root)
    )
    rows = _registration_rows(manifests)
    _preflight_existing_identity(service, rows)
    batch = getattr(service, "editable_source_registration_batch", None)
    if callable(batch):
        with batch():
            return tuple(service.register_editable_source(**row) for row in rows)
    return tuple(service.register_editable_source(**row) for row in rows)


__all__ = [
    "SourceDeclarationError",
    "load_editable_package_manifest",
    "register_editable_package_sources",
]
