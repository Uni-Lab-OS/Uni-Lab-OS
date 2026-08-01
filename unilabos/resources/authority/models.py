"""Material Authority 的持久领域记录与 closed errors。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
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
class ResourceTemplateIdentity:
    """Registry/PackageCatalog 提供给 Material Authority 的最小 identity。"""

    uuid: str
    material_class: str


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
    klass: str
    barcode: str
    name: str
    config: dict[str, Any]
    data: dict[str, Any]
    disposition: str | None
    material_kind: str
    version: int

    def to_dict(self) -> dict[str, Any]:
        """投影为 Backend exact-baseline 的结构化 Material 字段。"""

        return {
            "uuid": self.uuid,
            "create_time": self.create_time,
            "update_time": self.update_time,
            "deleted_at": self.deleted_at,
            "description": self.description,
            "meta_data": dict(self.meta_data),
            "resource_template_uuid": self.resource_template_uuid,
            "parent_uuid": self.parent_uuid,
            "class": self.klass,
            "barcode": self.barcode,
            "name": self.name,
            "config": dict(self.config),
            "data": dict(self.data),
            "disposition": self.disposition,
            "material_kind": self.material_kind,
            "version": self.version,
        }


class RuntimeAuthorityUnitOfWork(Protocol):
    """调用者已经打开的 runtime-authority transaction capability。"""

    def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> Any: ...

    def create_collation(
        self,
        name: str,
        comparison: Callable[[str, str], int],
    ) -> None: ...


class RuntimeAuthorityCoordinator(Protocol):
    """统一 runtime-authority transaction coordinator port。"""

    def transaction(
        self,
    ) -> AbstractContextManager[RuntimeAuthorityUnitOfWork]: ...


class MaterialAdapter(Protocol):
    """MaterialModule 所需的最小 durable adapter port。"""

    def create_business_material(
        self,
        *,
        material_uuid: str,
        resource_template_uuid: str,
        resource_class: str,
        barcode: str,
        name: str,
        description: str | None,
        meta_data: dict[str, Any],
        config: dict[str, Any],
        data: dict[str, Any],
        now: str,
        uow: RuntimeAuthorityUnitOfWork | None = None,
    ) -> MaterialRecord: ...

    def get_material(
        self,
        material_uuid: str,
        *,
        uow: RuntimeAuthorityUnitOfWork | None = None,
    ) -> MaterialRecord | None: ...
