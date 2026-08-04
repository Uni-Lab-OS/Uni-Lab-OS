"""Round 02G2 窄 Win32 文件 API Adapter 的平台无关合同。"""

from __future__ import annotations

import ctypes
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from unilabos.workflow import windows_file_api


class RecordingFunction:
    """记录一个 ctypes 函数的签名、参数与预设返回值。"""

    def __init__(self, results: list[int]) -> None:
        """保存按调用顺序返回的 `results`；构造函数没有返回值。"""

        self.results = results
        self.calls: list[tuple[Any, ...]] = []
        self.argtypes: list[Any] | None = None
        self.restype: Any = None

    def __call__(self, *args: Any) -> int:
        """记录 `args` 并返回下一个预设结果。"""

        self.calls.append(args)
        return self.results.pop(0)


def _fake_ctypes(
    create_file: RecordingFunction,
    close_handle: RecordingFunction,
) -> SimpleNamespace:
    """返回只暴露目录 guard 所需 ctypes 字段的假模块。"""

    kernel32 = SimpleNamespace(
        CreateFileW=create_file,
        CloseHandle=close_handle,
    )

    def load_kernel32(name: str, *, use_last_error: bool) -> SimpleNamespace:
        """验证 loader 参数并返回单个假 kernel32。"""

        assert (name, use_last_error) == ("kernel32", True)
        return kernel32

    def win_error(error_number: int) -> OSError:
        """把 `error_number` 转成带 WinError 的系统异常。"""

        error = OSError(error_number, f"WinError {error_number}")
        error.winerror = error_number
        return error

    def get_last_error() -> int:
        """返回无错误的 Win32 状态。"""

        return 0

    return SimpleNamespace(
        c_int=ctypes.c_int,
        c_uint32=ctypes.c_uint32,
        c_void_p=ctypes.c_void_p,
        c_wchar_p=ctypes.c_wchar_p,
        WinDLL=load_kernel32,
        WinError=win_error,
        get_last_error=get_last_error,
    )


def test_directory_guard_opens_without_delete_share_and_closes_in_reverse(
    tmp_path: Path,
) -> None:
    """目录链句柄必须拒绝 delete share，并按逆序释放。

    `tmp_path` 提供路径字面值；测试没有返回值。安全不变量是 guard 存续期间
    每一级目录都不能被另一个进程重命名或替换为 reparse point。
    """

    root = tmp_path / "registered"
    parent = root / "workflows"
    create_file = RecordingFunction([101, 202])
    close_handle = RecordingFunction([1, 1])
    ctypes_api = _fake_ctypes(create_file, close_handle)

    with windows_file_api.hold_directory_chain(
        (root, parent),
        ctypes_api=ctypes_api,
    ):
        assert close_handle.calls == []

    assert [call[0] for call in create_file.calls] == [
        windows_file_api.extended_path(root),
        windows_file_api.extended_path(parent),
    ]
    assert [call[2] for call in create_file.calls] == [
        windows_file_api.FILE_SHARE_READ | windows_file_api.FILE_SHARE_WRITE,
    ] * 2
    assert [call[5] for call in create_file.calls] == [
        windows_file_api.FILE_FLAG_BACKUP_SEMANTICS
        | windows_file_api.FILE_FLAG_OPEN_REPARSE_POINT,
    ] * 2
    assert close_handle.calls == [(202,), (101,)]
