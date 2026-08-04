"""工作流源码发布（Source Publication）的系统错误分类合同。"""

from __future__ import annotations

import errno
import hashlib
from pathlib import Path

import pytest

from unilabos.workflow import source_publication


class PortableFcntl:
    """提供不触碰宿主锁状态的最小 POSIX ``flock`` 替身。"""

    LOCK_EX = 1
    LOCK_NB = 2
    LOCK_UN = 4

    def flock(self, descriptor: int, operation: int) -> None:
        """接受一次目标文件锁操作。

        参数：``descriptor`` 是 CAS 原稿描述符；``operation`` 是锁操作。返回：
        无；测试替身不改变宿主文件状态，也不抛出锁错误。
        """

        del descriptor, operation


class RefusingPortableFcntl(PortableFcntl):
    """从 POSIX 非阻塞锁接缝注入一个指定 errno。"""

    def __init__(self, error_number: int) -> None:
        """保存后续锁获取要抛出的系统错误码。

        参数：``error_number`` 是非阻塞 ``flock`` 返回的 errno。返回：无；构造
        期间不访问文件描述符。
        """

        self.error_number = error_number

    def flock(self, descriptor: int, operation: int) -> None:
        """拒绝目标文件的非阻塞独占锁。

        参数：``descriptor`` 是 CAS 原稿；``operation`` 是锁标志。返回：无；
        总是抛出配置的 ``OSError``，用于证明锁错误使用专属语义。
        """

        del descriptor, operation
        raise _system_error(self.error_number)


class RefusingCrtLock:
    """从 Windows CRT 非阻塞字节锁接缝注入一个指定 errno。"""

    LK_NBLCK = 1
    LK_UNLCK = 2

    def __init__(self, error_number: int) -> None:
        """保存后续字节锁获取要抛出的系统错误码。

        参数：``error_number`` 是 ``msvcrt.locking`` 返回的 errno。返回：无；
        构造期间不访问文件描述符。
        """

        self.error_number = error_number

    def locking(self, descriptor: int, mode: int, length: int) -> None:
        """拒绝目标文件的非阻塞字节锁。

        参数：``descriptor`` 是 CAS 原稿；``mode`` 是锁操作；``length`` 是锁定
        字节数。返回：无；总是抛出配置的 ``OSError``。
        """

        del descriptor, mode, length
        raise _system_error(self.error_number)


def _draft_hash(content: bytes) -> str:
    """计算工作流草稿（Workflow Draft）的稳定内容身份。

    参数：``content`` 是原稿字节。返回：带算法前缀的 SHA-256 哈希；无异常。
    """

    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _system_error(error_number: int, *, winerror: int | None = None) -> OSError:
    """构造可注入 POSIX errno 或 Windows winerror 的系统异常。

    参数：``error_number`` 是 POSIX 错误码；``winerror`` 是可选 Windows 错误码。
    返回：带稳定属性的 ``OSError``；不接触真实文件系统。
    """

    error = OSError(error_number, "注入的源码发布故障")
    if winerror is not None:
        error.winerror = winerror  # type: ignore[attr-defined]
    return error


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.ENOSPC, errno.EIO, errno.EMFILE])
def test_posix_cas_infrastructure_errno_is_not_reported_as_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
) -> None:
    """权限、空间、I/O 与描述符耗尽不得伪装成草稿内容冲突。

    参数：``tmp_path`` 隔离原稿；``monkeypatch`` 注入替换故障；
    ``error_number`` 是基础设施 errno。返回：无；公共发布接口必须抛出
    ``SourcePublicationError``，原稿保持不变。
    """

    parent = tmp_path / "workflows"
    parent.mkdir()
    target = parent / "demo.py"
    original = b"value = 'initial'\n"
    target.write_bytes(original)

    def fail_replace(
        _location: object,
        _source_name: str,
        _target_name: str,
    ) -> None:
        """在最终原子替换处抛出指定基础设施故障。

        参数：目录与两个文件名只保持被测方法形状。返回：无；总是抛出本用例的
        ``OSError``。
        """

        raise _system_error(error_number)

    monkeypatch.setattr(source_publication, "_PLATFORM", "freebsd")
    monkeypatch.setattr(source_publication, "_fcntl", PortableFcntl())
    monkeypatch.setattr(source_publication, "_msvcrt", None)
    monkeypatch.setattr(
        source_publication._PublicationDirectory,
        "replace_child",
        fail_replace,
    )

    with pytest.raises(source_publication.SourcePublicationError):
        source_publication.atomic_publish_source(
            parent_path=parent,
            target_name=target.name,
            content=b"value = 'changed'\n",
            byte_limit=1024,
            expected_hash=_draft_hash(original),
        )

    assert target.read_bytes() == original


@pytest.mark.parametrize("error_number", [errno.ENOENT, errno.EEXIST, errno.EAGAIN])
def test_posix_cas_race_errno_remains_a_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
) -> None:
    """消失、并发创建与锁竞争必须继续分类为草稿冲突。

    参数：``tmp_path`` 隔离原稿；``monkeypatch`` 注入替换竞争；
    ``error_number`` 是竞争 errno。返回：无；公共发布接口抛出
    ``SourcePublicationConflict``，不得误报基础设施故障。
    """

    parent = tmp_path / "workflows"
    parent.mkdir()
    target = parent / "demo.py"
    original = b"value = 'initial'\n"
    target.write_bytes(original)

    def fail_replace(
        _location: object,
        _source_name: str,
        _target_name: str,
    ) -> None:
        """在最终原子替换处抛出指定竞争故障。

        参数：目录与两个文件名只保持被测方法形状。返回：无；总是抛出本用例的
        ``OSError``。
        """

        raise _system_error(error_number)

    monkeypatch.setattr(source_publication, "_PLATFORM", "freebsd")
    monkeypatch.setattr(source_publication, "_fcntl", PortableFcntl())
    monkeypatch.setattr(source_publication, "_msvcrt", None)
    monkeypatch.setattr(
        source_publication._PublicationDirectory,
        "replace_child",
        fail_replace,
    )

    with pytest.raises(source_publication.SourcePublicationConflict):
        source_publication.atomic_publish_source(
            parent_path=parent,
            target_name=target.name,
            content=b"value = 'changed'\n",
            byte_limit=1024,
            expected_hash=_draft_hash(original),
        )

    assert target.read_bytes() == original


def test_posix_flock_eacces_is_lock_contention_not_infrastructure_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POSIX 非阻塞锁返回 EACCES 时必须表达并发占用。

    参数：``tmp_path`` 隔离 CAS 原稿；``monkeypatch`` 注入 POSIX 锁后端。
    返回：无；公共发布接口抛出 ``SourcePublicationConflict``，但同 errno 在文件
    替换接缝仍由既有测试证明是 ``SourcePublicationError``。
    """

    parent = tmp_path / "workflows"
    parent.mkdir()
    target = parent / "demo.py"
    original = b"value = 'initial'\n"
    target.write_bytes(original)
    monkeypatch.setattr(source_publication, "_PLATFORM", "freebsd")
    monkeypatch.setattr(
        source_publication,
        "_fcntl",
        RefusingPortableFcntl(errno.EACCES),
    )
    monkeypatch.setattr(source_publication, "_msvcrt", None)

    with pytest.raises(source_publication.SourcePublicationConflict):
        source_publication.atomic_publish_source(
            parent_path=parent,
            target_name=target.name,
            content=b"value = 'changed'\n",
            byte_limit=1024,
            expected_hash=_draft_hash(original),
        )

    assert target.read_bytes() == original


@pytest.mark.parametrize(
    "error_number",
    [errno.EACCES, getattr(errno, "EDEADLOCK", errno.EDEADLK)],
)
def test_windows_crt_lock_contention_errno_is_not_infrastructure_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
) -> None:
    """Windows CRT 的 EACCES/EDEADLOCK 必须表达非阻塞锁竞争。

    参数：``tmp_path`` 隔离 CAS 原稿；``monkeypatch`` 注入 CRT 锁后端；
    ``error_number`` 是 CRT 非阻塞锁错误码。返回：无；公共发布接口抛出
    ``SourcePublicationConflict``，不得把合法占用误报为基础设施故障。
    """

    parent = tmp_path / "workflows"
    parent.mkdir()
    target = parent / "demo.py"
    original = b"value = 'initial'\n"
    target.write_bytes(original)
    monkeypatch.setattr(source_publication, "_PLATFORM", "freebsd")
    monkeypatch.setattr(source_publication, "_fcntl", None)
    monkeypatch.setattr(
        source_publication,
        "_msvcrt",
        RefusingCrtLock(error_number),
    )

    with pytest.raises(source_publication.SourcePublicationConflict):
        source_publication.atomic_publish_source(
            parent_path=parent,
            target_name=target.name,
            content=b"value = 'changed'\n",
            byte_limit=1024,
            expected_hash=_draft_hash(original),
        )

    assert target.read_bytes() == original
