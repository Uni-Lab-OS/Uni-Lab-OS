"""Round 02G2 在真实 Windows kernel32/msvcrt 上运行的 Draft CAS 门禁。"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from unilabos.workflow import windows_draft_cas

try:
    import msvcrt
except ModuleNotFoundError:  # pragma: no cover - Windows CI 执行真实分支
    msvcrt = None  # type: ignore[assignment]

pytestmark = pytest.mark.skipif(
    os.name != "nt",
    reason="真实 Win32 文件共享与 ReplaceFileW 合同只在 Windows 运行",
)


def _draft_hash(content: bytes) -> str:
    """返回 `content` 的 Workflow Draft hash；输入字节保持不变。"""

    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def test_native_windows_replaces_existing_draft_with_real_msvcrt(
    tmp_path: Path,
) -> None:
    """真实 `msvcrt` 锁和 `ReplaceFileW` 应发布匹配 CAS 的已有 Draft。

    `tmp_path` 提供本地 NTFS 临时目录；测试没有返回值，并验证 canonical 字节与
    临时 artifact。安全不变量是只有旧 hash 匹配时才能替换。
    """

    assert msvcrt is not None
    root = tmp_path / "registered"
    target = root / "workflows" / "windows.py"
    target.parent.mkdir(parents=True)
    initial = b"seed()\n"
    replacement = b"build()\n"
    target.write_bytes(initial)

    windows_draft_cas.write_windows_draft_cas(
        root=root,
        target=target,
        content=replacement,
        expected_hash=_draft_hash(initial),
        byte_limit=1024 * 1024,
        locking=msvcrt,
    )

    assert target.read_bytes() == replacement
    assert [path.name for path in target.parent.iterdir()] == [target.name]


def test_native_windows_creates_missing_draft_without_clobber(
    tmp_path: Path,
) -> None:
    """真实 Windows missing-Draft 分支应以 create-if-absent 语义发布。

    `tmp_path` 提供 registered root，目标开始不存在；测试没有返回值，并验证新
    Draft 字节。安全不变量是并发同名文件出现时不得覆盖。
    """

    assert msvcrt is not None
    root = tmp_path / "registered"
    target = root / "workflows" / "windows.py"
    root.mkdir()
    replacement = b"build()\n"

    windows_draft_cas.write_windows_draft_cas(
        root=root,
        target=target,
        content=replacement,
        expected_hash=None,
        byte_limit=1024 * 1024,
        locking=msvcrt,
    )

    assert target.read_bytes() == replacement


def test_native_windows_directory_guard_blocks_parent_rename(
    tmp_path: Path,
) -> None:
    """Win32 目录链 guard 必须阻止另一个进程调换 Draft 父目录。

    `tmp_path` 提供 registered 目录，子进程尝试原生 rename；测试没有返回值。
    guard 内 rename 必须失败，释放 guard 后同一命令必须成功，证明拒绝来自句柄
    share mode，而不是路径或权限配置。
    """

    root = tmp_path / "registered"
    parent = root / "workflows"
    renamed = root / "detached"
    parent.mkdir(parents=True)
    identity = windows_draft_cas._validate_directory_chain(root, parent)
    command = [
        sys.executable,
        "-c",
        "import os, sys; os.rename(sys.argv[1], sys.argv[2])",
        str(parent),
        str(renamed),
    ]

    with windows_draft_cas._publication_directory_guard(root, parent, identity):
        blocked = subprocess.run(command, capture_output=True, text=True, check=False)

    assert blocked.returncode != 0
    assert parent.is_dir()
    released = subprocess.run(command, capture_output=True, text=True, check=False)
    assert released.returncode == 0, released.stderr
    renamed.rename(parent)
