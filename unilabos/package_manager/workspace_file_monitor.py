"""不解释文件语义的稳定工作区输入代监视器。"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .workspace_runtime import WorkspaceInputGeneration, WorkspaceRefreshResult

_IGNORED_DIRECTORIES = frozenset(
    (
        ".git",
        ".hg",
        ".svn",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "unilabos_data",
        "venv",
    )
)
_IGNORED_FILE_NAMES = frozenset((".unilabos.packages.mutation.lock",))
_IGNORED_SUFFIXES = frozenset((".pyc", ".pyo", ".swp", ".tmp"))
_IGNORED_SQLITE_SUFFIXES = ("-journal", "-shm", "-wal")


class StableWorkspaceFileMonitor:
    """把相邻文件事件收敛为完整稳定工作区输入代。"""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        graph_argument: str,
        interval_seconds: float = 0.25,
        settle_seconds: float = 0.15,
    ) -> None:
        """建立尚未启动的内容观察器。

        参数：``workspace_root`` 是唯一观察根；``graph_argument`` 原样传给完整代
        编译器；``interval_seconds`` 是轮询间隔；``settle_seconds`` 是相同摘要
        持续多久才可提交。
        返回：无；构造不读文件、不启动线程。
        异常：目录、参数或时间范围非法时抛出 ``ValueError``。
        """

        selected_root = Path(workspace_root).absolute()
        if not selected_root.is_dir() or selected_root.is_symlink():
            raise ValueError("工作区监视根必须是无符号链接的既有目录")
        if not isinstance(graph_argument, str) or not graph_argument.strip():
            raise ValueError("工作区监视器的物理图参数不能为空")
        if interval_seconds <= 0 or settle_seconds < 0:
            raise ValueError("工作区监视时间参数必须为正间隔和非负稳定期")
        self._workspace_root = selected_root
        self._graph_argument = graph_argument.strip()
        self._interval_seconds = float(interval_seconds)
        self._settle_seconds = float(settle_seconds)
        self._lifecycle_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_captured_identity: str | None = None

    def capture(self) -> WorkspaceInputGeneration:
        """同步捕获当前完整工作区文件输入代。

        参数：无。
        返回：以全部非临时普通文件内容摘要为身份的不可变输入代；不解析 Python、
        YAML、JSON、动作合同（Action Contract）或工作流（Workflow）语义。
        异常：文件遍历中出现符号链接、越界、读取竞争或 I/O 故障时传播异常。
        """

        # ``generation_identity`` 只表达字节级文件世代，不承担包内容解释责任。
        generation_identity = _workspace_content_identity(self._workspace_root)
        self._last_captured_identity = generation_identity
        return WorkspaceInputGeneration(
            identity=generation_identity,
            workspace_root=self._workspace_root,
            graph_argument=self._graph_argument,
        )

    def start(
        self,
        submit: Callable[[WorkspaceInputGeneration], WorkspaceRefreshResult],
    ) -> None:
        """幂等启动稳定文件世代后台观察。

        参数：``submit`` 接收完整稳定输入代并返回刷新结算结果。
        返回：无；已经运行时不创建第二线程。
        异常：回调不可调用、初始捕获或线程启动失败时传播异常。
        """

        if not callable(submit):
            raise TypeError("稳定工作区输入代提交回调必须可调用")
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            # ``processed_identity`` 是启动前已由产品编译的文件代；若调用者没有
            # 先 capture，则在开启线程前同步建立唯一基线。
            processed_identity = self._last_captured_identity or self.capture().identity
            stop_event = threading.Event()
            worker = threading.Thread(
                target=self._run,
                args=(stop_event, submit, processed_identity),
                name="workspace-file-generation-monitor",
                daemon=True,
            )
            self._stop_event = stop_event
            self._thread = worker
            try:
                worker.start()
            except BaseException:
                self._thread = None
                raise

    def close(self) -> None:
        """幂等停止监视线程并等待有界退出。

        参数：无。
        返回：无；成功后不再提交新输入代。
        异常：线程五秒内无法退出时抛出 ``RuntimeError``。
        """

        with self._lifecycle_lock:
            worker = self._thread
            if worker is None:
                return
            self._stop_event.set()
        if worker is not threading.current_thread():
            worker.join(timeout=5)
        with self._lifecycle_lock:
            if worker.is_alive():
                raise RuntimeError("稳定工作区文件监视器未能停止")
            if self._thread is worker:
                self._thread = None

    def _run(
        self,
        stop_event: threading.Event,
        submit: Callable[[WorkspaceInputGeneration], WorkspaceRefreshResult],
        processed_identity: str,
    ) -> None:
        """轮询文件摘要并只提交经过稳定期的完整输入代。

        参数：``stop_event`` 属于本线程生命周期；``submit`` 是协调器命令；
        ``processed_identity`` 是启动前已编译并发布的输入身份。
        返回：无；文件竞争和刷新失败保留同一代继续重试，停止事件结束循环。
        异常：未分类回调错误留在后台线程边界，不会把不稳定代标记为已处理。
        """

        pending_identity: str | None = None
        pending_since = 0.0
        while not stop_event.wait(self._interval_seconds):
            try:
                observed = self.capture()
            except (OSError, RuntimeError, ValueError):
                pending_identity = None
                continue
            if observed.identity == processed_identity:
                pending_identity = None
                continue
            now = time.monotonic()
            if observed.identity != pending_identity:
                pending_identity = observed.identity
                pending_since = now
                continue
            if now - pending_since < self._settle_seconds:
                continue
            try:
                # ``refresh_result`` 决定该稳定代是否已经由运行时可靠结算。
                refresh_result = submit(observed)
            except (OSError, RuntimeError, TypeError, ValueError):
                pending_since = now
                continue
            if getattr(refresh_result, "outcome", None) == "failed":
                pending_since = now
                continue
            processed_identity = observed.identity
            pending_identity = None


def _workspace_content_identity(workspace_root: Path) -> str:
    """按规范相对路径和原始字节计算工作区文件代身份。

    参数：``workspace_root`` 是已经过构造校验的观察根。
    返回：包含全部相关普通文件的 ``sha256:`` 摘要。
    异常：发现符号链接、路径逃逸或读取竞争时抛出 ``ValueError``/``OSError``，
    不产生看似完整的部分摘要。
    """

    digest = hashlib.sha256()
    # ``observed_files`` 只按路径枚举，不解释扩展名或包内领域语义。
    observed_files: list[Path] = []
    for candidate in workspace_root.rglob("*"):
        relative = candidate.relative_to(workspace_root)
        if any(part in _IGNORED_DIRECTORIES for part in relative.parts):
            continue
        if candidate.is_symlink():
            raise ValueError(f"工作区监视范围不得包含符号链接: {relative.as_posix()}")
        if candidate.is_file() and not _ignore_file(candidate):
            observed_files.append(candidate)
    for candidate in sorted(
        observed_files,
        key=lambda item: item.relative_to(workspace_root).as_posix(),
    ):
        relative_bytes = (
            candidate.relative_to(workspace_root).as_posix().encode("utf-8")
        )
        content = candidate.read_bytes()
        digest.update(len(relative_bytes).to_bytes(4, "big"))
        digest.update(relative_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return "sha256:" + digest.hexdigest()


def _ignore_file(candidate: Path) -> bool:
    """判断一个普通文件是否属于编辑器或解释器临时产物。

    参数：``candidate`` 是工作区根下的既有普通文件。
    返回：文件名或后缀属于固定基础设施忽略集合时为 ``True``。
    异常：无；本函数不读取文件内容。
    """

    return (
        candidate.name in _IGNORED_FILE_NAMES
        or candidate.suffix in _IGNORED_SUFFIXES
        or candidate.name.endswith(_IGNORED_SQLITE_SUFFIXES)
    )


__all__ = ["StableWorkspaceFileMonitor"]
