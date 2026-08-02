"""WorkflowTask ResourceSlot port 的 Inventory adapter。"""

from __future__ import annotations

from unilabos.app.scheduler.inventory import (
    InventoryService,
    MaterialError,
)
from unilabos.workflow.task_input import ResolvedResourceSlot, TaskInputError


class MaterialResourceSlotResolver:
    """把 Workflow concrete ResourceSlot lookup 委托给 InventoryService。"""

    def __init__(self, inventory: InventoryService) -> None:
        self._inventory = inventory

    def resolve(
        self,
        *,
        material_uuid: str,
        allowed_resource_template_uuids: tuple[str, ...] | None,
    ) -> ResolvedResourceSlot:
        """返回 Workflow port 的 immutable identity，并封闭领域错误。"""

        try:
            resolution = self._inventory.resolve_resource_slot(
                material_uuid=material_uuid,
                allowed_resource_template_uuids=allowed_resource_template_uuids,
            )
        except MaterialError as error:
            raise TaskInputError(error.code) from None
        return ResolvedResourceSlot(
            uuid=resolution.uuid,
            resource_template_uuid=resolution.resource_template_uuid,
        )


__all__ = ["MaterialResourceSlotResolver"]
