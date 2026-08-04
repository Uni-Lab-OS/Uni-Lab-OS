"""Windows 工作流草稿（Workflow Draft）原子发布安全合同。"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

from unilabos.workflow import source_publication


class RecordingWindowsLock:
    """记录 Windows 字节锁生命周期并暴露最后一个目标描述符。"""

    LK_NBLCK = 1
    LK_UNLCK = 2

    def __init__(self) -> None:
        """初始化锁状态；参数无，返回无，不接触真实平台锁。"""

        self.calls: list[tuple[int, int, int]] = []
        self.locked_descriptors: set[int] = set()
        self.last_descriptor: int | None = None

    def locking(self, descriptor: int, mode: int, length: int) -> None:
        """记录锁定或解锁并维护当前锁集合。

        参数：``descriptor`` 是原草稿文件，``mode`` 是锁操作，``length`` 是
        字节区间。返回：无；未知操作使测试立即失败。
        """

        self.calls.append((descriptor, mode, length))
        self.last_descriptor = descriptor
        if mode == self.LK_NBLCK:
            self.locked_descriptors.add(descriptor)
            return
        if mode == self.LK_UNLCK:
            self.locked_descriptors.discard(descriptor)
            return
        raise AssertionError(f"unexpected lock mode: {mode}")


def _draft_hash(content: bytes) -> str:
    """返回 ``content`` 的工作流草稿（Workflow Draft）SHA-256 身份。"""

    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def test_windows_existing_draft_closes_lock_before_native_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已有 Windows 草稿必须解锁并关闭原文件后才调用原生替换。

    参数：``tmp_path`` 提供同目录原稿和临时文件；``monkeypatch`` 模拟 Windows
    锁与 ``ReplaceFileW``。返回：无；安全不变量是不得在目标 FD 打开/锁定时调用
    ``os.replace``，且替换瞬间的旧字节必须进入备份供 CAS 再验证。
    """

    parent = tmp_path / "workflows"
    parent.mkdir()
    target = parent / "demo.py"
    original = b"value = 'initial'\n"
    replacement = b"value = 'changed'\n"
    target.write_bytes(original)
    windows_lock = RecordingWindowsLock()
    original_replace = os.replace
    native_calls: list[tuple[Path, Path, Path]] = []

    def native_replace(target_path: Path, replacement_path: Path, backup: Path) -> None:
        """模拟一次 ``ReplaceFileW`` 并验证调用前的锁和描述符状态。

        参数：三个路径依次是规范草稿、替换稿和旧稿备份。返回：无；模拟器使用
        捕获的宿主替换原语实现同目录发布。
        """

        native_calls.append((target_path, replacement_path, backup))
        assert windows_lock.locked_descriptors == set()
        assert windows_lock.last_descriptor is not None
        with pytest.raises(OSError):
            os.fstat(windows_lock.last_descriptor)
        original_replace(target_path, backup)
        original_replace(replacement_path, target_path)

    def forbidden_portable_replace(*_args: object, **_kwargs: object) -> None:
        """拒绝 Windows 既有草稿路径退回 ``os.replace``；参数只用于捕获误用。"""

        raise AssertionError("Windows existing draft used os.replace")

    monkeypatch.setattr(source_publication, "_PLATFORM", "win32")
    monkeypatch.setattr(source_publication, "_fcntl", None)
    monkeypatch.setattr(source_publication, "_msvcrt", windows_lock)
    monkeypatch.setattr(
        source_publication,
        "replace_windows_file_with_backup",
        native_replace,
        raising=False,
    )
    monkeypatch.setattr(source_publication.os, "replace", forbidden_portable_replace)

    source_publication.atomic_publish_source(
        parent_path=parent,
        target_name=target.name,
        content=replacement,
        byte_limit=1024,
        expected_hash=_draft_hash(original),
    )

    assert target.read_bytes() == replacement
    assert len(native_calls) == 1
    assert [path.name for path in parent.iterdir()] == [target.name]


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="真实 Windows 文件共享和 ReplaceFileW 合同只在 Windows CI 运行",
)
def test_native_windows_replaces_existing_draft(tmp_path: Path) -> None:
    """真实 Windows 必须完成已有工作流草稿（Workflow Draft）的 CAS 发布。

    参数：``tmp_path`` 是 Windows CI 的本地临时目录。返回：无；发布后只保留
    完整规范草稿，不遗留临时文件或旧稿备份。
    """

    parent = tmp_path / "workflows"
    parent.mkdir()
    target = parent / "demo.py"
    original = b"value = 'initial'\n"
    replacement = b"value = 'changed'\n"
    target.write_bytes(original)

    source_publication.atomic_publish_source(
        parent_path=parent,
        target_name=target.name,
        content=replacement,
        byte_limit=1024,
        expected_hash=_draft_hash(original),
    )

    assert target.read_bytes() == replacement
    assert [path.name for path in parent.iterdir()] == [target.name]
