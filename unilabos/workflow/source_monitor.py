"""Authoring Draft 文件的进程级轮询监视器。"""

from __future__ import annotations

import threading
from typing import Optional

from unilabos.workflow.service import WorkflowError, WorkflowService


class WorkflowSourceMonitor:
    """轮询全部已注册源码，并让 Service 负责哈希去重与编译。"""

    def __init__(
        self,
        service: WorkflowService,
        *,
        interval_seconds: float = 0.05,
    ):
        self._service = service
        self._interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
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
            thread.join(timeout=2)
        self._thread = None

    def _run(self) -> None:
        while not self._stop_event.is_set():
            for registration in self._service.store.list_source_registrations():
                if self._stop_event.is_set():
                    return
                try:
                    self._service.reconcile_registered_source(
                        registration["workflow_uuid"]
                    )
                except (OSError, RuntimeError, WorkflowError):
                    # 单个无效 Draft 不得终止其他工作流的监视。
                    continue
            self._stop_event.wait(self._interval_seconds)


__all__ = ["WorkflowSourceMonitor"]
