"""Authoring Draft 文件的进程级轮询监视器。"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Protocol, Tuple

from unilabos.workflow.service import WorkflowError


class SourceChangeService(Protocol):
    """声明源码监视器（Source Monitor）唯一依赖的命令接口。"""

    def list_registered_sources(self) -> List[Dict[str, Any]]:
        """返回全部规范来源注册。

        参数：无。返回：每项含稳定工作流 UUID 的来源注册列表。
        """

        ...

    def source_signature(
        self,
        registration: Dict[str, Any],
    ) -> Tuple[Any, ...]:
        """读取一个来源注册当前的轻量文件签名。

        参数：``registration`` 是服务发布的规范来源注册。
        返回：仅用于去抖和文件世代比较的稳定元组。
        """

        ...

    def submit_source_change(
        self,
        workflow_uuid: str,
        *,
        observed_signature: Tuple[Any, ...],
    ) -> bool:
        """提交一个已经稳定观测的源码变化命令。

        参数：``workflow_uuid`` 是规范工作流身份；``observed_signature`` 是
        本次观测的文件世代。返回：``True`` 表示已结算，``False`` 表示服务要求
        保留并重试；异常表示本次提交未被确认。
        """

        ...


class WorkflowSourceMonitor:
    """轮询全部已注册源码，并让 Service 负责哈希去重与编译。"""

    def __init__(
        self,
        service: SourceChangeService,
        *,
        interval_seconds: float = 0.25,
        settle_seconds: float = 0.1,
    ) -> None:
        """建立进程级轮询器，但不自动启动后台线程。

        参数：``service`` 是唯一源码变化命令权威接口；``interval_seconds`` 是
        轮询间隔；``settle_seconds`` 是文件世代保持不变后才可提交的去抖时间。
        返回：无；待处理、已结算和退避记录都只用于进程内轮询恢复。
        """

        self._service = service
        self._interval_seconds = interval_seconds
        self._settle_seconds = settle_seconds
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._processed: Dict[str, Tuple[Any, ...]] = {}
        self._pending: Dict[str, Tuple[Tuple[Any, ...], float]] = {}
        self._retries: Dict[
            str,
            Tuple[Tuple[Any, ...], float, float],
        ] = {}

    def start(self) -> None:
        """幂等启动唯一源码监视后台线程。

        参数：无。返回：无；已经启动时不创建第二个线程。
        """

        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="workflow-source-monitor",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """停止源码监视线程并等待其有界退出。

        参数：无。返回：无；线程未在五秒内退出时抛出 ``RuntimeError``，避免
        组合根误认为监视权威已经停止。
        """

        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
            if thread.is_alive():
                raise RuntimeError("Workflow Draft 监视器未能停止")
        self._thread = None

    def _run(self) -> None:
        """轮询注册来源并只向服务提交稳定源码变化命令。

        参数：无。返回：无；线程在停止事件设置后退出。确定性输入错误会等待
        新文件世代，瞬态失败和未结算结果保留同一命令并按上限退避重试。
        """

        while not self._stop_event.is_set():
            registrations = self._service.list_registered_sources()
            # ``active`` 是本轮仍由服务确认存在的规范工作流来源身份集合。
            active = {registration["workflow_uuid"] for registration in registrations}
            known = set(self._processed) | set(self._pending) | set(self._retries)
            for workflow_uuid in known - active:
                self._processed.pop(workflow_uuid, None)
                self._pending.pop(workflow_uuid, None)
                self._retries.pop(workflow_uuid, None)
            for registration in registrations:
                if self._stop_event.is_set():
                    return
                workflow_uuid = registration["workflow_uuid"]
                signature: Optional[Tuple[Any, ...]] = None
                try:
                    # ``signature`` 只标识文件观测世代；源码身份仍由注册记录决定。
                    signature = self._service.source_signature(registration)
                    if self._processed.get(workflow_uuid) == signature:
                        self._pending.pop(workflow_uuid, None)
                        self._retries.pop(workflow_uuid, None)
                        continue
                    now = time.monotonic()
                    pending = self._pending.get(workflow_uuid)
                    if pending is None or pending[0] != signature:
                        self._pending[workflow_uuid] = (signature, now)
                        self._retries.pop(workflow_uuid, None)
                        continue
                    if now - pending[1] < self._settle_seconds:
                        continue
                    retry = self._retries.get(workflow_uuid)
                    if retry is not None and retry[0] == signature and now < retry[1]:
                        continue
                    settled = self._service.submit_source_change(
                        workflow_uuid,
                        observed_signature=signature,
                    )
                    latest_signature = self._service.source_signature(registration)
                    if not settled:
                        self._processed.pop(workflow_uuid, None)
                        self._pending[workflow_uuid] = (
                            latest_signature,
                            time.monotonic(),
                        )
                        self._schedule_retry(
                            workflow_uuid,
                            latest_signature,
                        )
                    elif latest_signature == signature:
                        self._processed[workflow_uuid] = signature
                        self._pending.pop(workflow_uuid, None)
                        self._retries.pop(workflow_uuid, None)
                    else:
                        self._pending[workflow_uuid] = (
                            latest_signature,
                            time.monotonic(),
                        )
                        self._retries.pop(workflow_uuid, None)
                except WorkflowError as exc:
                    # 文件内容错误只在签名变化后重试；编译器和目录等
                    # 暂态故障则使用有上限的指数退避。
                    if signature is not None and exc.code in {
                        "invalid_input",
                        "workflow_not_found",
                    }:
                        self._processed[workflow_uuid] = signature
                        self._pending.pop(workflow_uuid, None)
                        self._retries.pop(workflow_uuid, None)
                    elif signature is not None:
                        self._schedule_retry(workflow_uuid, signature)
                    continue
                except (OSError, RuntimeError):
                    if signature is not None:
                        self._schedule_retry(workflow_uuid, signature)
                    continue
            self._stop_event.wait(self._interval_seconds)

    def _schedule_retry(
        self,
        workflow_uuid: str,
        signature: Tuple[Any, ...],
    ) -> None:
        """为未确认命令安排有上限的指数退避。

        参数：``workflow_uuid`` 是规范工作流身份；``signature`` 是必须保留的
        文件观测世代。返回：无；相同世代逐次退避，变化后的新世代从最小值开始。
        """

        previous = self._retries.get(workflow_uuid)
        minimum = max(0.02, self._interval_seconds * 4)
        delay = minimum
        if previous is not None and previous[0] == signature:
            delay = min(previous[2] * 2, 1.0)
        self._retries[workflow_uuid] = (
            signature,
            time.monotonic() + delay,
            delay,
        )


__all__ = ["SourceChangeService", "WorkflowSourceMonitor"]
