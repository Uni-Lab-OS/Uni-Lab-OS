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
from yaml.events import AliasEvent, MappingStartEvent, ScalarEvent, SequenceStartEvent
from yaml.nodes import MappingNode

from unilabos.workflow.models import validate_uuid

_PACKAGE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
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
class WorkflowSourceDeclaration:
    workflow_uuid: str
    relative_path: str


@dataclass(frozen=True)
class EditablePackageManifest:
    package_id: str
    package_root: Path
    workflows: tuple[WorkflowSourceDeclaration, ...]


class EditableSourceRegistrar(Protocol):
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
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            return True
    return False


def _regular_file_bytes(path: Path, *, missing_ok: bool) -> bytes | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise SourceDeclarationError("invalid_manifest") from None
    except OSError:
        raise SourceDeclarationError("invalid_workflow_source") from None
    if not stat.S_ISREG(metadata.st_mode):
        raise SourceDeclarationError("invalid_workflow_source")
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise SourceDeclarationError("invalid_workflow_source")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return stream.read()
    except SourceDeclarationError:
        raise
    except OSError:
        code = "invalid_manifest" if not missing_ok else "invalid_workflow_source"
        raise SourceDeclarationError(code) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _load_closed_yaml(raw: bytes) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
        document_count = 0
        for event in yaml.parse(text):
            if isinstance(event, AliasEvent):
                raise SourceDeclarationError("invalid_manifest")
            if isinstance(
                event, (MappingStartEvent, SequenceStartEvent, ScalarEvent)
            ) and (event.anchor is not None or event.tag is not None):
                raise SourceDeclarationError("invalid_manifest")
            if isinstance(event, yaml.events.DocumentStartEvent):
                document_count += 1
        if document_count != 1:
            raise SourceDeclarationError("invalid_manifest")
        value = yaml.load(text, Loader=_ClosedSafeLoader)
    except SourceDeclarationError:
        raise
    except (UnicodeError, yaml.YAMLError):
        raise SourceDeclarationError("invalid_manifest") from None
    if not isinstance(value, dict):
        raise SourceDeclarationError("invalid_manifest")
    return value


def _source_declaration(
    raw: Any,
    *,
    package_id: str,
) -> WorkflowSourceDeclaration:
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
    if workflow_uuid != raw_uuid:
        raise SourceDeclarationError("invalid_workflow_source")
    if "\\" in source:
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
    return WorkflowSourceDeclaration(
        workflow_uuid=workflow_uuid,
        relative_path=PurePosixPath(*path.parts[1:]).as_posix(),
    )


def load_editable_package_manifest(
    package_root: str | Path,
) -> EditablePackageManifest:
    """读取并完全验证一个显式选择的 editable package declaration。"""

    supplied_root = Path(os.path.abspath(package_root))
    if _contains_symlink(supplied_root):
        raise SourceDeclarationError("invalid_package_root")
    try:
        root = supplied_root.resolve(strict=True)
    except OSError:
        raise SourceDeclarationError("invalid_package_root") from None
    if not root.is_dir():
        raise SourceDeclarationError("invalid_package_root")

    raw = _regular_file_bytes(root / "package.yaml", missing_ok=False)
    assert raw is not None
    manifest = _load_closed_yaml(raw)
    if set(manifest) != {"package", "workflows"}:
        raise SourceDeclarationError("invalid_manifest")
    package = manifest["package"]
    workflows = manifest["workflows"]
    if not isinstance(package, dict) or set(package) != {"name"}:
        raise SourceDeclarationError("invalid_package")
    package_id = package["name"]
    if not isinstance(package_id, str) or _PACKAGE_NAME.fullmatch(package_id) is None:
        raise SourceDeclarationError("invalid_package")
    if not isinstance(workflows, list) or not workflows:
        raise SourceDeclarationError("invalid_manifest")

    source_root = root / package_id
    if _contains_symlink(source_root):
        raise SourceDeclarationError("invalid_package_root")
    try:
        resolved_source_root = source_root.resolve(strict=True)
        resolved_source_root.relative_to(root)
    except (OSError, ValueError):
        raise SourceDeclarationError("invalid_package_root") from None
    if not resolved_source_root.is_dir():
        raise SourceDeclarationError("invalid_package_root")

    declarations = tuple(
        _source_declaration(item, package_id=package_id) for item in workflows
    )
    workflow_ids = [item.workflow_uuid for item in declarations]
    relative_paths = [item.relative_path for item in declarations]
    if len(workflow_ids) != len(set(workflow_ids)) or len(relative_paths) != len(
        set(relative_paths)
    ):
        raise SourceDeclarationError("duplicate_workflow_source")

    for declaration in declarations:
        target = resolved_source_root.joinpath(
            *PurePosixPath(declaration.relative_path).parts
        )
        try:
            target.relative_to(resolved_source_root)
        except ValueError:
            raise SourceDeclarationError("invalid_workflow_source") from None
        for parent in target.parents:
            if parent == resolved_source_root:
                break
            if parent.exists() and parent.is_symlink():
                raise SourceDeclarationError("invalid_workflow_source")
        content = _regular_file_bytes(target, missing_ok=True)
        if content is not None:
            try:
                content.decode("utf-8")
            except UnicodeError:
                raise SourceDeclarationError("invalid_workflow_source") from None

    return EditablePackageManifest(
        package_id=package_id,
        package_root=resolved_source_root,
        workflows=declarations,
    )


def register_editable_package_sources(
    service: EditableSourceRegistrar,
    package_root: str | Path,
) -> tuple[dict[str, Any], ...]:
    """验证完整 manifest 后，按声明顺序调用既有 Service Interface。"""

    manifest = load_editable_package_manifest(package_root)
    return tuple(
        service.register_editable_source(
            workflow_uuid=declaration.workflow_uuid,
            package_id=manifest.package_id,
            package_root=manifest.package_root,
            relative_path=declaration.relative_path,
        )
        for declaration in manifest.workflows
    )


__all__ = [
    "EditablePackageManifest",
    "SourceDeclarationError",
    "WorkflowSourceDeclaration",
    "load_editable_package_manifest",
    "register_editable_package_sources",
]
