"""MaterialSource 对 durable Material/Site authority 的只读静态证明。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Never, Protocol
from uuid import UUID

from unilabos.resources.authority import (
    MaterialAuthorityUnavailable,
    MaterialError,
    MaterialNotFound,
    MaterialRecord,
    SiteRecord,
)


class MaterialSourceStaticAuthority(Protocol):
    """MaterialSource Preview/Save 所需的最小只读 authority port。"""

    def get_material(self, material_uuid: str) -> MaterialRecord: ...

    def get_site(self, site_uuid: str) -> SiteRecord: ...

    def list_sites(self, material_uuid: str) -> Sequence[SiteRecord]: ...


class MaterialSourceAuthorityError(RuntimeError):
    """对外稳定的 MaterialSource 静态 authority 诊断。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_material_source_authority(
    graph: Mapping[str, Any],
    authority: MaterialSourceStaticAuthority | None,
) -> None:
    """证明每个 canonical MaterialSource selector 的静态位置可行性。"""

    nodes = graph.get("nodes")
    if not isinstance(nodes, list):
        return
    selectors = [
        node.get("param")
        for node in nodes
        if isinstance(node, Mapping) and node.get("type") == "material_source"
    ]
    authority_selectors = [
        selector
        for selector in selectors
        if isinstance(selector, Mapping) and _selector_has_authority_shape(selector)
    ]
    if not authority_selectors:
        return
    if authority is None:
        raise MaterialSourceAuthorityError(
            "material_authority_unavailable",
            "MaterialSource 静态 authority 未配置",
        )
    for selector in authority_selectors:
        _validate_selector_authority(selector, authority)


def _selector_has_authority_shape(selector: Mapping[str, Any]) -> bool:
    """只让 shape 已闭合到可安全查询的 selector 进入 authority adapter。"""

    expected_keys = {
        "mode",
        "resource_template_uuid",
        "mount",
        "material_uuid",
        "site",
        "slot_range",
        "flow_role",
    }
    if set(selector) != expected_keys:
        return False
    mode = selector.get("mode")
    mount = selector.get("mount")
    material_uuid = selector.get("material_uuid")
    site = selector.get("site")
    slot_range = selector.get("slot_range")
    if mode not in {"existing", "create_new"} or not _canonical_uuid(
        selector.get("resource_template_uuid")
    ):
        return False
    if (
        not isinstance(mount, Mapping)
        or set(mount) != {"uuid"}
        or not _canonical_uuid(mount.get("uuid"))
    ):
        return False
    if material_uuid is not None and not _canonical_uuid(material_uuid):
        return False
    if mode == "create_new" and material_uuid is not None:
        return False
    if site is not None and not _canonical_uuid(site):
        return False
    if site is not None and slot_range is not None:
        return False
    if slot_range is not None and (
        not isinstance(slot_range, list)
        or not slot_range
        or any(not _canonical_uuid(item) for item in slot_range)
        or len(set(slot_range)) != len(slot_range)
        or slot_range != sorted(slot_range)
    ):
        return False
    return selector.get("flow_role") in {
        "primary_sample",
        "aliquot_sample",
        "reagent",
        "consumable",
    }


def _canonical_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = UUID(value)
    except (AttributeError, ValueError):
        return False
    return parsed.int != 0 and str(parsed) == value


def _validate_selector_authority(
    selector: Mapping[str, Any],
    authority: MaterialSourceStaticAuthority,
) -> None:
    mount = selector.get("mount")
    mount_uuid = mount.get("uuid") if isinstance(mount, Mapping) else None
    template_uuid = selector.get("resource_template_uuid")
    if not isinstance(mount_uuid, str) or not isinstance(template_uuid, str):
        raise MaterialSourceAuthorityError(
            "invalid_material_source",
            "MaterialSource selector 不符合 authority 查询合同",
        )

    mount_material = _get_material(authority, mount_uuid)
    if mount_material.deleted_at is not None:
        _not_found()

    site_uuid = selector.get("site")
    slot_range = selector.get("slot_range")
    if isinstance(site_uuid, str):
        selected_sites = (_get_site(authority, site_uuid),)
    elif isinstance(slot_range, list):
        selected_sites = tuple(_get_site(authority, item) for item in slot_range)
    else:
        selected_sites = _list_sites(authority, mount_uuid)

    for site in selected_sites:
        if site.deleted_at is not None:
            _not_found()
        if site.material_uuid != mount_uuid:
            _conflict()

    compatible_sites = tuple(
        site
        for site in selected_sites
        if not site.allowed_resource_template_uuids
        or template_uuid in site.allowed_resource_template_uuids
    )
    if not compatible_sites:
        _conflict()

    fixed_material_uuid = selector.get("material_uuid")
    if fixed_material_uuid is None:
        return
    if not isinstance(fixed_material_uuid, str):
        raise MaterialSourceAuthorityError(
            "invalid_material_source",
            "MaterialSource selector 不符合 authority 查询合同",
        )
    fixed_material = _get_material(authority, fixed_material_uuid)
    if (
        fixed_material.deleted_at is not None
        or fixed_material.resource_template_uuid != template_uuid
    ):
        _conflict()
    if not any(
        site.occupied_material_uuid == fixed_material_uuid for site in compatible_sites
    ):
        _conflict()


def _get_material(
    authority: MaterialSourceStaticAuthority,
    material_uuid: str,
) -> MaterialRecord:
    try:
        material = authority.get_material(material_uuid)
    except MaterialNotFound:
        _not_found()
    except MaterialAuthorityUnavailable:
        _unavailable()
    except MaterialError:
        _conflict()
    except Exception:
        _unavailable()
    if not isinstance(material, MaterialRecord) or material.uuid != material_uuid:
        _conflict()
    return material


def _get_site(
    authority: MaterialSourceStaticAuthority,
    site_uuid: str,
) -> SiteRecord:
    try:
        site = authority.get_site(site_uuid)
    except MaterialNotFound:
        _not_found()
    except MaterialAuthorityUnavailable:
        _unavailable()
    except MaterialError:
        _conflict()
    except Exception:
        _unavailable()
    if not isinstance(site, SiteRecord) or site.uuid != site_uuid:
        _conflict()
    return site


def _list_sites(
    authority: MaterialSourceStaticAuthority,
    mount_uuid: str,
) -> tuple[SiteRecord, ...]:
    try:
        sites = authority.list_sites(mount_uuid)
    except MaterialNotFound:
        _not_found()
    except MaterialAuthorityUnavailable:
        _unavailable()
    except MaterialError:
        _conflict()
    except Exception:
        _unavailable()
    if isinstance(sites, (str, bytes)) or not isinstance(sites, Sequence):
        _conflict()
    result = tuple(sites)
    if any(not isinstance(site, SiteRecord) for site in result):
        _conflict()
    return result


def _not_found() -> Never:
    raise MaterialSourceAuthorityError(
        "not_found",
        "MaterialSource 引用的 Material 或 Site 不存在",
    )


def _conflict() -> Never:
    raise MaterialSourceAuthorityError(
        "material_source_conflict",
        "MaterialSource 与 mount/Site 静态事实冲突",
    )


def _unavailable() -> Never:
    raise MaterialSourceAuthorityError(
        "material_authority_unavailable",
        "MaterialSource 静态 authority 暂不可用",
    )


__all__ = [
    "MaterialSourceAuthorityError",
    "MaterialSourceStaticAuthority",
    "validate_material_source_authority",
]
