"""Workflow 模块的 Windows import-time 可移植性回归。"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
from pathlib import Path

import pytest

from unilabos.workflow import composition


class RecordingMsvcrt:
    LK_NBLCK = 1
    LK_UNLCK = 2

    def __init__(self) -> None:
        self.calls: list[tuple[int, int, int]] = []
        self.locked = False

    def locking(self, descriptor: int, mode: int, size: int) -> None:
        self.calls.append((descriptor, mode, size))
        if mode == self.LK_NBLCK:
            if self.locked:
                raise OSError(errno.EACCES, "lock already held")
            self.locked = True
        elif mode == self.LK_UNLCK:
            self.locked = False


def test_workflow_import_chain_does_not_require_posix_fcntl() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    probe = """
import builtins
import os
import signal

original_import = builtins.__import__

def windows_import(name, *args, **kwargs):
    if name == "fcntl":
        raise ModuleNotFoundError("No module named 'fcntl'")
    return original_import(name, *args, **kwargs)

builtins.__import__ = windows_import
for signal_name in ("SIGIO", "SIGRTMAX"):
    if hasattr(signal, signal_name):
        delattr(signal, signal_name)
for flag_name in ("O_DIRECTORY", "O_CLOEXEC", "O_NOFOLLOW", "O_NONBLOCK"):
    if hasattr(os, flag_name):
        delattr(os, flag_name)

import unilabos.workflow.service
import unilabos.workflow.composition
import unilabos.workflow.source_discovery
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=repository_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_windows_workspace_authority_uses_msvcrt_byte_range_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    windows_lock = RecordingMsvcrt()
    monkeypatch.setattr(composition, "fcntl", None)
    monkeypatch.setattr(composition, "msvcrt", windows_lock)

    descriptor = composition._acquire_workspace_lease(tmp_path)
    try:
        assert os.fstat(descriptor).st_size == 1
        assert windows_lock.calls == [
            (descriptor, windows_lock.LK_NBLCK, 1),
        ]
    finally:
        composition._release_workspace_lease(descriptor)

    assert windows_lock.calls == [
        (descriptor, windows_lock.LK_NBLCK, 1),
        (descriptor, windows_lock.LK_UNLCK, 1),
    ]


def test_windows_workspace_authority_rejects_a_second_process_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    windows_lock = RecordingMsvcrt()
    monkeypatch.setattr(composition, "fcntl", None)
    monkeypatch.setattr(composition, "msvcrt", windows_lock)

    descriptor = composition._acquire_workspace_lease(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="已由另一个 OS Workflow Authority 占用"):
            composition._acquire_workspace_lease(tmp_path)
    finally:
        composition._release_workspace_lease(descriptor)
