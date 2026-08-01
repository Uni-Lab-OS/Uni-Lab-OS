"""M2A tests 共用的窄 Material/Site static-authority fake。"""

from __future__ import annotations

from collections.abc import Sequence

from unilabos.resources.authority import MaterialNotFound, MaterialRecord, SiteRecord

MOUNT_MATERIAL_UUID = "50000000-0000-4000-8000-000000000001"
OTHER_MOUNT_MATERIAL_UUID = "50000000-0000-4000-8000-000000000002"
FIXED_MATERIAL_UUID = "51000000-0000-4000-8000-000000000001"
PLATE_RESOURCE_TEMPLATE_UUID = "32000000-0000-4000-8000-000000000001"
INCOMPATIBLE_RESOURCE_TEMPLATE_UUID = "32000000-0000-4000-8000-000000000002"
MOUNT_RESOURCE_TEMPLATE_UUID = "31000000-0000-4000-8000-000000000001"

SITE_A_UUID = "60000000-0000-4000-8000-000000000001"
SITE_B_UUID = "60000000-0000-4000-8000-000000000002"
SITE_C_UUID = "60000000-0000-4000-8000-000000000003"
FOREIGN_SITE_UUID = "60000000-0000-4000-8000-000000000004"
DELETED_SITE_UUID = "60000000-0000-4000-8000-000000000005"
MISSING_SITE_UUID = "60000000-0000-4000-8000-000000000099"


def material_record(
    material_uuid: str,
    *,
    resource_template_uuid: str,
    deleted_at: str | None = None,
) -> MaterialRecord:
    return MaterialRecord(
        uuid=material_uuid,
        create_time="2026-08-02T00:00:00Z",
        update_time="2026-08-02T00:00:00Z",
        deleted_at=deleted_at,
        description=None,
        meta_data={},
        resource_template_uuid=resource_template_uuid,
        parent_uuid=None,
        klass="TestMaterial",
        barcode=f"TEST-{material_uuid}",
        name=f"Material {material_uuid}",
        config={},
        data={},
        disposition="active",
        material_kind="business",
        version=1,
    )


def site_record(
    site_uuid: str,
    *,
    material_uuid: str = MOUNT_MATERIAL_UUID,
    sort_order: int,
    allowed_resource_template_uuids: tuple[str, ...],
    occupied_material_uuid: str | None = None,
    deleted_at: str | None = None,
) -> SiteRecord:
    return SiteRecord(
        uuid=site_uuid,
        create_time="2026-08-02T00:00:00Z",
        update_time="2026-08-02T00:00:00Z",
        deleted_at=deleted_at,
        description=None,
        meta_data={},
        material_uuid=material_uuid,
        name=f"Site {site_uuid}",
        sort_order=sort_order,
        allowed_resource_template_uuids=allowed_resource_template_uuids,
        occupied_material_uuid=occupied_material_uuid,
        position_x=0.0,
        position_y=0.0,
        position_z=0.0,
        depth=1.0,
        length=1.0,
        width=1.0,
        version=1,
    )


DEFAULT_MATERIALS = (
    material_record(
        MOUNT_MATERIAL_UUID,
        resource_template_uuid=MOUNT_RESOURCE_TEMPLATE_UUID,
    ),
    material_record(
        OTHER_MOUNT_MATERIAL_UUID,
        resource_template_uuid=MOUNT_RESOURCE_TEMPLATE_UUID,
    ),
    material_record(
        FIXED_MATERIAL_UUID,
        resource_template_uuid=PLATE_RESOURCE_TEMPLATE_UUID,
    ),
)

DEFAULT_SITES = (
    # 显式允许目标模板，并持有固定 existing Material。
    site_record(
        SITE_A_UUID,
        sort_order=1,
        allowed_resource_template_uuids=(PLATE_RESOURCE_TEMPLATE_UUID,),
        occupied_material_uuid=FIXED_MATERIAL_UUID,
    ),
    # 空 allowlist 是 universal，不是“不允许任何模板”。
    site_record(
        SITE_B_UUID,
        sort_order=2,
        allowed_resource_template_uuids=(),
    ),
    site_record(
        SITE_C_UUID,
        sort_order=3,
        allowed_resource_template_uuids=(INCOMPATIBLE_RESOURCE_TEMPLATE_UUID,),
    ),
    site_record(
        FOREIGN_SITE_UUID,
        material_uuid=OTHER_MOUNT_MATERIAL_UUID,
        sort_order=1,
        allowed_resource_template_uuids=(PLATE_RESOURCE_TEMPLATE_UUID,),
    ),
    site_record(
        DELETED_SITE_UUID,
        sort_order=4,
        allowed_resource_template_uuids=(PLATE_RESOURCE_TEMPLATE_UUID,),
        deleted_at="2026-08-02T01:00:00Z",
    ),
)


class StaticMaterialSourceAuthority:
    """MaterialSourceStaticAuthority 的只读结构化 fake。"""

    def __init__(
        self,
        *,
        materials: Sequence[MaterialRecord] = DEFAULT_MATERIALS,
        sites: Sequence[SiteRecord] = DEFAULT_SITES,
    ) -> None:
        self._materials = {item.uuid: item for item in materials}
        self._sites = {item.uuid: item for item in sites}

    def get_material(self, material_uuid: str) -> MaterialRecord:
        try:
            return self._materials[material_uuid]
        except KeyError:
            raise MaterialNotFound(f"material {material_uuid} not found") from None

    def get_site(self, site_uuid: str) -> SiteRecord:
        try:
            return self._sites[site_uuid]
        except KeyError:
            raise MaterialNotFound(f"site {site_uuid} not found") from None

    def list_sites(self, material_uuid: str) -> Sequence[SiteRecord]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._sites.values()
                    if item.material_uuid == material_uuid and item.deleted_at is None
                ),
                key=lambda item: (item.sort_order, item.uuid),
            )
        )


def default_material_source_authority() -> StaticMaterialSourceAuthority:
    return StaticMaterialSourceAuthority()
