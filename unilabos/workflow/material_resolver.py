"""WorkflowTask ResourceSlot port 的 Material Authority adapter。"""

from __future__ import annotations

from unilabos.resources.authority import (
    MaterialError,
    MaterialModule,
    MaterialReservationOutcome,
)
from unilabos.resources.authority.models import RuntimeAuthorityUnitOfWork
from unilabos.workflow.task_input import ResolvedResourceSlot, TaskInputError


class MaterialResourceSlotResolver:
    """把 Workflow concrete ResourceSlot lookup 委托给 Material Authority。"""

    def __init__(self, materials: MaterialModule) -> None:
        self._materials = materials

    def resolve(
        self,
        *,
        material_uuid: str,
        allowed_resource_template_uuids: tuple[str, ...] | None,
    ) -> ResolvedResourceSlot:
        """返回 Workflow port 的 immutable identity，并封闭领域错误。"""

        try:
            resolution = self._materials.resolve_resource_slot(
                material_uuid=material_uuid,
                allowed_resource_template_uuids=allowed_resource_template_uuids,
            )
        except MaterialError as error:
            raise TaskInputError(error.code) from None
        return ResolvedResourceSlot(
            uuid=resolution.uuid,
            resource_template_uuid=resolution.resource_template_uuid,
        )

    def reserve_task_materials(
        self,
        uow: RuntimeAuthorityUnitOfWork,
        *,
        task_uuid: str,
        root_material_uuids: tuple[str, ...],
    ) -> MaterialReservationOutcome:
        """把 Task transaction 原样借给 Material Authority。"""

        try:
            return self._materials.reserve_task_materials(
                uow,
                task_uuid=task_uuid,
                root_material_uuids=root_material_uuids,
            )
        except MaterialError as error:
            raise TaskInputError(error.code) from None

    def has_complete_task_reservation(
        self,
        uow: RuntimeAuthorityUnitOfWork,
        *,
        task_uuid: str,
        root_material_uuids: tuple[str, ...],
    ) -> bool:
        """通过 public Material seam 执行 dispatch 前完整性检查。"""

        try:
            return self._materials.has_complete_task_reservation(
                uow,
                task_uuid=task_uuid,
                root_material_uuids=root_material_uuids,
            )
        except MaterialError as error:
            raise TaskInputError(error.code) from None


__all__ = ["MaterialResourceSlotResolver"]
