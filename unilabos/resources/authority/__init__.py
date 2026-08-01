"""OS-local durable Material Authority public seam。"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from .models import (
    MaterialAdapter,
    MaterialAuthorityUnavailable,
    MaterialConflict,
    MaterialError,
    MaterialInvalidInput,
    MaterialNotFound,
    MaterialRecord,
    RuntimeAuthorityUnitOfWork,
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


class MaterialModule:
    """Material identity 与 invariant 的唯一 public facade。"""

    def __init__(self, adapter: MaterialAdapter):
        self._adapter = adapter

    def create_business_material(
        self,
        *,
        material_uuid: str,
        resource_template_uuid: str,
        barcode: str,
        uow: RuntimeAuthorityUnitOfWork | None = None,
    ) -> MaterialRecord:
        """创建一个初始为 active 的 durable business Material。"""

        if not isinstance(barcode, str):
            raise MaterialInvalidInput("barcode must be a string")
        return self._adapter.create_business_material(
            material_uuid=_canonical_uuid(material_uuid, "material_uuid"),
            resource_template_uuid=_canonical_uuid(
                resource_template_uuid,
                "resource_template_uuid",
            ),
            barcode=barcode,
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


__all__ = [
    "MaterialAuthorityUnavailable",
    "MaterialConflict",
    "MaterialError",
    "MaterialInvalidInput",
    "MaterialModule",
    "MaterialNotFound",
    "MaterialRecord",
]
