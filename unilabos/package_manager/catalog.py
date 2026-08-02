"""PackageCatalog 的不可变 schema、canonical serialization 与 diagnostics。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Literal

import rfc8785

JSONScalar = str | int | float | bool | None
JSONValue = JSONScalar | tuple["JSONValue", ...] | Mapping[str, "JSONValue"]


def _freeze_json(value: Any) -> JSONValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in sorted(value.items())}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    raise TypeError(f"Catalog 只接受 JSON 值，收到 {type(value).__name__}")


def _json_value(value: JSONValue) -> Any:
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


@dataclass(frozen=True)
class DistributionIdentity:
    name: str
    normalized_name: str
    version: str
    requires_python: str = ""
    dependencies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "dependencies",
            tuple(sorted(self.dependencies)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "dependencies": list(self.dependencies),
            "name": self.name,
            "normalized_name": self.normalized_name,
            "requires_python": self.requires_python,
            "version": self.version,
        }


@dataclass(frozen=True)
class DefinitionRecord:
    kind: Literal["device", "resource", "workflow"]
    id: str
    fqid: str
    module: str
    symbol: str
    declaring_file: str
    content_hash: str
    version: str = "1.0.0"
    displayname: str = ""
    description: str = ""
    category: tuple[str, ...] = ()
    manufacturer: str = ""
    details: Mapping[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "category", tuple(self.category))
        object.__setattr__(self, "details", _freeze_json(self.details))

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": list(self.category),
            "content_hash": self.content_hash,
            "declaring_file": self.declaring_file,
            "description": self.description,
            "details": _json_value(self.details),
            "displayname": self.displayname,
            "fqid": self.fqid,
            "id": self.id,
            "kind": self.kind,
            "manufacturer": self.manufacturer,
            "module": self.module,
            "symbol": self.symbol,
            "version": self.version,
        }


@dataclass(frozen=True)
class DefinitionCatalog:
    devices: tuple[DefinitionRecord, ...] = ()
    resources: tuple[DefinitionRecord, ...] = ()
    workflows: tuple[DefinitionRecord, ...] = ()

    def __post_init__(self) -> None:
        for name in ("devices", "resources", "workflows"):
            records = tuple(sorted(getattr(self, name), key=lambda item: item.fqid))
            object.__setattr__(self, name, records)

    def to_dict(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "devices": [item.to_dict() for item in self.devices],
            "resources": [item.to_dict() for item in self.resources],
            "workflows": [item.to_dict() for item in self.workflows],
        }


@dataclass(frozen=True)
class PackageAsset:
    logical_path: str
    digest: str
    size: int
    media_type: str = "application/octet-stream"

    def to_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "logical_path": self.logical_path,
            "media_type": self.media_type,
            "size": self.size,
        }


@dataclass(frozen=True)
class PackageDiagnostic:
    code: str
    severity: Literal["error", "warning"]
    message: str
    path: str | None = None
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
        }
        if self.path is not None:
            result["path"] = self.path
        if self.line is not None:
            result["line"] = self.line
        return result


class PackageCompileError(RuntimeError):
    """一个或多个结构化 diagnostics 阻止 Catalog 产生。"""

    def __init__(
        self, diagnostics: tuple[PackageDiagnostic, ...] | list[PackageDiagnostic]
    ):
        self.diagnostics = tuple(diagnostics)
        message = "; ".join(f"{item.code}: {item.message}" for item in self.diagnostics)
        super().__init__(message or "Package Catalog 编译失败")


@dataclass(frozen=True)
class PackageCatalog:
    schema_version: Literal["1"]
    distribution: DistributionIdentity
    import_package: str
    namespace: str
    definitions: DefinitionCatalog
    assets: tuple[PackageAsset, ...]
    content_digest: str
    catalog_digest: str
    diagnostics: tuple[PackageDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "assets",
            tuple(sorted(self.assets, key=lambda item: item.logical_path)),
        )
        object.__setattr__(
            self,
            "diagnostics",
            tuple(
                sorted(
                    self.diagnostics,
                    key=lambda item: (
                        item.severity,
                        item.code,
                        item.path or "",
                        item.line or 0,
                        item.message,
                    ),
                )
            ),
        )

    @classmethod
    def create(
        cls,
        *,
        distribution: DistributionIdentity,
        import_package: str,
        namespace: str,
        definitions: DefinitionCatalog,
        assets: tuple[PackageAsset, ...] = (),
        content_digest: str,
        diagnostics: tuple[PackageDiagnostic, ...] = (),
    ) -> PackageCatalog:
        catalog = cls(
            schema_version="1",
            distribution=distribution,
            import_package=import_package,
            namespace=namespace,
            definitions=definitions,
            assets=assets,
            content_digest=content_digest,
            catalog_digest="",
            diagnostics=diagnostics,
        )
        digest = (
            "sha256:"
            + hashlib.sha256(
                catalog._canonical_bytes(include_catalog_digest=False)
            ).hexdigest()
        )
        return replace(catalog, catalog_digest=digest)

    def to_dict(self, *, include_catalog_digest: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "assets": [item.to_dict() for item in self.assets],
            "content_digest": self.content_digest,
            "definitions": self.definitions.to_dict(),
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "distribution": self.distribution.to_dict(),
            "import_package": self.import_package,
            "namespace": self.namespace,
            "schema_version": self.schema_version,
        }
        if include_catalog_digest:
            result["catalog_digest"] = self.catalog_digest
        return result

    def _canonical_bytes(self, *, include_catalog_digest: bool) -> bytes:
        return rfc8785.dumps(
            self.to_dict(include_catalog_digest=include_catalog_digest)
        )

    def to_canonical_bytes(self) -> bytes:
        return self._canonical_bytes(include_catalog_digest=True)

    @classmethod
    def from_canonical_bytes(cls, payload: bytes) -> PackageCatalog:
        """Parse and verify one embedded canonical Catalog."""

        try:
            raw = json.loads(payload)
            if not isinstance(raw, dict) or raw.get("schema_version") != "1":
                raise ValueError("unsupported PackageCatalog schema")
            distribution = raw["distribution"]
            definitions = raw["definitions"]
            catalog = cls(
                schema_version="1",
                distribution=DistributionIdentity(
                    name=str(distribution["name"]),
                    normalized_name=str(distribution["normalized_name"]),
                    version=str(distribution["version"]),
                    requires_python=str(distribution.get("requires_python") or ""),
                    dependencies=tuple(distribution.get("dependencies") or ()),
                ),
                import_package=str(raw["import_package"]),
                namespace=str(raw["namespace"]),
                definitions=DefinitionCatalog(
                    devices=tuple(
                        _definition_from_dict(item)
                        for item in definitions.get("devices", ())
                    ),
                    resources=tuple(
                        _definition_from_dict(item)
                        for item in definitions.get("resources", ())
                    ),
                    workflows=tuple(
                        _definition_from_dict(item)
                        for item in definitions.get("workflows", ())
                    ),
                ),
                assets=tuple(
                    PackageAsset(
                        logical_path=str(item["logical_path"]),
                        digest=str(item["digest"]),
                        size=int(item["size"]),
                        media_type=str(item.get("media_type") or ""),
                    )
                    for item in raw.get("assets", ())
                ),
                content_digest=str(raw["content_digest"]),
                catalog_digest=str(raw["catalog_digest"]),
                diagnostics=tuple(
                    PackageDiagnostic(
                        code=str(item["code"]),
                        severity=str(item["severity"]),
                        message=str(item["message"]),
                        path=(str(item["path"]) if "path" in item else None),
                        line=(int(item["line"]) if "line" in item else None),
                    )
                    for item in raw.get("diagnostics", ())
                ),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"embedded PackageCatalog is invalid: {exc}") from exc

        expected_digest = (
            "sha256:"
            + hashlib.sha256(
                catalog._canonical_bytes(include_catalog_digest=False)
            ).hexdigest()
        )
        if catalog.catalog_digest != expected_digest:
            raise ValueError(
                "embedded PackageCatalog catalog_digest mismatch: "
                f"{catalog.catalog_digest} != {expected_digest}"
            )
        if payload != catalog.to_canonical_bytes():
            raise ValueError("embedded PackageCatalog is not canonical")
        return catalog


def _definition_from_dict(raw: Mapping[str, Any]) -> DefinitionRecord:
    return DefinitionRecord(
        kind=str(raw["kind"]),
        id=str(raw["id"]),
        fqid=str(raw["fqid"]),
        module=str(raw["module"]),
        symbol=str(raw["symbol"]),
        declaring_file=str(raw["declaring_file"]),
        content_hash=str(raw["content_hash"]),
        version=str(raw.get("version") or "1.0.0"),
        displayname=str(raw.get("displayname") or ""),
        description=str(raw.get("description") or ""),
        category=tuple(raw.get("category") or ()),
        manufacturer=str(raw.get("manufacturer") or ""),
        details=raw.get("details") or {},
    )


__all__ = [
    "DefinitionCatalog",
    "DefinitionRecord",
    "DistributionIdentity",
    "PackageAsset",
    "PackageCatalog",
    "PackageCompileError",
    "PackageDiagnostic",
]
