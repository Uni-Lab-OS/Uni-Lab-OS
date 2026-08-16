"""工作区工作流源码（Workflow Source）的固定点激活协调。"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WorkspaceActivationCoordinator:
    """隔离跨来源固定点循环，调用方保留编译、持久化与错误语义。"""

    recover_sources: Callable[[], None]
    list_registrations: Callable[[], Sequence[Mapping[str, Any]]]
    reconcile_source: Callable[[str], None]
    load_authoring_record: Callable[[str], Mapping[str, Any]]
    apply_candidate: Callable[[str, str], Mapping[str, Any]]
    require_apply_complete: Callable[[Mapping[str, Any]], None]
    record_isolated_failure: Callable[[str, Exception], None]
    public_error_type: type[Exception]
    public_error_code: Callable[[Exception], str]
    isolated_error_codes: Collection[str]
    error_factory: Callable[[str], Exception]

    def activate_to_fixed_point(self) -> None:
        """逐轮重编译并应用候选，直到一轮内没有来源成功推进。"""

        self.recover_sources()
        registrations = self.list_registrations()
        while True:
            applied_in_pass = False
            for registration in registrations:
                workflow_uuid = str(registration["workflow_uuid"])
                self._reconcile(workflow_uuid)
                candidate_hash = self._candidate_hash(workflow_uuid)
                if candidate_hash is None:
                    continue
                try:
                    result = self.apply_candidate(workflow_uuid, candidate_hash)
                    self.require_apply_complete(result)
                except self.public_error_type as error:
                    if self.public_error_code(error) not in self.isolated_error_codes:
                        raise
                    self.record_isolated_failure(workflow_uuid, error)
                    continue
                applied_in_pass = True
            if not applied_in_pass:
                return

    def _reconcile(self, workflow_uuid: str) -> None:
        try:
            self.reconcile_source(workflow_uuid)
        except self.public_error_type:
            raise
        except Exception as error:  # noqa: BLE001 - 组合根必须失败关闭
            raise self.error_factory("template_catalog_unavailable") from error

    def _candidate_hash(self, workflow_uuid: str) -> str | None:
        candidate = self.load_authoring_record(workflow_uuid).get("candidate")
        if not isinstance(candidate, Mapping):
            return None
        candidate_hash = candidate.get("candidate_hash")
        if not isinstance(candidate_hash, str) or not candidate_hash:
            raise self.error_factory("candidate_invalid")
        return candidate_hash
