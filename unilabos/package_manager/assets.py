"""随包模型资产闭包与路径安全规则。"""

from __future__ import annotations

import hashlib
import io
import mimetypes
from collections.abc import Iterable, Mapping
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from .catalog import (
    DefinitionRecord,
    PackageAsset,
    PackageCatalog,
    PackageDiagnostic,
)
from .sources import PackageSource

_ALLOWED_MODEL_SUFFIXES = frozenset(
    {
        ".dae",
        ".glb",
        ".gltf",
        ".iges",
        ".igs",
        ".jpeg",
        ".jpg",
        ".json",
        ".mtl",
        ".obj",
        ".ply",
        ".png",
        ".step",
        ".stl",
        ".stp",
        ".svg",
        ".urdf",
        ".webp",
        ".xacro",
        ".xml",
        ".yaml",
        ".yml",
    }
)


class PackageAssetResolver:
    """只通过 Catalog logical path 读取并复核当前 Source observation。"""

    def __init__(self, source: PackageSource, catalog: PackageCatalog) -> None:
        self._source = source
        self._catalog = catalog
        self._assets = {item.logical_path: item for item in catalog.assets}

    def public_metadata(self, logical_path: str) -> PackageAsset:
        try:
            return self._assets[logical_path]
        except KeyError as exc:
            raise ValueError(f"Package asset 未编目: {logical_path}") from exc

    def open_binary(self, logical_path: str) -> BinaryIO:
        metadata = self.public_metadata(logical_path)
        try:
            content = self._source.read_bytes(logical_path)
        except ValueError as exc:
            raise ValueError(f"Package asset 读取失败: {logical_path}: {exc}") from exc
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        if digest != metadata.digest or len(content) != metadata.size:
            raise ValueError(f"Package asset 摘要不一致: {logical_path}")
        return io.BytesIO(content)


def collect_model_assets(
    root: Path,
    records: Iterable[DefinitionRecord],
) -> tuple[
    tuple[DefinitionRecord, ...],
    tuple[PackageAsset, ...],
    tuple[PackageDiagnostic, ...],
    tuple[Path, ...],
]:
    """规范化 model.entry，并对每个 definition 的 ``models/`` 建立安全闭包。"""

    normalized_records: list[DefinitionRecord] = []
    assets: dict[str, PackageAsset] = {}
    asset_paths: dict[str, Path] = {}
    diagnostics: list[PackageDiagnostic] = []

    for record in records:
        model = record.details.get("model")
        if model is None:
            normalized_records.append(record)
            continue
        if not isinstance(model, Mapping):
            diagnostics.append(
                _diagnostic(record, "MODEL_INVALID", "model 必须是静态对象")
            )
            normalized_records.append(record)
            continue

        representations = _representation_entries(model)
        if not representations:
            diagnostics.append(
                _diagnostic(
                    record,
                    "MODEL_ENTRY_INVALID",
                    "model.entry 或 model.<representation>.entry 必须是静态相对路径",
                )
            )
            normalized_records.append(record)
            continue

        declaring_path = root / record.declaring_file
        normalized_model = _thaw(model)
        invalid = False
        scanned_models_roots: set[Path] = set()
        for representation_path, representation in representations:
            entry = representation["entry"]
            entry_logical = PurePosixPath(entry.replace("\\", "/"))
            if (
                entry_logical.is_absolute()
                or ".." in entry_logical.parts
                or "models" not in entry_logical.parts
            ):
                diagnostics.append(
                    _diagnostic(
                        record,
                        "ASSET_PATH_ESCAPE",
                        f"model entry 必须是 models/ 内的相对路径: {entry}",
                    )
                )
                invalid = True
                continue

            models_index = max(
                index
                for index, part in enumerate(entry_logical.parts)
                if part == "models"
            )
            models_relative = Path(*entry_logical.parts[: models_index + 1])
            models_root = declaring_path.parent / models_relative
            entry_path = declaring_path.parent.joinpath(*entry_logical.parts)
            if models_root.is_symlink() or entry_path.is_symlink():
                diagnostics.append(
                    _diagnostic(
                        record,
                        "ASSET_SYMLINK_UNSAFE",
                        f"model entry 与 owner models/ 不得是 symlink: {entry}",
                    )
                )
                invalid = True
                continue
            try:
                resolved_models_root = models_root.resolve(strict=True)
                resolved_entry = entry_path.resolve(strict=True)
            except FileNotFoundError:
                diagnostics.append(
                    _diagnostic(
                        record,
                        "ASSET_MISSING",
                        f"model entry 不存在: {entry}",
                    )
                )
                invalid = True
                continue

            if not resolved_entry.is_relative_to(resolved_models_root):
                diagnostics.append(
                    _diagnostic(
                        record,
                        "ASSET_PATH_ESCAPE",
                        f"model entry 必须位于其 owner models/ 内: {entry}",
                    )
                )
                invalid = True
                continue
            if not resolved_entry.is_file():
                diagnostics.append(
                    _diagnostic(
                        record, "ASSET_MISSING", f"model entry 不是文件: {entry}"
                    )
                )
                invalid = True
                continue

            _set_representation_entry(
                normalized_model,
                representation_path,
                resolved_entry.relative_to(root).as_posix(),
            )
            if resolved_models_root in scanned_models_roots:
                continue
            scanned_models_roots.add(resolved_models_root)

            for path in sorted(resolved_models_root.rglob("*")):
                if path.is_dir():
                    continue
                logical = path.relative_to(root).as_posix()
                if path.is_symlink():
                    diagnostics.append(
                        _diagnostic(
                            record,
                            "ASSET_SYMLINK_UNSAFE",
                            f"models/ 资产不得是 symlink: {logical}",
                        )
                    )
                    invalid = True
                    continue
                if path.suffix.lower() not in _ALLOWED_MODEL_SUFFIXES:
                    diagnostics.append(
                        _diagnostic(
                            record,
                            "ASSET_TYPE_UNSUPPORTED",
                            f"models/ 包含不支持的文件类型: {logical}",
                        )
                    )
                    invalid = True
                    continue
                content = path.read_bytes()
                media_type = (
                    mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                )
                assets[logical] = PackageAsset(
                    logical_path=logical,
                    digest="sha256:" + hashlib.sha256(content).hexdigest(),
                    size=len(content),
                    media_type=media_type,
                )
                asset_paths[logical] = path

        if invalid:
            normalized_records.append(record)
            continue

        details = _thaw(record.details)
        details["model"] = normalized_model
        normalized_records.append(replace(record, details=details))

    return (
        tuple(normalized_records),
        tuple(assets[key] for key in sorted(assets)),
        tuple(diagnostics),
        tuple(asset_paths[key] for key in sorted(asset_paths)),
    )


def _diagnostic(record: DefinitionRecord, code: str, message: str) -> PackageDiagnostic:
    return PackageDiagnostic(
        code=code,
        severity="error",
        message=message,
        path=record.declaring_file,
    )


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _representation_entries(
    model: Mapping[str, Any],
) -> list[tuple[tuple[str, ...], Mapping[str, Any]]]:
    direct_entry = model.get("entry")
    if isinstance(direct_entry, str) and direct_entry:
        return [((), model)]
    return [
        ((str(name),), representation)
        for name, representation in sorted(model.items())
        if isinstance(representation, Mapping)
        and isinstance(representation.get("entry"), str)
        and representation.get("entry")
    ]


def _set_representation_entry(
    model: dict[str, Any], representation_path: tuple[str, ...], entry: str
) -> None:
    if not representation_path:
        model["entry"] = entry
        return
    representation = model[representation_path[0]]
    representation["entry"] = entry


__all__ = ["PackageAssetResolver", "collect_model_assets"]
