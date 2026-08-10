"""远端设备软件包解析、缓存、可选复制与源码导出的编排。"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Protocol

from .cache import PackageCache
from .errors import PackageTransferError
from .transfer_models import (
    PackageDownloadRequest,
    PackageReleaseDescriptor,
    command_success_document,
)
from .workspace_export import export_derived_workspace


class PackageAcquirerPort(Protocol):
    """下载编排需要的最小远端包 Interface。"""

    def probe(self) -> str:
        """只读证明目标 Backend 的包协议能力。"""

        ...

    def resolve(self, request: PackageDownloadRequest) -> PackageReleaseDescriptor:
        """把用户选择器解析为可信发布描述。"""

        ...

    def download_artifact(self, template_uuid: str, target: Path) -> None:
        """把指定模板对应 Artifact 流式写入缓存临时文件。"""

        ...


def acquire_package(
    request: PackageDownloadRequest,
    *,
    port: PackageAcquirerPort,
    cache: PackageCache,
    environment: str,
    compile_catalog: Any,
    output_dir: str | Path | None = None,
    extract_source: str | Path | None = None,
) -> dict[str, Any]:
    """安全下载并缓存一个设备软件包，不安装或激活。

    参数：``request`` 是互斥选择器；``port`` 是 Backend Adapter；``cache`` 是
    内容寻址缓存；``environment`` 是固定环境；``compile_catalog`` 是统一静态
    编译器；``output_dir`` 是可选 wheel 副本目录；``extract_source`` 是可选派生
    工作区目标。
    返回：稳定 ``package.download`` 成功 JSON 字典。
    异常：能力、解析、网络、摘要、缓存或源码导出失败时抛出稳定
    ``PackageTransferError``；不修改依赖锁、Graph、Inventory 或设备实例。
    """

    port.probe()
    descriptor = port.resolve(request)
    cache_entry = cache.acquire(
        descriptor,
        download=port.download_artifact,
        compile_catalog=compile_catalog,
        exact_source_set=request.selector_kind == "package",
    )
    output_path = None
    if output_dir is not None:
        output_path = _copy_verified_wheel(
            cache_entry.wheel,
            Path(output_dir),
            descriptor,
        )
    source_path = None
    if extract_source is not None:
        source_path = export_derived_workspace(
            cache_entry.wheel,
            extract_source,
            descriptor=descriptor,
            catalog=cache_entry.catalog,
            environment=environment,
            compile_catalog=compile_catalog,
        )
    status = (
        "package_cached_and_source_exported"
        if source_path is not None
        else "package_cached"
    )
    extra: dict[str, Any] = {
        "cache_hit": cache_entry.cache_hit,
        "cache_key": cache_entry.cache_key,
    }
    if output_path is not None:
        extra["output"] = str(output_path)
    if source_path is not None:
        extra.update(
            {
                "source_exported": True,
                "source_output": str(source_path),
                "source_kind": "derived_workspace",
            }
        )
    return command_success_document(
        command="package.download",
        environment=environment,
        status=status,
        descriptor=descriptor,
        extra=extra,
    )


def _copy_verified_wheel(
    source: Path,
    output_dir: Path,
    descriptor: PackageReleaseDescriptor,
) -> Path:
    """把缓存权威对象原子复制到用户输出目录。

    参数：``source`` 是已验证缓存 wheel；``output_dir`` 是用户目录；
    ``descriptor`` 提供稳定文件名。
    返回：最终副本路径。
    异常：目标目录不可创建或文件不可写时转为 ``output_write_failed``。
    """

    try:
        destination_root = output_dir.expanduser().resolve()
        destination_root.mkdir(parents=True, exist_ok=True)
        target = destination_root / (
            f"{descriptor.normalized_name}-{descriptor.version}.whl"
        )
        with tempfile.NamedTemporaryFile(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=destination_root,
            delete=False,
        ) as stream:
            with source.open("rb") as source_stream:
                shutil.copyfileobj(source_stream, stream, length=1024 * 1024)
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)
        temporary.replace(target)
        return target
    except OSError as error:
        raise PackageTransferError(
            "output_write_failed",
            "已验证 wheel 无法复制到 --out 目录",
            retryable=False,
        ) from error


__all__ = ["PackageAcquirerPort", "acquire_package"]
