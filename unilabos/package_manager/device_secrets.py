"""本地设备初始化秘密的版本化引用与受管文件存储。"""

from __future__ import annotations

import hmac
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

SECRET_REFERENCE_KEY = "$unilab_secret"
SECRET_REFERENCE_SCHEMA = "device-secret-ref/v1"
_SECRET_DIRECTORY = "device-secrets"
_MAX_SECRET_BYTES = 64 * 1024


class DeviceSecretError(RuntimeError):
    """设备初始化秘密无法安全保存或解析。"""


def protect_device_configuration(
    configuration: Mapping[str, Any],
    schema: Mapping[str, Any],
    *,
    working_dir: str | Path,
    existing_configuration: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """把秘密配置写入受管存储，并返回只含版本化引用的设备图配置。

    参数 ``configuration`` 是已经按 PackageCatalog 校验的本次用户输入；``schema``
    标识哪些顶层字段是秘密；``working_dir`` 是当前 OS 受管工作目录；可选
    ``existing_configuration`` 用于同值重放时复用既有引用。返回值可安全写入设备
    图，但秘密明文只在本函数调用期间存在。字段合同、秘密类型、存储路径或权限
    无效时抛出 :class:`DeviceSecretError`，且绝不在异常正文中回显秘密。
    """

    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        raise DeviceSecretError("设备配置 Schema 缺少 properties object")
    existing = existing_configuration or {}
    protected: dict[str, Any] = {}
    for name, value in configuration.items():
        property_schema = properties.get(name)
        if not isinstance(property_schema, Mapping):
            raise DeviceSecretError(f"设备配置 Schema 缺少字段合同: {name}")
        if property_schema.get("x-unilab-secret") is not True:
            protected[name] = value
            continue
        if property_schema.get("type") != "string" or not isinstance(value, str):
            raise DeviceSecretError(f"设备秘密参数 {name} 必须是 string")
        if not value:
            raise DeviceSecretError(f"设备秘密参数 {name} 不能为空")
        existing_reference = existing.get(name)
        if existing_reference is not None:
            existing_secret = _read_existing_secret_if_valid(
                existing_reference,
                working_dir=working_dir,
            )
            if existing_secret is not None and hmac.compare_digest(
                existing_secret.encode("utf-8"),
                value.encode("utf-8"),
            ):
                protected[name] = existing_reference
                continue
        protected[name] = _write_device_secret(value, working_dir=working_dir)
    return protected


def resolve_device_configuration(
    configuration: Mapping[str, Any],
    *,
    working_dir: str | Path,
) -> dict[str, Any]:
    """在驱动构造边界把设备图中的秘密引用解析为短生命周期参数。

    参数 ``configuration`` 来自当前设备图，``working_dir`` 是启动时冻结的 OS
    受管目录。返回供驱动构造函数使用的新字典，不修改设备图或 Resource 投影。
    引用结构、目标文件、所有者、权限或 UTF-8 内容不可信时抛出
    :class:`DeviceSecretError` 并失败关闭。
    """

    resolved: dict[str, Any] = {}
    for name, value in configuration.items():
        reference = _parse_secret_reference(value)
        resolved[name] = (
            _read_device_secret(reference, working_dir=working_dir)
            if reference is not None
            else value
        )
    return resolved


def _write_device_secret(value: str, *, working_dir: str | Path) -> dict[str, Any]:
    """以独占 0600 文件保存一个秘密并返回不含路径的随机引用。

    参数 ``value`` 是本次短生命周期明文，``working_dir`` 是受管目录。返回封闭
    的 ``device-secret-ref/v1`` JSON 对象；大小或文件系统操作不安全时抛出
    :class:`DeviceSecretError`。
    """

    payload = value.encode("utf-8")
    if len(payload) > _MAX_SECRET_BYTES:
        raise DeviceSecretError("设备秘密超过 64 KiB 上限")
    secret_root = _secret_root(working_dir, create=True)
    secret_id = str(uuid4())
    target = secret_root / secret_id
    descriptor: int | None = None
    try:
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(target, 0o600)
        _fsync_directory(secret_root)
    except (OSError, ValueError) as exc:
        if descriptor is not None:
            os.close(descriptor)
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        raise DeviceSecretError("设备秘密无法写入受管存储") from exc
    return {
        SECRET_REFERENCE_KEY: {
            "schema_version": SECRET_REFERENCE_SCHEMA,
            "id": secret_id,
        }
    }


def _read_existing_secret_if_valid(
    value: Any,
    *,
    working_dir: str | Path,
) -> str | None:
    """读取可复用既有引用；缺失文件视为可修复，非法引用仍失败关闭。

    参数 ``value`` 是既有设备图字段，``working_dir`` 是受管目录。返回已验证的
    明文或在引用文件缺失时返回 ``None``；结构和权限错误仍抛出异常。
    """

    reference = _parse_secret_reference(value)
    if reference is None:
        raise DeviceSecretError("设备图的秘密字段不是受支持的引用")
    try:
        return _read_device_secret(reference, working_dir=working_dir)
    except DeviceSecretError as exc:
        if exc.__cause__ is not None and isinstance(exc.__cause__, FileNotFoundError):
            return None
        raise


def _read_device_secret(reference: str, *, working_dir: str | Path) -> str:
    """校验受管文件身份、所有者和权限后读取一个 UTF-8 秘密。

    参数 ``reference`` 是规范 UUID，``working_dir`` 是受管目录。返回秘密明文；
    文件不可读、越权、过大或内容无效时抛出 :class:`DeviceSecretError`。
    """

    secret_root = _secret_root(working_dir, create=False)
    target = secret_root / reference
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(target, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise DeviceSecretError("设备秘密引用目标不是普通文件")
        if os.name != "nt":
            if metadata.st_mode & 0o077:
                raise DeviceSecretError("设备秘密文件权限过宽")
            if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
                raise DeviceSecretError("设备秘密文件所有者不匹配")
        if metadata.st_size > _MAX_SECRET_BYTES:
            raise DeviceSecretError("设备秘密文件超过 64 KiB 上限")
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = None
            payload = stream.read(_MAX_SECRET_BYTES + 1)
    except DeviceSecretError:
        raise
    except OSError as exc:
        raise DeviceSecretError("设备秘密引用不可读取") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(payload) > _MAX_SECRET_BYTES:
        raise DeviceSecretError("设备秘密文件超过 64 KiB 上限")
    try:
        value = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DeviceSecretError("设备秘密不是合法 UTF-8") from exc
    if not value:
        raise DeviceSecretError("设备秘密不能为空")
    return value


def _parse_secret_reference(value: Any) -> str | None:
    """严格解析封闭的 ``device-secret-ref/v1`` 引用。

    参数 ``value`` 是设备图中的未知 JSON 值。普通配置返回 ``None``，合法引用
    返回规范 UUID；带保留键但合同无效时抛出 :class:`DeviceSecretError`。
    """

    if not isinstance(value, Mapping) or SECRET_REFERENCE_KEY not in value:
        return None
    if set(value) != {SECRET_REFERENCE_KEY}:
        raise DeviceSecretError("设备秘密引用包含未知字段")
    body = value[SECRET_REFERENCE_KEY]
    if not isinstance(body, Mapping) or set(body) != {"schema_version", "id"}:
        raise DeviceSecretError("设备秘密引用合同无效")
    if body.get("schema_version") != SECRET_REFERENCE_SCHEMA:
        raise DeviceSecretError("设备秘密引用版本不受支持")
    try:
        return str(UUID(str(body.get("id") or "")))
    except ValueError as exc:
        raise DeviceSecretError("设备秘密引用 ID 无效") from exc


def _secret_root(working_dir: str | Path, *, create: bool) -> Path:
    """解析并校验受管秘密目录，拒绝符号链接和非目录替换。

    参数 ``working_dir`` 是 OS 受管根目录，``create`` 决定是否允许首次创建。
    返回固定 ``device-secrets/v1`` 路径；目录身份或权限不可信时抛出异常。
    """

    try:
        working_root = Path(working_dir).expanduser().resolve(strict=True)
        if not working_root.is_dir():
            raise DeviceSecretError("OS 受管工作目录不可信")
        parent = working_root / _SECRET_DIRECTORY
        root = parent / "v1"
        _secure_directory(parent, create=create)
        _secure_directory(root, create=create)
    except DeviceSecretError:
        raise
    except OSError as exc:
        raise DeviceSecretError("设备秘密目录不可用") from exc
    return root


def _secure_directory(path: Path, *, create: bool) -> None:
    """创建或校验一个 0700 受管目录，绝不跟随既有符号链接。

    参数 ``path`` 是固定子目录，``create`` 决定是否允许创建。函数无返回值；
    路径、所有者或 POSIX 权限不符合要求时抛出 :class:`DeviceSecretError`。
    """

    if not path.exists() and not path.is_symlink():
        if not create:
            raise FileNotFoundError(path)
        path.mkdir(mode=0o700)
        if os.name != "nt":
            os.chmod(path, 0o700)
    if path.is_symlink() or not path.is_dir():
        raise DeviceSecretError("设备秘密目录不可信")
    metadata = path.stat()
    if os.name != "nt":
        if metadata.st_mode & 0o077:
            raise DeviceSecretError("设备秘密目录权限过宽")
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            raise DeviceSecretError("设备秘密目录所有者不匹配")


def _fsync_directory(path: Path) -> None:
    """在支持目录 fsync 的平台持久化新秘密目录项。

    参数 ``path`` 是已验证秘密目录。函数无业务返回值；Windows 直接返回，其他
    平台将文件系统错误交给调用方转换为不含秘密的诊断。
    """

    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "DeviceSecretError",
    "SECRET_REFERENCE_KEY",
    "SECRET_REFERENCE_SCHEMA",
    "protect_device_configuration",
    "resolve_device_configuration",
]
