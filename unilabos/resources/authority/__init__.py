"""OS-local durable Material Authority public seam。"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any
from uuid import UUID

from .models import (
    MaterialAdapter,
    MaterialAuthorityUnavailable,
    MaterialConflict,
    MaterialError,
    MaterialInvalidInput,
    MaterialNotFound,
    MaterialRecord,
    ResourceTemplateIdentity,
    RuntimeAuthorityUnitOfWork,
    SiteRecord,
)


def _canonical_uuid(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise MaterialInvalidInput(f"{field} must be a UUID string")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise MaterialInvalidInput(f"{field} must be a valid UUID") from exc
    if parsed.int == 0:
        raise MaterialInvalidInput(f"{field} must not be nil")
    return str(parsed)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_object(value: Mapping[str, Any] | None, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise MaterialInvalidInput(f"{field} must be a JSON object")
    try:
        encoded = json.dumps(
            dict(value),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise MaterialInvalidInput(f"{field} must be a JSON object") from exc
    return decoded


def _finite_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MaterialInvalidInput(f"{field} must be a finite number")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise MaterialInvalidInput(f"{field} must be a finite number")
    return normalized


class MaterialModule:
    """Material identity 与 invariant 的唯一 public facade。"""

    def __init__(
        self,
        adapter: MaterialAdapter,
        *,
        resource_templates: Mapping[str, object],
    ):
        self._adapter = adapter
        canonical_templates: dict[str, ResourceTemplateIdentity] = {}
        for key, identity in resource_templates.items():
            if not isinstance(identity, ResourceTemplateIdentity):
                raise MaterialInvalidInput(
                    "resource_templates must contain ResourceTemplateIdentity values"
                )
            canonical_key = _canonical_uuid(key, "resource_template key")
            canonical_identity_uuid = _canonical_uuid(
                identity.uuid,
                "resource_template identity uuid",
            )
            if canonical_key != canonical_identity_uuid:
                raise MaterialInvalidInput(
                    "resource_template key must match identity uuid"
                )
            if not isinstance(identity.material_class, str):
                raise MaterialInvalidInput("resource_template class must be a string")
            material_class = identity.material_class.strip()
            if not material_class:
                raise MaterialInvalidInput("resource_template class must not be blank")
            canonical_templates[canonical_key] = ResourceTemplateIdentity(
                uuid=canonical_identity_uuid,
                material_class=material_class,
            )
        self._resource_templates = MappingProxyType(canonical_templates)

    def create_business_material(
        self,
        *,
        material_uuid: str,
        resource_template_uuid: str,
        barcode: str,
        name: str,
        description: str | None = None,
        meta_data: Mapping[str, Any] | None = None,
        config: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        uow: RuntimeAuthorityUnitOfWork | None = None,
    ) -> MaterialRecord:
        """创建一个初始为 active 的 durable business Material。"""

        if not isinstance(barcode, str):
            raise MaterialInvalidInput("barcode must be a string")
        if not isinstance(name, str) or not name.strip():
            raise MaterialInvalidInput("name must be a non-blank string")
        if description is not None and not isinstance(description, str):
            raise MaterialInvalidInput("description must be a string or null")
        canonical_template_uuid = _canonical_uuid(
            resource_template_uuid,
            "resource_template_uuid",
        )
        template = self._resource_templates.get(canonical_template_uuid)
        if template is None:
            raise MaterialInvalidInput("resource_template_uuid is not registered")
        return self._adapter.create_business_material(
            material_uuid=_canonical_uuid(material_uuid, "material_uuid"),
            resource_template_uuid=canonical_template_uuid,
            resource_class=template.material_class,
            barcode=barcode,
            name=name.strip(),
            description=description,
            meta_data=_json_object(meta_data, "meta_data"),
            config=_json_object(config, "config"),
            data=_json_object(data, "data"),
            now=_utc_now(),
            uow=uow,
        )

    def get_material(
        self,
        material_uuid: str,
        *,
        uow: RuntimeAuthorityUnitOfWork | None = None,
    ) -> MaterialRecord:
        """读取一个未 soft-delete 的 durable Material。"""

        canonical_uuid = _canonical_uuid(material_uuid, "material_uuid")
        material = self._adapter.get_material(canonical_uuid, uow=uow)
        if material is None:
            raise MaterialNotFound(f"material {canonical_uuid} not found")
        return material

    def create_site(
        self,
        *,
        site_uuid: str,
        description: str | None,
        meta_data: Mapping[str, Any] | None,
        material_uuid: str,
        name: str,
        sort_order: int,
        allowed_resource_template_uuids: Sequence[str],
        occupied_material_uuid: str | None,
        position_x: float,
        position_y: float,
        position_z: float,
        depth: float,
        length: float,
        width: float,
        uow: RuntimeAuthorityUnitOfWork | None = None,
    ) -> SiteRecord:
        """创建一个 Backend-shaped durable Site。"""

        if description is not None and not isinstance(description, str):
            raise MaterialInvalidInput("description must be a string or null")
        if not isinstance(name, str) or not name.strip():
            raise MaterialInvalidInput("name must be a non-blank string")
        if isinstance(sort_order, bool) or not isinstance(sort_order, int):
            raise MaterialInvalidInput("sort_order must be a non-negative integer")
        if sort_order < 0:
            raise MaterialInvalidInput("sort_order must be a non-negative integer")
        if isinstance(allowed_resource_template_uuids, (str, bytes)) or not isinstance(
            allowed_resource_template_uuids,
            Sequence,
        ):
            raise MaterialInvalidInput(
                "allowed_resource_template_uuids must be a UUID array"
            )

        allowed_templates: set[str] = set()
        for value in allowed_resource_template_uuids:
            canonical_template_uuid = _canonical_uuid(
                value,
                "allowed_resource_template_uuid",
            )
            if canonical_template_uuid not in self._resource_templates:
                raise MaterialInvalidInput(
                    "allowed resource template is not registered"
                )
            allowed_templates.add(canonical_template_uuid)

        geometry = {
            "position_x": _finite_number(position_x, "position_x"),
            "position_y": _finite_number(position_y, "position_y"),
            "position_z": _finite_number(position_z, "position_z"),
            "depth": _finite_number(depth, "depth"),
            "length": _finite_number(length, "length"),
            "width": _finite_number(width, "width"),
        }
        for field in ("depth", "length", "width"):
            if geometry[field] < 0:
                raise MaterialInvalidInput(f"{field} must not be negative")

        canonical_occupant_uuid = (
            _canonical_uuid(occupied_material_uuid, "occupied_material_uuid")
            if occupied_material_uuid is not None
            else None
        )
        canonical_material_uuid = _canonical_uuid(material_uuid, "material_uuid")
        if canonical_occupant_uuid == canonical_material_uuid:
            raise MaterialConflict("site placement would create a cycle")
        return self._adapter.create_site(
            site_uuid=_canonical_uuid(site_uuid, "site_uuid"),
            description=description,
            meta_data=_json_object(meta_data, "meta_data"),
            material_uuid=canonical_material_uuid,
            name=name.strip(),
            sort_order=sort_order,
            allowed_resource_template_uuids=tuple(sorted(allowed_templates)),
            occupied_material_uuid=canonical_occupant_uuid,
            **geometry,
            now=_utc_now(),
            uow=uow,
        )

    def get_site(
        self,
        site_uuid: str,
        *,
        uow: RuntimeAuthorityUnitOfWork | None = None,
    ) -> SiteRecord:
        """读取一个未 soft-delete 的 durable Site。"""

        canonical_uuid = _canonical_uuid(site_uuid, "site_uuid")
        site = self._adapter.get_site(canonical_uuid, uow=uow)
        if site is None:
            raise MaterialNotFound(f"site {canonical_uuid} not found")
        return site


__all__ = [
    "MaterialAuthorityUnavailable",
    "MaterialConflict",
    "MaterialError",
    "MaterialInvalidInput",
    "MaterialModule",
    "MaterialNotFound",
    "MaterialRecord",
    "ResourceTemplateIdentity",
    "SiteRecord",
]
