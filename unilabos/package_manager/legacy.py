"""Internal adapters for the legacy YAML/``--devices`` package layout.

This module is deliberately absent from :mod:`unilabos.package_manager`'s
public exports. New workspace and distribution flows compile PackageCatalog;
these helpers only keep the existing Registry compatibility path testable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import tomllib
import yaml

from unilabos.registry.yaml_ref import resolve_yaml_refs
from unilabos.utils import logger


def discover_registry_paths_from_project(project_root: str | Path) -> list[Path]:
    """Resolve explicitly declared legacy registry roots, then the old default."""

    root = Path(project_root).resolve()
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            document = {}
        raw_paths = (
            document.get("tool", {})
            .get("unilabos", {})
            .get("registry", {})
            .get("paths", [])
        )
        if isinstance(raw_paths, list):
            paths = [
                (root / item).resolve()
                for item in raw_paths
                if isinstance(item, str) and (root / item).is_dir()
            ]
            if paths:
                return paths
    fallback = root / "unilabos_registry"
    return [fallback] if fallback.is_dir() else []


def read_registry_yaml_devices(package_root: str | Path) -> dict[str, dict[str, Any]]:
    """Read legacy device entries placed directly in a package root."""

    root = Path(package_root).resolve()
    entries: dict[str, dict[str, Any]] = {}
    for path in sorted((*root.glob("*.yaml"), *root.glob("*.yml"))):
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - compatibility input is untrusted
            logger.warning(f"[package legacy] 解析 {path} 失败: {exc}")
            continue
        if not isinstance(document, dict):
            continue
        for definition_id, entry in document.items():
            if not isinstance(entry, dict):
                continue
            class_config = entry.get("class")
            class_config = class_config if isinstance(class_config, dict) else {}
            if entry.get("resource_type") == "device" or class_config.get(
                "action_value_mappings"
            ):
                entries[str(definition_id)] = entry
    return entries


def read_external_registry_devices(
    package_root: str | Path,
) -> dict[str, dict[str, Any]]:
    """Read the legacy ``unilabos_registry/devices`` folder layout."""

    entries: dict[str, dict[str, Any]] = {}
    for registry_root in discover_registry_paths_from_project(package_root):
        devices_dir = registry_root / "devices"
        if not devices_dir.is_dir():
            continue
        for path in sorted((*devices_dir.glob("*.yaml"), *devices_dir.glob("*.yml"))):
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
                document = resolve_yaml_refs(raw, base_file=path)
            except Exception as exc:  # noqa: BLE001 - compatibility input is untrusted
                logger.warning(f"[package legacy] 解析 {path} 失败: {exc}")
                continue
            if not isinstance(document, dict):
                continue
            for definition_id, entry in document.items():
                if not isinstance(entry, dict):
                    continue
                class_config = entry.get("class")
                class_config = class_config if isinstance(class_config, dict) else {}
                if class_config.get("module") or entry.get("resource_type") == "device":
                    entries[str(definition_id)] = entry
    return entries


__all__: list[str] = []
