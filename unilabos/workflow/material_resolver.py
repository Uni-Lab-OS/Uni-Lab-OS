"""WorkflowTask ResourceSlot port 的 Material Authority adapter。"""

from __future__ import annotations

from unilabos.resources.authority import MaterialError, MaterialModule
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


__all__ = ["MaterialResourceSlotResolver"]
