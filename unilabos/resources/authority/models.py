"""Material Authority 的持久领域记录与 closed errors。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class MaterialError(RuntimeError):
    """Material Authority public error 基类。"""

    code = "material_error"


class MaterialInvalidInput(MaterialError):
    """调用者提交了无效的 Material identity 或字段。"""

    code = "invalid_input"


class MaterialNotFound(MaterialError):
    """未找到可见的 Material。"""

    code = "not_found"


class MaterialConflict(MaterialError):
    """Material identity 或持久约束冲突。"""

    code = "conflict"


class MaterialAuthorityUnavailable(MaterialError):
    """Material durable adapter 无法完成请求。"""

    code = "material_authority_unavailable"


@dataclass(frozen=True, slots=True)
class MaterialRecord:
    """一个 Backend-field-aligned durable Material projection。"""

    uuid: str
    create_time: str
    update_time: str
    deleted_at: str | None
    description: str | None
    meta_data: dict[str, Any]
    resource_template_uuid: str
    parent_uuid: str | None
    resource_class: str
    barcode: str
    name: str
    config: dict[str, Any]
    data: dict[str, Any]
    disposition: str | None
    material_kind: str
    version: int


class MaterialAdapter(Protocol):
    """MaterialModule 所需的最小 durable adapter port。"""

    def create_business_material(
        self,
        *,
        material_uuid: str,
        resource_template_uuid: str,
        barcode: str,
        now: str,
    ) -> MaterialRecord: ...

    def get_material(self, material_uuid: str) -> MaterialRecord | None: ...
