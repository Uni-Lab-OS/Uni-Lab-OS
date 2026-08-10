"""设备软件包 wheel 的内容寻址缓存、锁与原子提交。"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

try:  # pragma: no cover - 目标操作系统决定锁实现
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows 没有 fcntl
    _fcntl = None

try:  # pragma: no cover - 目标操作系统决定锁实现
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - POSIX 没有 msvcrt
    _msvcrt = None

from ..package_catalog import PackageCatalog
from .errors import PackageBuildError, PackageTransferError
from .transfer_models import PackageCacheEntry, PackageReleaseDescriptor
from .wheel import verify_downloaded_package_wheel


class PackageCache:
    """只保存可重建验证元数据的内容寻址 Artifact 缓存。"""

    def __init__(self, root: str | Path) -> None:
        """固定缓存 v1 根并创建受管子目录。

        参数：``root`` 是 ``package-cache/v1`` 目录。
        返回：无。
        异常：目录不可创建时传播原始 IO 异常。
        """

        self.root = Path(root).expanduser().resolve()
        self.objects = self.root / "objects" / "sha256"
        self.verification = self.root / "verification"
        self.locks = self.root / "locks"
        for directory in (self.objects, self.verification, self.locks):
            directory.mkdir(parents=True, exist_ok=True)

    def acquire(
        self,
        descriptor: PackageReleaseDescriptor,
        *,
        download: Callable[[str, Path], None],
        compile_catalog: Callable[[Any], PackageCatalog],
        exact_source_set: bool,
    ) -> PackageCacheEntry:
        """取得、完整验证并原子提交一个远端 wheel。

        参数：``descriptor`` 是 Backend 可信描述；``download`` 把指定模板的同一
        wheel 流式写到目标临时文件；``compile_catalog`` 是统一目录编译器；
        ``exact_source_set`` 控制包选择和模板选择的源码集合验证强度。
        返回：已验证缓存条目；命中时不调用 ``download``。
        异常：网络错误保持 Adapter 错误；归档或 parity 错误归一为
        ``PackageTransferError``，失败文件不会成为可用缓存对象。
        """

        digest_hex = descriptor.artifact_digest.removeprefix("sha256:")
        object_path = self.objects / f"{digest_hex}.whl"
        lock_path = self.locks / f"{digest_hex}.lock"
        with _exclusive_file_lock(lock_path):
            if object_path.exists():
                try:
                    catalog = self._verify(
                        object_path,
                        descriptor,
                        compile_catalog=compile_catalog,
                        exact_source_set=exact_source_set,
                    )
                    return PackageCacheEntry(
                        wheel=object_path,
                        catalog=catalog,
                        cache_hit=True,
                        cache_key=descriptor.cache_key,
                    )
                except (PackageBuildError, ValueError):
                    corrupt = object_path.with_name(
                        f".{object_path.name}.corrupt-{uuid4().hex}"
                    )
                    object_path.replace(corrupt)

            temporary_path = self._temporary_object_path(digest_hex)
            try:
                download(descriptor.template_uuid, temporary_path)
                catalog = self._verify(
                    temporary_path,
                    descriptor,
                    compile_catalog=compile_catalog,
                    exact_source_set=exact_source_set,
                )
                _fsync_file(temporary_path)
                temporary_path.replace(object_path)
                try:
                    self._write_verification(descriptor, object_path)
                except OSError:
                    # 验证记录只是可重建的观测数据；正式对象会在每次命中时重新验签，
                    # 因此不能让元数据落盘失败否定已经原子提交的可信 wheel。
                    pass
            except (PackageBuildError, ValueError) as error:
                temporary_path.unlink(missing_ok=True)
                raise PackageTransferError(
                    "remote_package_incompatible",
                    "下载的设备软件包与远端发布描述不一致",
                    retryable=False,
                ) from error
            except Exception:
                temporary_path.unlink(missing_ok=True)
                raise
        return PackageCacheEntry(
            wheel=object_path,
            catalog=catalog,
            cache_hit=False,
            cache_key=descriptor.cache_key,
        )

    def _verify(
        self,
        wheel: Path,
        descriptor: PackageReleaseDescriptor,
        *,
        compile_catalog: Callable[[Any], PackageCatalog],
        exact_source_set: bool,
    ) -> PackageCatalog:
        """验证缓存候选的 Artifact、Catalog 和远端描述 parity。

        参数：``wheel`` 是缓存对象或临时候选；``descriptor`` 是远端描述；
        ``compile_catalog`` 是统一编译器；``exact_source_set`` 控制设备集合验证。
        返回：从 wheel 来源重新证明的目录。
        异常：归档或身份不一致时抛出 ``PackageBuildError``/``ValueError``。
        """

        catalog = verify_downloaded_package_wheel(
            wheel,
            expected_digest=descriptor.artifact_digest,
            compile_catalog=compile_catalog,
        )
        descriptor.assert_catalog_parity(
            catalog,
            exact_source_set=exact_source_set,
        )
        return catalog

    def _temporary_object_path(self, digest_hex: str) -> Path:
        """在对象目录创建同文件系统空临时文件。

        参数：``digest_hex`` 是 Artifact 十六进制摘要。
        返回：调用方独占的临时路径。
        异常：目录不可写时传播原始 IO 异常。
        """

        descriptor, name = tempfile.mkstemp(
            prefix=f".{digest_hex}.",
            suffix=".download",
            dir=self.objects,
        )
        os.close(descriptor)
        return Path(name)

    def _write_verification(
        self,
        descriptor: PackageReleaseDescriptor,
        object_path: Path,
    ) -> None:
        """原子写入可重建的缓存验证记录。

        参数：``descriptor`` 是已完成对账的发布描述；``object_path`` 是正式对象。
        返回：无。
        异常：元数据目录不可写时传播原始 IO 异常；wheel 对象仍可在下次重验。
        """

        digest_hex = descriptor.artifact_digest.removeprefix("sha256:")
        payload = {
            "schema_version": "unilab-package-cache-verification/v1",
            "artifact_digest": descriptor.artifact_digest,
            "catalog_digest": descriptor.catalog_digest,
            "content_digest": descriptor.content_digest,
            "distribution": descriptor.distribution,
            "namespace": descriptor.namespace,
            "object": object_path.relative_to(self.root).as_posix(),
            "version": descriptor.version,
        }
        target = self.verification / f"{digest_hex}.json"
        _atomic_write_json(target, payload)


@contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    """在一个稳定缓存键上持有跨进程排他文件锁。

    参数：``path`` 是锁文件。
    返回：上下文期间不返回额外值。
    异常：平台不支持文件锁或 IO 失败时抛出 ``RuntimeError``/原始异常。
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    try:
        if _fcntl is not None:
            _fcntl.flock(stream.fileno(), _fcntl.LOCK_EX)
        elif _msvcrt is not None:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            _msvcrt.locking(stream.fileno(), _msvcrt.LK_LOCK, 1)
        else:
            raise RuntimeError("当前平台不支持软件包缓存文件锁")
        yield
    finally:
        try:
            if _fcntl is not None:
                _fcntl.flock(stream.fileno(), _fcntl.LOCK_UN)
            elif _msvcrt is not None:
                stream.seek(0)
                _msvcrt.locking(stream.fileno(), _msvcrt.LK_UNLCK, 1)
        finally:
            stream.close()


def _fsync_file(path: Path) -> None:
    """同步一个下载候选的文件内容。

    参数：``path`` 是已关闭写端的普通文件。
    返回：无。
    异常：文件不可打开或同步时传播原始 IO 异常。
    """

    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _atomic_write_json(target: Path, document: dict[str, Any]) -> None:
    """在目标目录内原子写入一个确定性 JSON 文档。

    参数：``target`` 是正式路径；``document`` 是可序列化字典。
    返回：无。
    异常：编码或文件系统失败时传播原始异常。
    """

    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
        delete=False,
    ) as stream:
        json.dump(document, stream, ensure_ascii=False, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
        temporary = Path(stream.name)
    temporary.replace(target)


__all__ = ["PackageCache"]
