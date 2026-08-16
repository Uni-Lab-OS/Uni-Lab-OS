"""无 ``dir_fd`` 平台的工作流源码（Workflow Source）绝对路径后端。"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from unilabos.workflow.source_file_access import (
    StableFileAccessError,
    StableFileSnapshot,
    assert_directory_identity,
    directory_identity,
    ensure_child_directory,
    read_regular_path,
    regular_path_signature,
)
from unilabos.workflow.source_publication import atomic_publish_source


def assert_package_root(
    package_root: Path,
    expected_identity: tuple[int, int],
) -> None:
    """复核无 ``dir_fd`` 包目录仍是授权时的同一物理目录。

    参数：``package_root`` 是规范绝对包路径；``expected_identity`` 是设备/索引
    节点身份。返回：身份一致时无返回值；不安全或变化时抛出
    ``StableFileAccessError``。
    """

    assert_directory_identity(package_root, expected_identity)


def validate_registered_source(
    package_root: Path,
    relative_path: PurePosixPath,
    *,
    expected_root_identity: tuple[int, int],
) -> None:
    """验证允许缺失的既有注册目标是安全普通文件。

    参数：包路径、规范相对路径和预期根身份共同固定来源。返回：安全或缺失时无
    返回值；目录/目标竞态、符号链接或类型错误抛出 ``StableFileAccessError``。
    """

    source_parent = _source_parent_path(
        package_root,
        relative_path,
        expected_root_identity=expected_root_identity,
        create=False,
    )
    if source_parent is None:
        return
    regular_path_signature(
        source_parent / relative_path.name,
        missing_ok=True,
    )
    assert_directory_identity(package_root, expected_root_identity)


def read_registered_source(
    package_root: Path,
    relative_path: PurePosixPath,
    *,
    expected_root_identity: tuple[int, int],
    byte_limit: int,
) -> StableFileSnapshot | None:
    """读取注册来源的稳定普通文件快照。

    参数：包路径、相对路径和根身份固定来源；``byte_limit`` 是源码硬上限。返回：
    缺失时为 ``None``，否则为稳定字节及元数据；不安全时抛出
    ``StableFileAccessError``。
    """

    source_parent = _source_parent_path(
        package_root,
        relative_path,
        expected_root_identity=expected_root_identity,
        create=False,
    )
    if source_parent is None:
        return None
    snapshot = read_regular_path(
        source_parent / relative_path.name,
        byte_limit=byte_limit,
        missing_ok=True,
    )
    assert_directory_identity(package_root, expected_root_identity)
    return snapshot


def registered_source_signature(
    package_root: Path,
    relative_path: PurePosixPath,
    *,
    expected_root_identity: tuple[int, int],
) -> tuple[object, ...]:
    """读取注册来源的稳定文件世代签名。

    参数：包路径、相对路径和预期根身份固定来源。返回：缺失或普通文件签名；
    不安全和身份变化时抛出 ``StableFileAccessError``。
    """

    source_parent = _source_parent_path(
        package_root,
        relative_path,
        expected_root_identity=expected_root_identity,
        create=False,
    )
    if source_parent is None:
        return ("missing",)
    signature = regular_path_signature(
        source_parent / relative_path.name,
        missing_ok=True,
    )
    assert_directory_identity(package_root, expected_root_identity)
    return signature


def publish_registered_source(
    package_root: Path,
    relative_path: PurePosixPath,
    content: bytes,
    *,
    expected_root_identity: tuple[int, int],
    byte_limit: int,
    expected_hash: object | str | None,
) -> None:
    """创建固定父目录并通过绝对路径后端原子发布注册源码。

    参数：包路径、相对路径、内容和根身份固定写入对象；``byte_limit`` 限制内容；
    ``expected_hash`` 是 CAS 条件。返回：发布并复核根身份后无返回值；安全、CAS
    和基础设施错误沿用下层分类。
    """

    source_parent = _source_parent_path(
        package_root,
        relative_path,
        expected_root_identity=expected_root_identity,
        create=True,
    )
    assert source_parent is not None
    atomic_publish_source(
        parent_path=source_parent,
        target_name=relative_path.name,
        content=content,
        byte_limit=byte_limit,
        expected_hash=expected_hash,
    )
    assert_directory_identity(package_root, expected_root_identity)


def read_package_manifest(
    selected_root: Path,
    *,
    byte_limit: int,
) -> tuple[tuple[int, int], bytes]:
    """读取显式选择目录中的稳定 ``package.yaml``。

    参数：``selected_root`` 是无符号链接绝对目录；``byte_limit`` 是 manifest
    上限。返回：目录身份与完整 manifest 字节；目录或文件不安全时抛出
    ``StableFileAccessError``。
    """

    root_identity = directory_identity(selected_root)
    snapshot = read_regular_path(
        selected_root / "package.yaml",
        byte_limit=byte_limit,
        missing_ok=False,
    )
    assert snapshot is not None
    assert_directory_identity(selected_root, root_identity)
    return root_identity, snapshot.content


def validate_declared_sources(
    selected_root: Path,
    *,
    expected_selected_identity: tuple[int, int],
    package_id: str,
    relative_paths: Iterable[str],
    source_byte_limit: int,
) -> tuple[Path, tuple[int, int]]:
    """验证 manifest 声明的包目录与允许缺失的 Python 源码。

    参数：选择目录及身份固定授权根；``package_id`` 是直接子包；相对路径已经由
    manifest 语法层验证；``source_byte_limit`` 是源码上限。返回：实际包路径和
    身份。异常：目录竞态、符号链接、非法文件、超限或非 UTF-8 抛出
    ``StableFileAccessError``。
    """

    assert_directory_identity(selected_root, expected_selected_identity)
    package_root = selected_root / package_id
    package_identity = directory_identity(package_root)
    workflows_root = package_root / "workflows"
    try:
        workflows_identity = directory_identity(workflows_root)
    except StableFileAccessError:
        if workflows_root.exists() or workflows_root.is_symlink():
            raise
        workflows_identity = None
    if workflows_identity is not None:
        for relative_path in tuple(relative_paths):
            normalized_relative = PurePosixPath(relative_path)
            try:
                snapshot = read_registered_source(
                    package_root,
                    normalized_relative,
                    expected_root_identity=package_identity,
                    byte_limit=source_byte_limit,
                )
            except StableFileAccessError:
                raise StableFileAccessError("invalid_workflow_source") from None
            if snapshot is not None:
                try:
                    snapshot.content.decode("utf-8")
                except UnicodeError:
                    raise StableFileAccessError("invalid_utf8_source") from None
        assert_directory_identity(workflows_root, workflows_identity)
    assert_directory_identity(package_root, package_identity)
    assert_directory_identity(selected_root, expected_selected_identity)
    return package_root, package_identity


def _source_parent_path(
    package_root: Path,
    relative_path: PurePosixPath,
    *,
    expected_root_identity: tuple[int, int],
    create: bool,
) -> Path | None:
    """在无 ``dir_fd`` 平台逐级验证或创建源码父目录。

    参数：``package_root`` 与其身份限定授权根；``relative_path`` 是已规范化源码
    路径；``create`` 决定是否创建缺失的 ``workflows`` 与工艺分类目录。
    返回：安全的直接父目录，允许缺失时返回 ``None``。
    安全：每一级都复核目录身份且拒绝符号链接与 Windows 重解析点。
    """

    assert_directory_identity(package_root, expected_root_identity)
    current = package_root
    current_identity = expected_root_identity
    for directory_name in relative_path.parts[:-1]:
        if create:
            child = ensure_child_directory(
                current,
                expected_root_identity=current_identity,
                child_name=directory_name,
            )
        else:
            child = current / directory_name
            try:
                child_identity = directory_identity(child)
            except StableFileAccessError:
                if child.exists() or child.is_symlink():
                    raise
                assert_directory_identity(current, current_identity)
                assert_directory_identity(package_root, expected_root_identity)
                return None
            assert_directory_identity(current, current_identity)
            current = child
            current_identity = child_identity
            continue
        current = child
        current_identity = directory_identity(current)
    assert_directory_identity(package_root, expected_root_identity)
    return current


__all__ = [
    "assert_package_root",
    "publish_registered_source",
    "read_package_manifest",
    "read_registered_source",
    "registered_source_signature",
    "validate_declared_sources",
    "validate_registered_source",
]
