"""Round 02G2 Windows Draft CAS reviewer blocker 的独立 RED 测试。"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow import windows_draft_cas

INITIAL_SOURCE = b"seed()\n"
REQUEST_SOURCE = b"build()\n"
EXTERNAL_SOURCE = b"external_authority()\n"
DRAFT_BYTE_LIMIT = 1024 * 1024
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class AcceptingWindowsLocking:
    """提供无争用的 Windows 字节区间锁测试边界。"""

    LK_NBLCK = 1
    LK_UNLCK = 4

    @staticmethod
    def locking(descriptor: int, mode: int, size: int) -> None:
        """接受一个锁操作且不改变文件。

        `descriptor` 是目标文件描述符，`mode` 是锁模式，`size` 是锁定字节数；
        此无争用模拟没有返回值，也不会抛出锁冲突。
        """

        del descriptor, mode, size


class RecordingReplaceFile:
    """记录一次或多次假 `ReplaceFileW` 调用及 ctypes 声明。"""

    def __init__(self, *, succeeded: bool) -> None:
        """用 `succeeded` 配置 Win32 返回值；构造函数没有返回值。"""

        self.succeeded = succeeded
        self.calls: list[tuple[Any, ...]] = []
        self.argtypes: list[Any] | None = None
        self.restype: Any = None

    def __call__(
        self,
        target: str,
        replacement: str,
        backup: str,
        flags: int,
        exclude: object | None,
        reserved: object | None,
    ) -> int:
        """记录 Win32 参数并返回配置结果。

        `target`、`replacement`、`backup` 是三个原生路径，`flags` 是替换选项，
        `exclude` 与 `reserved` 是保留指针。返回 `1` 表示成功、`0` 表示失败。
        """

        self.calls.append((target, replacement, backup, flags, exclude, reserved))
        return int(self.succeeded)


@dataclass(frozen=True)
class NativeReplaceProbe:
    """保存假 Win32 loader 与 `ReplaceFileW` 调用证据。"""

    replace_file: RecordingReplaceFile
    loader_calls: list[tuple[str, bool]]


@dataclass(frozen=True)
class ReparseDirectoryStat:
    """提供 Windows reparse 目录识别所需的最小 stat 投影。"""

    st_mode: int
    st_dev: int
    st_ino: int
    st_file_attributes: int


def _draft_hash(content: bytes) -> str:
    """返回 `content` 的独立 Draft SHA-256 字面值。"""

    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _create_registered_target(
    tmp_path: Path,
    *,
    existing: bool,
) -> tuple[Path, Path]:
    """创建注册根目录与 Draft 路径。

    `tmp_path` 是隔离目录，`existing` 决定是否写入初始 Draft；返回注册根目录
    和目标路径，不创建任何根目录外的副本。
    """

    root = tmp_path / "registered"
    target = root / "workflows" / "windows.py"
    target.parent.mkdir(parents=True)
    if existing:
        target.write_bytes(INITIAL_SOURCE)
    return root, target


def _install_native_replace_probe(
    monkeypatch: pytest.MonkeyPatch,
    *,
    succeeded: bool,
    last_error: int,
) -> NativeReplaceProbe:
    """安装 Linux 可运行的假 Win32 `ReplaceFileW` 边界。

    `monkeypatch` 管理 ctypes 替换，`succeeded` 是原生返回值，`last_error`
    是失败时的 WinError。返回记录 loader、参数和 ctypes 签名的探针。
    """

    replace_file = RecordingReplaceFile(succeeded=succeeded)
    loader_calls: list[tuple[str, bool]] = []

    class FakeKernel32:
        """暴露单个受记录 `ReplaceFileW` 属性的假 kernel32。"""

        ReplaceFileW = replace_file

    def fake_windll(name: str, *, use_last_error: bool) -> FakeKernel32:
        """记录 `name` 与 `use_last_error`，并返回假 kernel32。"""

        loader_calls.append((name, use_last_error))
        return FakeKernel32()

    def fake_get_last_error() -> int:
        """返回调用方配置的 `last_error` WinError。"""

        return last_error

    def fake_format_error(error_number: int) -> str:
        """把 `error_number` 转成稳定的测试诊断文本。"""

        return f"WinError {error_number}"

    def fake_win_error(error_number: int) -> OSError:
        """把 `error_number` 转成带 `winerror` 属性的 `OSError`。"""

        error = OSError(error_number, fake_format_error(error_number))
        error.winerror = error_number
        return error

    monkeypatch.setattr(
        windows_draft_cas.ctypes,
        "WinDLL",
        fake_windll,
        raising=False,
    )
    monkeypatch.setattr(
        windows_draft_cas.ctypes,
        "get_last_error",
        fake_get_last_error,
        raising=False,
    )
    monkeypatch.setattr(
        windows_draft_cas.ctypes,
        "FormatError",
        fake_format_error,
        raising=False,
    )
    monkeypatch.setattr(
        windows_draft_cas.ctypes,
        "WinError",
        fake_win_error,
        raising=False,
    )
    return NativeReplaceProbe(
        replace_file=replace_file,
        loader_calls=loader_calls,
    )


def test_existing_draft_rejects_parent_symlink_swap_before_final_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """最终 replace 前父目录变成符号链接时不得写注册根目录外。

    `tmp_path` 提供注册根与根外目录，`monkeypatch` 在最终路径复核返回后替换
    父目录；测试没有返回值，并允许以路径无效或 CAS 冲突拒绝请求。
    """

    root, target = _create_registered_target(tmp_path, existing=True)
    registered_parent = target.parent
    detached_parent = tmp_path / "detached-existing"
    original_read_path = windows_draft_cas._read_regular_path
    swapped = False

    def read_then_swap_parent(path: Path, *, byte_limit: int) -> bytes:
        """有界读取 `path` 后替换父目录。

        `byte_limit` 限制读取长度；返回替换前读到的 Draft 字节。
        """

        nonlocal swapped
        content = original_read_path(path, byte_limit=byte_limit)
        if path == target and not swapped:
            swapped = True
            registered_parent.rename(detached_parent)
            registered_parent.symlink_to(detached_parent, target_is_directory=True)
        return content

    monkeypatch.setattr(
        windows_draft_cas,
        "_read_regular_path",
        read_then_swap_parent,
    )

    with pytest.raises(
        (
            windows_draft_cas.WindowsDraftCasConflict,
            windows_draft_cas.WindowsDraftCasInvalidTarget,
        )
    ):
        windows_draft_cas.write_windows_draft_cas(
            root=root,
            target=target,
            content=REQUEST_SOURCE,
            expected_hash=_draft_hash(INITIAL_SOURCE),
            byte_limit=DRAFT_BYTE_LIMIT,
            locking=AcceptingWindowsLocking(),
        )

    assert swapped
    assert registered_parent.is_symlink()
    assert (detached_parent / target.name).read_bytes() == INITIAL_SOURCE


def test_missing_draft_rejects_parent_symlink_swap_before_hard_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """missing Draft 的 hard-link 发布前父目录替换也必须失败关闭。

    `tmp_path` 提供注册根与根外目录，`monkeypatch` 在目标缺失检查后替换父目录；
    测试没有返回值，并断言请求字节没有发布到注册根外。
    """

    root, target = _create_registered_target(tmp_path, existing=False)
    registered_parent = target.parent
    detached_parent = tmp_path / "detached-missing"
    original_exists = Path.exists
    swapped = False

    def exists_then_swap_parent(path: Path) -> bool:
        """返回 `path` 原存在性，并在目标缺失检查后替换注册父目录。"""

        nonlocal swapped
        exists = original_exists(path)
        if path == target and not swapped:
            swapped = True
            registered_parent.rename(detached_parent)
            registered_parent.symlink_to(detached_parent, target_is_directory=True)
        return exists

    monkeypatch.setattr(Path, "exists", exists_then_swap_parent)

    with pytest.raises(
        (
            windows_draft_cas.WindowsDraftCasConflict,
            windows_draft_cas.WindowsDraftCasInvalidTarget,
        )
    ):
        windows_draft_cas.write_windows_draft_cas(
            root=root,
            target=target,
            content=REQUEST_SOURCE,
            expected_hash=None,
            byte_limit=DRAFT_BYTE_LIMIT,
            locking=AcceptingWindowsLocking(),
        )

    assert swapped
    assert registered_parent.is_symlink()
    assert not (detached_parent / target.name).exists()


def test_backup_winner_survives_first_rollback_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """backup 已证明外部胜者时，首次回滚失败不得保留请求字节为 canonical。

    `tmp_path` 提供隔离 Draft，`monkeypatch` 在发布时注入外部胜者并让第一次
    rollback replace 失败；测试没有返回值，冲突或内部错误都必须恢复外部字节。
    """

    root, target = _create_registered_target(tmp_path, existing=True)
    original_replace = windows_draft_cas._replace_with_backup
    replace_count = 0

    def replace_with_external_winner_and_one_rollback_failure(
        canonical: Path,
        replacement: Path,
        backup: Path,
    ) -> None:
        """注入一次发布竞争和一次回滚失败。

        `canonical`、`replacement`、`backup` 保持 replace seam 含义；没有返回值。
        """

        nonlocal replace_count
        replace_count += 1
        if replace_count == 1:
            canonical.write_bytes(EXTERNAL_SOURCE)
            original_replace(canonical, replacement, backup)
            return
        if replace_count == 2:
            raise OSError(32, "injected first rollback failure")
        original_replace(canonical, replacement, backup)

    monkeypatch.setattr(
        windows_draft_cas,
        "_replace_with_backup",
        replace_with_external_winner_and_one_rollback_failure,
    )

    with pytest.raises(
        (
            windows_draft_cas.WindowsDraftCasConflict,
            windows_draft_cas.WindowsDraftCasInternalError,
        )
    ):
        windows_draft_cas.write_windows_draft_cas(
            root=root,
            target=target,
            content=REQUEST_SOURCE,
            expected_hash=_draft_hash(INITIAL_SOURCE),
            byte_limit=DRAFT_BYTE_LIMIT,
            locking=AcceptingWindowsLocking(),
        )

    assert replace_count >= 2
    assert target.read_bytes() == EXTERNAL_SOURCE


@pytest.mark.parametrize(
    ("error_number", "expected_exception"),
    [
        pytest.param(
            2,
            windows_draft_cas.WindowsDraftCasConflict,
            id="file-not-found",
        ),
        pytest.param(
            3,
            windows_draft_cas.WindowsDraftCasConflict,
            id="path-not-found",
        ),
        pytest.param(
            32,
            windows_draft_cas.WindowsDraftCasConflict,
            id="sharing-violation",
        ),
        pytest.param(
            33,
            windows_draft_cas.WindowsDraftCasConflict,
            id="lock-violation",
        ),
        pytest.param(
            5,
            windows_draft_cas.WindowsDraftCasInternalError,
            id="access-denied-acl",
        ),
        pytest.param(
            112,
            windows_draft_cas.WindowsDraftCasInternalError,
            id="disk-full",
        ),
        pytest.param(
            29,
            windows_draft_cas.WindowsDraftCasInternalError,
            id="write-fault-io",
        ),
    ],
)
def test_native_replace_classifies_winerror_without_hiding_internal_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
    expected_exception: type[Exception],
) -> None:
    """原生 `ReplaceFileW` 必须区分 CAS 竞争与基础设施故障。

    `tmp_path` 提供三个路径，`monkeypatch` 安装失败 Win32 API，`error_number`
    是原生 WinError，`expected_exception` 是稳定分类；测试没有返回值。
    """

    _install_native_replace_probe(
        monkeypatch,
        succeeded=False,
        last_error=error_number,
    )

    with pytest.raises(expected_exception):
        windows_draft_cas._native_replace_with_backup(
            tmp_path / "windows.py",
            tmp_path / ".windows.py.tmp",
            tmp_path / ".windows.py.cas",
        )


def test_native_replace_passes_target_replacement_and_backup_to_win32(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功调用必须按 Win32 顺序传 target、replacement 与非空 backup。

    `tmp_path` 提供三个独立路径，`monkeypatch` 安装成功 Win32 API；测试没有
    返回值，并验证 loader、参数顺序、保守 flags 与 ctypes 函数签名。
    """

    target = tmp_path / "windows.py"
    replacement = tmp_path / ".windows.py.tmp"
    backup = tmp_path / ".windows.py.cas"
    probe = _install_native_replace_probe(
        monkeypatch,
        succeeded=True,
        last_error=0,
    )

    windows_draft_cas._native_replace_with_backup(target, replacement, backup)

    assert probe.loader_calls == [("kernel32", True)]
    assert probe.replace_file.calls == [
        (
            f"\\\\?\\{target.absolute()}",
            f"\\\\?\\{replacement.absolute()}",
            f"\\\\?\\{backup.absolute()}",
            0,
            None,
            None,
        )
    ]
    assert probe.replace_file.argtypes == [
        windows_draft_cas.ctypes.c_wchar_p,
        windows_draft_cas.ctypes.c_wchar_p,
        windows_draft_cas.ctypes.c_wchar_p,
        windows_draft_cas.ctypes.c_uint32,
        windows_draft_cas.ctypes.c_void_p,
        windows_draft_cas.ctypes.c_void_p,
    ]
    assert probe.replace_file.restype is windows_draft_cas.ctypes.c_int


def test_directory_chain_rejects_windows_reparse_point_attribute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows reparse-point 目录即使不是普通 symlink 也必须拒绝。

    `tmp_path` 提供真实目录链，`monkeypatch` 只给父目录附加 reparse 属性；
    测试没有返回值，并要求路径校验抛出 `WindowsDraftCasInvalidTarget`。
    """

    root = tmp_path / "registered"
    parent = root / "workflows"
    parent.mkdir(parents=True)
    original_lstat = Path.lstat

    def lstat_with_reparse_attribute(
        path: Path,
    ) -> os.stat_result | ReparseDirectoryStat:
        """返回 `path` 的 stat，并只为测试父目录附加 reparse 属性。"""

        result = original_lstat(path)
        if path != parent:
            return result
        return ReparseDirectoryStat(
            st_mode=result.st_mode,
            st_dev=result.st_dev,
            st_ino=result.st_ino,
            st_file_attributes=FILE_ATTRIBUTE_REPARSE_POINT,
        )

    monkeypatch.setattr(Path, "lstat", lstat_with_reparse_attribute)

    with pytest.raises(windows_draft_cas.WindowsDraftCasInvalidTarget):
        windows_draft_cas._validate_directory_chain(root, parent)
