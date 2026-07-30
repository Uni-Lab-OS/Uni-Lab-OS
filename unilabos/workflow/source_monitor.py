"""Authoring Draft 文件的进程级轮询监视器。"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional, Tuple

from unilabos.workflow.service import WorkflowError, WorkflowService


class WorkflowSourceMonitor:
    """轮询全部已注册源码，并让 Service 负责哈希去重与编译。"""

    def __init__(
        self,
        service: WorkflowService,
        *,
        interval_seconds: float = 0.05,
        settle_seconds: float = 0.05,
    ):
        self._service = service
        self._interval_seconds = interval_seconds
        self._settle_seconds = settle_seconds
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._processed: Dict[str, Tuple[Any, ...]] = {}
        self._pending: Dict[str, Tuple[Tuple[Any, ...], float]] = {}

    def start(self) -> None:
        if self._thread is not None:
            return
        for registration in self._service.store.list_source_registrations():
            try:
                self._processed[registration["workflow_uuid"]] = (
                    self._service.source_signature(registration)
                )
            except (OSError, RuntimeError, WorkflowError):
                continue
        self._thread = threading.Thread(
            target=self._run,
            name="workflow-source-monitor",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
            if thread.is_alive():
                raise RuntimeError("Workflow Draft 监视器未能停止")
        self._thread = None

    def _run(self) -> None:
        while not self._stop_event.is_set():
            registrations = self._service.store.list_source_registrations()
            active = {
                registration["workflow_uuid"]
                for registration in registrations
            }
            for workflow_uuid in set(self._processed) - active:
                self._processed.pop(workflow_uuid, None)
                self._pending.pop(workflow_uuid, None)
            for registration in registrations:
                if self._stop_event.is_set():
                    return
                workflow_uuid = registration["workflow_uuid"]
                signature: Optional[Tuple[Any, ...]] = None
                try:
                    signature = self._service.source_signature(registration)
                    if self._processed.get(workflow_uuid) == signature:
                        self._pending.pop(workflow_uuid, None)
                        continue
                    now = time.monotonic()
                    pending = self._pending.get(workflow_uuid)
                    if pending is None or pending[0] != signature:
                        self._pending[workflow_uuid] = (signature, now)
                        continue
                    if now - pending[1] < self._settle_seconds:
                        continue
                    self._service.reconcile_registered_source(
                        workflow_uuid
                    )
                    latest_signature = self._service.source_signature(
                        registration
                    )
                    if latest_signature == signature:
                        self._processed[workflow_uuid] = signature
                        self._pending.pop(workflow_uuid, None)
                    else:
                        self._pending[workflow_uuid] = (
                            latest_signature,
                            time.monotonic(),
                        )
                except (OSError, RuntimeError, WorkflowError):
                    # 单个无效 Draft 不得终止其他工作流的监视。
                    if signature is not None:
                        self._processed[workflow_uuid] = signature
                        self._pending.pop(workflow_uuid, None)
                    continue
            self._stop_event.wait(self._interval_seconds)


__all__ = ["WorkflowSourceMonitor"]
