"""wheel 内派生开发工作区清单的构建与完整性校验。"""

from __future__ import annotations

import base64
import hashlib
import json
import zipfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from ..package_catalog import PackageCatalog, WorkspaceSource
from ..package_catalog.project_metadata import parse_project_metadata
from .errors import PackageBuildError

WORKSPACE_MANIFEST_SCHEMA = "unilab-derived-workspace/v1"
WORKSPACE_MANIFEST_SUFFIX = ".dist-info/unilab_workspace/manifest.json"


def build_workspace_manifest_member(
    wheel: str | Path,
    source: WorkspaceSource,
    catalog: PackageCatalog,
    *,
    generated_members: Mapping[str, bytes],
) -> tuple[str, bytes]:
    """为候选 wheel 生成唯一派生开发工作区清单成员。

    参数：``wheel`` 是标准构建候选；``source`` 是同代暂存来源；``catalog`` 是
    同代包目录；``generated_members`` 是稍后注入 wheel 的重编译证据。
    返回：``.dist-info`` 成员名和规范 JSON 字节。
    异常：wheel 元数据、项目声明或可导出成员无效时抛出
    ``PackageBuildError``。
    """

    try:
        with zipfile.ZipFile(wheel) as archive:
            members = {
                item.filename: archive.read(item)
                for item in archive.infolist()
                if not item.is_dir()
            }
    except zipfile.BadZipFile as error:
        raise PackageBuildError("标准构建产物不是合法 wheel ZIP") from error
    record_names = [name for name in members if name.endswith(".dist-info/RECORD")]
    if len(record_names) != 1:
        raise PackageBuildError(f"wheel RECORD 数量不是 1：{len(record_names)}")
    dist_info_root = record_names[0].removesuffix("/RECORD")
    manifest_name = f"{dist_info_root}/unilab_workspace/manifest.json"

    package_prefix = f"{catalog.import_package}/"
    generated_prefix = f"{catalog.import_package}/_generated/"
    export_files_by_target: dict[str, dict[str, Any]] = {}
    for name, payload in sorted(members.items()):
        if not name.startswith(package_prefix) or name.startswith(generated_prefix):
            continue
        export_files_by_target[name] = _file_record(name, name, payload)
    evidence_prefix = f"{generated_prefix}workspace/"
    for name, payload in sorted(generated_members.items()):
        if name.startswith(evidence_prefix):
            target = name.removeprefix(evidence_prefix)
            existing = export_files_by_target.get(target)
            if existing is not None:
                if not _record_matches(existing, payload):
                    raise PackageBuildError("启动证据与 wheel 包成员内容不一致")
                continue
            export_files_by_target[target] = _file_record(name, target, payload)
    export_files = [export_files_by_target[key] for key in sorted(export_files_by_target)]

    project_bytes = source.read_bytes("pyproject.toml")
    project = parse_project_metadata(project_bytes)
    # 当前 PackageCatalog 的 content_digest 明确绑定项目声明原始字节；导出时保留
    # 同一已审计声明，才能在不改变编译语义的前提下获得 canonical parity。清单
    # 仍额外冻结统一解析后的项目身份，未来可独立迁移到语义化项目摘要。
    normalized_project = project_bytes
    package_yaml = source.read_bytes("package.yaml") if source.has_file("package.yaml") else None
    document: dict[str, Any] = {
        "schema_version": WORKSPACE_MANIFEST_SCHEMA,
        "distribution": catalog.distribution.name,
        "normalized_name": catalog.distribution.normalized_name,
        "version": catalog.distribution.version,
        "namespace": catalog.namespace,
        "import_package": catalog.import_package,
        "catalog_digest": catalog.catalog_digest,
        "content_digest": catalog.content_digest,
        "files": export_files,
        "pyproject": _inline_record(normalized_project),
        "project": {
            "name": project.name,
            "normalized_name": project.normalized_name,
            "version": project.version,
            "description": project.description,
            "license": project.license,
            "homepage": project.homepage,
            "requires_python": project.requires_python,
            "dependencies": list(project.dependencies),
            "registry_paths": list(project.registry_paths),
        },
    }
    if package_yaml is not None:
        document["package_yaml"] = _inline_record(package_yaml)
    payload = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return manifest_name, payload


def load_workspace_manifest(
    members: Mapping[str, bytes],
) -> dict[str, Any] | None:
    """严格读取 wheel 中唯一可选开发工作区清单。

    参数：``members`` 是已通过 wheel 安全与 RECORD 校验的普通成员。
    返回：老 wheel 无清单时返回 ``None``，否则返回新字典。
    异常：清单重复、非规范 JSON 或根形状无效时抛出 ``PackageBuildError``。
    """

    names = [name for name in members if name.endswith(WORKSPACE_MANIFEST_SUFFIX)]
    if not names:
        return None
    if len(names) != 1:
        raise PackageBuildError("wheel 开发工作区清单数量不是 1")
    payload = members[names[0]]
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise PackageBuildError("wheel 开发工作区清单不是合法 UTF-8 JSON") from error
    if not isinstance(document, dict):
        raise PackageBuildError("wheel 开发工作区清单根必须是对象")
    canonical = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if canonical != payload:
        raise PackageBuildError("wheel 开发工作区清单不是规范 JSON")
    return document


def validate_workspace_manifest(
    members: Mapping[str, bytes],
    catalog: PackageCatalog,
) -> dict[str, Any] | None:
    """校验可选工作区清单的身份、路径和成员摘要。

    参数：``members`` 是完整已验证 wheel 内容；``catalog`` 是同 wheel 规范目录。
    返回：老 wheel 返回 ``None``；新 wheel 返回验证后的清单。
    异常：身份、结构、路径、摘要或大小不一致时抛出 ``PackageBuildError``。
    """

    manifest = load_workspace_manifest(members)
    if manifest is None:
        return None
    expected_identity = {
        "schema_version": WORKSPACE_MANIFEST_SCHEMA,
        "distribution": catalog.distribution.name,
        "normalized_name": catalog.distribution.normalized_name,
        "version": catalog.distribution.version,
        "namespace": catalog.namespace,
        "import_package": catalog.import_package,
        "catalog_digest": catalog.catalog_digest,
        "content_digest": catalog.content_digest,
    }
    if any(manifest.get(key) != value for key, value in expected_identity.items()):
        raise PackageBuildError("wheel 开发工作区清单身份与包目录不一致")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise PackageBuildError("wheel 开发工作区清单缺少文件闭包")
    targets: set[str] = set()
    for record in files:
        if not isinstance(record, dict):
            raise PackageBuildError("wheel 开发工作区文件记录必须是对象")
        source_name = _safe_manifest_path(record.get("source"), "source")
        target_name = _safe_manifest_path(record.get("target"), "target")
        if target_name in targets:
            raise PackageBuildError("wheel 开发工作区清单包含重复目标")
        targets.add(target_name)
        payload = members.get(source_name)
        if payload is None or not _record_matches(record, payload):
            raise PackageBuildError("wheel 开发工作区成员摘要或大小不匹配")
    _decode_inline_record(manifest.get("pyproject"), "pyproject")
    if "package_yaml" in manifest:
        _decode_inline_record(manifest.get("package_yaml"), "package_yaml")
    return manifest


def _file_record(source: str, target: str, payload: bytes) -> dict[str, Any]:
    """生成一个 wheel 成员导出记录。

    参数：``source`` 是 wheel 成员；``target`` 是工作区相对路径；``payload`` 是内容。
    返回：带大小和 SHA-256 的新字典。
    异常：无。
    """

    return {
        "source": source,
        "target": target,
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _inline_record(payload: bytes) -> dict[str, Any]:
    """把一个根声明编码为带摘要的内联清单记录。

    参数：``payload`` 是待导出的完整字节。
    返回：Base64、大小和 SHA-256 字典。
    异常：无。
    """

    return {
        "content_base64": base64.b64encode(payload).decode("ascii"),
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _decode_inline_record(record: Any, label: str) -> bytes:
    """验证并解码一个内联根声明。

    参数：``record`` 是未知 JSON 值；``label`` 是安全诊断字段名。
    返回：完成大小和摘要校验的原始字节。
    异常：形状、Base64、大小或摘要无效时抛出 ``PackageBuildError``。
    """

    if not isinstance(record, dict):
        raise PackageBuildError(f"wheel 开发工作区清单缺少 {label}")
    encoded = record.get("content_base64")
    if not isinstance(encoded, str):
        raise PackageBuildError(f"wheel 开发工作区清单 {label} 内容无效")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as error:
        raise PackageBuildError(f"wheel 开发工作区清单 {label} Base64 无效") from error
    if not _record_matches(record, payload):
        raise PackageBuildError(f"wheel 开发工作区清单 {label} 摘要不匹配")
    return payload


def _record_matches(record: Mapping[str, Any], payload: bytes) -> bool:
    """判断一项清单记录是否匹配实际字节。

    参数：``record`` 是带大小和摘要的映射；``payload`` 是实际内容。
    返回：两项证据都精确匹配时为 ``True``。
    异常：无；无效字段返回 ``False``。
    """

    return record.get("size") == len(payload) and record.get("sha256") == (
        hashlib.sha256(payload).hexdigest()
    )


def _safe_manifest_path(value: Any, label: str) -> str:
    """验证清单中的工作区或 wheel 相对路径。

    参数：``value`` 是未知 JSON 值；``label`` 是诊断字段名。
    返回：安全 POSIX 路径文本。
    异常：空、绝对、反斜杠或逃逸路径抛出 ``PackageBuildError``。
    """

    if not isinstance(value, str) or not value or "\\" in value:
        raise PackageBuildError(f"wheel 开发工作区清单 {label} 路径无效")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PackageBuildError(f"wheel 开发工作区清单 {label} 路径逃逸")
    return path.as_posix()


__all__ = [
    "WORKSPACE_MANIFEST_SCHEMA",
    "build_workspace_manifest_member",
    "load_workspace_manifest",
    "validate_workspace_manifest",
]
