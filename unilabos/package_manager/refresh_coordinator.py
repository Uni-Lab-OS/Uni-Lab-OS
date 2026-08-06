"""稳定工作区输入代监视器与包运行时之间的刷新协调器。"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Protocol

from .workspace_runtime import (
    WorkspaceInputGeneration,
    WorkspacePackageRuntime,
    WorkspaceRefreshResult,
    WorkspaceRuntimeStatus,
)


class StableWorkspaceGenerationMonitor(Protocol):
    """只观察稳定文件世代、不解释包内容的监视器 Adapter 接口。"""

    def start(
        self,
        submit: Callable[[WorkspaceInputGeneration], WorkspaceRefreshResult],
    ) -> None:
        """启动稳定输入代观察。

        参数：``submit`` 是完整输入代稳定后调用的唯一命令接缝。
        返回：无。
        异常：监视线程无法启动时传播异常；不得提交零散文件事件。
        """

        ...

    def close(self) -> None:
        """停止监视并等待提交线程退出。

        参数：无。
        返回：无。
        异常：无法确认停止时抛出异常，不能伪装为已经关闭。
        """

        ...


class WorkspaceRefreshCoordinator:
    """串行连接稳定输入监视器与工作区包运行时深模块。"""

    def __init__(
        self,
        runtime: WorkspacePackageRuntime,
        monitor: StableWorkspaceGenerationMonitor,
    ) -> None:
        """建立尚未启动的工作区刷新协调器。

        参数：``runtime`` 负责解释、门禁和发布完整输入代；``monitor`` 只负责
        稳定观察和提交输入代。
        返回：无；构造不启动线程、不编译文件。
        异常：依赖缺少所需公开方法时抛出 ``TypeError``。
        """

        if not isinstance(runtime, WorkspacePackageRuntime):
            raise TypeError("runtime 必须是 WorkspacePackageRuntime")
        if not callable(getattr(monitor, "start", None)) or not callable(
            getattr(monitor, "close", None)
        ):
            raise TypeError("monitor 必须实现稳定输入代监视接口")
        self._runtime = runtime
        self._monitor = monitor
        self._lock = threading.RLock()
        self._started = False
        self._closed = False

    def start(self) -> WorkspaceRuntimeStatus:
        """先启动包运行时，再幂等开启稳定输入代监视。

        参数：无。
        返回：工作区包运行时当前状态。
        异常：关闭后启动抛出 ``RuntimeError``；初始发布或监视器启动失败传播原
        异常，且不会创建第二个监视器生命周期。
        """

        with self._lock:
            if self._closed:
                raise RuntimeError("工作区刷新协调器已经关闭")
            if self._started:
                return self._runtime.status()
            status = self._runtime.start()
            self._monitor.start(self.submit)
            self._started = True
            return status

    def submit(
        self,
        generation: WorkspaceInputGeneration,
    ) -> WorkspaceRefreshResult:
        """串行转交一个监视器已经稳定观察的完整输入代。

        参数：``generation`` 是不可变稳定工作区输入代。
        返回：工作区包运行时的刷新结果。
        异常：协调器未启动、已关闭或输入类型无效时抛出
        ``RuntimeError``/``TypeError``；本方法不自行读取或解释文件。
        """

        if not isinstance(generation, WorkspaceInputGeneration):
            raise TypeError("generation 必须是 WorkspaceInputGeneration")
        with self._lock:
            if self._closed or not self._started:
                raise RuntimeError("工作区刷新协调器未运行")
            return self._runtime.refresh(generation)

    def status(self) -> WorkspaceRuntimeStatus:
        """读取工作区包运行时的稳定状态。

        参数：无。
        返回：运行时不可变状态投影。
        异常：无；协调器不另建第二份状态权威。
        """

        return self._runtime.status()

    def close(self) -> None:
        """幂等停止监视器并关闭刷新命令接缝。

        参数：无。
        返回：无；监视器先停止，运行时随后拒绝刷新。
        异常：监视器关闭故障在运行时完成关闭后传播，避免继续接收新命令。
        """

        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                if self._started:
                    self._monitor.close()
            finally:
                self._runtime.close()
                self._started = False


__all__ = [
    "StableWorkspaceGenerationMonitor",
    "WorkspaceRefreshCoordinator",
]
