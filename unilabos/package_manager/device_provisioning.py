"""受管设备包到本地设备图实例声明的原子接入模块。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID, uuid4

from .community import CommunityPackageError, load_cached_community_package
from .device_package import (
    DevicePackageError,
    configuration_schema_for_definition,
    device_definition_from_catalog,
    validate_configuration_for_definition,
)
from .device_secrets import DeviceSecretError, protect_device_configuration

_INSTANCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class DeviceProvisioningError(RuntimeError):
    """候选本地设备接入无法安全修改设备图。"""


@dataclass(frozen=True)
class DeviceGraphMutationResult:
    """设备图原子变更后的稳定 CLI 投影。"""

    status: str
    instance_id: str
    instance_uuid: str
    definition_fqid: str
    graph_fingerprint: str
    backup_path: str | None
    changed: bool

    def to_dict(self) -> dict[str, Any]:
        """返回 Electron Main 可持久化的 JSON 安全变更结果。"""

        return {
            "status": self.status,
            "instance_id": self.instance_id,
            "instance_uuid": self.instance_uuid,
            "definition_fqid": self.definition_fqid,
            "graph_fingerprint": self.graph_fingerprint,
            "backup_path": self.backup_path,
            "changed": self.changed,
        }


def stage_device_instance(
    *,
    graph_path: str | Path,
    working_dir: str | Path,
    cache_key: str,
    definition_fqid: str,
    instance_id: str,
    instance_uuid: str | None,
    display_name: str,
    configuration: Mapping[str, Any],
) -> DeviceGraphMutationResult:
    """校验缓存、配置和设备图后原子新增一个本地设备实例声明。

    ``graph_path`` 是 Electron 当前 LocalRuntime 选择的设备图，``working_dir``
    是同一 OS 的受管缓存目录；``cache_key`` 与 ``definition_fqid`` 绑定已校验
    设备包；``instance_id``/``instance_uuid`` 是本地实例身份；
    ``display_name`` 与 ``configuration`` 是用户确认且只经 stdin 传递的配置。
    秘密字段会进入受管秘密存储，设备图只保存版本化引用。返回 ``graph_staged``
    结果。完全相同的重复请求幂等成功；身份冲突或任一校验失败时抛出
    :class:`DeviceProvisioningError`，原图保持不变。
    """

    return _write_device_instance(
        graph_path=graph_path,
        working_dir=working_dir,
        cache_key=cache_key,
        definition_fqid=definition_fqid,
        instance_id=instance_id,
        instance_uuid=instance_uuid,
        display_name=display_name,
        configuration=configuration,
        update_existing=False,
    )


def update_device_instance(
    *,
    graph_path: str | Path,
    working_dir: str | Path,
    cache_key: str,
    definition_fqid: str,
    instance_id: str,
    instance_uuid: str,
    display_name: str,
    configuration: Mapping[str, Any],
) -> DeviceGraphMutationResult:
    """显式校验并原子更新一个既有本地设备实例的名称与配置。

    所有参数与 :func:`stage_device_instance` 相同，但 ``instance_uuid`` 必填并
    必须与图中实例一致。函数不允许改变设备 definition、实例 ID 或 UUID；返回
    ``graph_staged``，完全相同的配置返回幂等成功，冲突时原图保持不变。
    """

    return _write_device_instance(
        graph_path=graph_path,
        working_dir=working_dir,
        cache_key=cache_key,
        definition_fqid=definition_fqid,
        instance_id=instance_id,
        instance_uuid=instance_uuid,
        display_name=display_name,
        configuration=configuration,
        update_existing=True,
    )


def remove_device_instance(
    *,
    graph_path: str | Path,
    instance_id: str,
    instance_uuid: str | None = None,
) -> DeviceGraphMutationResult:
    """从设备图原子移除一个无子节点的本地设备实例及其连接。

    ``graph_path`` 是当前设备图，``instance_id`` 是目标本地身份；可选
    ``instance_uuid`` 用于防止同名误删。返回 ``removed`` 和变更前备份路径。
    目标不存在时幂等成功；存在子节点、UUID 冲突或图无效时抛出
    :class:`DeviceProvisioningError` 且不修改原图。
    """

    path, graph, original = _read_graph(graph_path)
    nodes = graph["nodes"]
    matches = [node for node in nodes if node.get("id") == instance_id]
    if not matches:
        return DeviceGraphMutationResult(
            status="removed",
            instance_id=instance_id,
            instance_uuid=instance_uuid or "",
            definition_fqid="",
            graph_fingerprint=_graph_fingerprint(graph),
            backup_path=None,
            changed=False,
        )
    if len(matches) != 1:
        raise DeviceProvisioningError(f"设备图存在重复实例 ID: {instance_id}")
    target = matches[0]
    target_uuid = str(target.get("uuid") or "")
    if instance_uuid and target_uuid != _canonical_uuid(instance_uuid, "instance UUID"):
        raise DeviceProvisioningError("设备实例 UUID 与移除请求不一致")
    if any(
        node is not target
        and node.get("parent") in {instance_id, target_uuid}
        for node in nodes
    ):
        raise DeviceProvisioningError("设备实例仍有子节点，不能直接移除")
    graph["nodes"] = [node for node in nodes if node is not target]
    link_key = _graph_link_key(graph)
    graph[link_key] = [
        link
        for link in graph[link_key]
        if link.get("source") not in {instance_id, target_uuid}
        and link.get("target") not in {instance_id, target_uuid}
    ]
    backup = _commit_graph(path, graph, original)
    return DeviceGraphMutationResult(
        status="removed",
        instance_id=instance_id,
        instance_uuid=target_uuid,
        definition_fqid=str(target.get("class") or ""),
        graph_fingerprint=_graph_fingerprint(graph),
        backup_path=str(backup),
        changed=True,
    )


def restore_device_graph(
    *,
    graph_path: str | Path,
    backup_path: str | Path,
) -> DeviceGraphMutationResult:
    """校验并原子恢复同目录下由本模块生成的设备图备份。

    ``graph_path`` 是当前设备图，``backup_path`` 必须是同目录、非 symlink 且
    名称绑定目标图的备份。返回 ``graph_restored``；恢复前仍会为当前图生成
    可恢复备份。无效或跨目录备份会失败关闭。
    """

    path, current_graph, original = _read_graph(graph_path)
    backup = Path(backup_path).expanduser().absolute()
    if (
        backup.parent != path.parent
        or not backup.name.startswith(f"{path.name}.unilab-backup-")
        or backup.is_symlink()
        or not backup.is_file()
    ):
        raise DeviceProvisioningError("设备图备份路径不受信")
    restored_bytes = backup.read_bytes()
    restored_graph = _decode_graph(restored_bytes, backup)
    recovery_backup = _write_backup(path, original, current_graph)
    _atomic_replace(path, _encode_graph(restored_graph), path.stat().st_mode)
    return DeviceGraphMutationResult(
        status="graph_restored",
        instance_id="",
        instance_uuid="",
        definition_fqid="",
        graph_fingerprint=_graph_fingerprint(restored_graph),
        backup_path=str(recovery_backup),
        changed=restored_graph != current_graph,
    )


def _write_device_instance(
    *,
    graph_path: str | Path,
    working_dir: str | Path,
    cache_key: str,
    definition_fqid: str,
    instance_id: str,
    instance_uuid: str | None,
    display_name: str,
    configuration: Mapping[str, Any],
    update_existing: bool,
) -> DeviceGraphMutationResult:
    """实现新增与显式更新共享的缓存、身份、配置和原子写入规则。"""

    if not _INSTANCE_ID.fullmatch(instance_id):
        raise DeviceProvisioningError("设备实例 ID 只能包含字母、数字、点、横线和下划线")
    normalized_name = display_name.strip()
    if not normalized_name or len(normalized_name) > 200:
        raise DeviceProvisioningError("设备显示名称不能为空且不能超过 200 字符")
    requested_uuid = (
        _canonical_uuid(instance_uuid, "instance UUID") if instance_uuid else None
    )
    try:
        acquisition = load_cached_community_package(
            cache_key=cache_key,
            working_dir=working_dir,
        )
        definition = device_definition_from_catalog(
            acquisition.catalog,
            definition_fqid,
        )
        normalized_configuration = validate_configuration_for_definition(
            definition,
            configuration,
        )
        configuration_schema = configuration_schema_for_definition(definition)
    except (CommunityPackageError, DevicePackageError, OSError, ValueError) as exc:
        raise DeviceProvisioningError(str(exc)) from exc
    path, graph, original = _read_graph(graph_path)
    matches = [node for node in graph["nodes"] if node.get("id") == instance_id]
    if len(matches) > 1:
        raise DeviceProvisioningError(f"设备图存在重复实例 ID: {instance_id}")
    if update_existing and not matches:
        raise DeviceProvisioningError(f"设备图不存在待更新实例: {instance_id}")
    if matches:
        existing = matches[0]
        existing_uuid = str(existing.get("uuid") or "")
        if str(existing.get("class") or "") != definition_fqid:
            raise DeviceProvisioningError("同名设备实例绑定了不同 definition")
        if requested_uuid and existing_uuid != requested_uuid:
            raise DeviceProvisioningError("同名设备实例 UUID 与请求不一致")
        try:
            protected_configuration = protect_device_configuration(
                normalized_configuration,
                configuration_schema,
                working_dir=working_dir,
                existing_configuration=(
                    existing.get("config")
                    if isinstance(existing.get("config"), Mapping)
                    else None
                ),
            )
        except DeviceSecretError as exc:
            raise DeviceProvisioningError(str(exc)) from exc
        candidate = _device_node(
            instance_id=instance_id,
            instance_uuid=existing_uuid,
            display_name=normalized_name,
            definition_fqid=definition_fqid,
            cache_key=cache_key,
            configuration=protected_configuration,
        )
        if not update_existing and existing != candidate:
            raise DeviceProvisioningError("同名设备实例已存在且内容不同")
        if existing == candidate:
            return DeviceGraphMutationResult(
                status="graph_staged",
                instance_id=instance_id,
                instance_uuid=existing_uuid,
                definition_fqid=definition_fqid,
                graph_fingerprint=_graph_fingerprint(graph),
                backup_path=None,
                changed=False,
            )
        existing.clear()
        existing.update(candidate)
        stable_uuid = existing_uuid
    else:
        stable_uuid = requested_uuid or str(uuid4())
        if any(str(node.get("uuid") or "") == stable_uuid for node in graph["nodes"]):
            raise DeviceProvisioningError("设备实例 UUID 已被其他节点使用")
        try:
            protected_configuration = protect_device_configuration(
                normalized_configuration,
                configuration_schema,
                working_dir=working_dir,
            )
        except DeviceSecretError as exc:
            raise DeviceProvisioningError(str(exc)) from exc
        graph["nodes"].append(
            _device_node(
                instance_id=instance_id,
                instance_uuid=stable_uuid,
                display_name=normalized_name,
                definition_fqid=definition_fqid,
                cache_key=cache_key,
                configuration=protected_configuration,
            )
        )
    backup = _commit_graph(path, graph, original)
    return DeviceGraphMutationResult(
        status="graph_staged",
        instance_id=instance_id,
        instance_uuid=stable_uuid,
        definition_fqid=definition_fqid,
        graph_fingerprint=_graph_fingerprint(graph),
        backup_path=str(backup),
        changed=True,
    )


def _device_node(
    *,
    instance_id: str,
    instance_uuid: str,
    display_name: str,
    definition_fqid: str,
    cache_key: str,
    configuration: Mapping[str, Any],
) -> dict[str, Any]:
    """生成当前 OS Graph 启动链路可直接加载的规范根设备节点。"""

    return {
        "id": instance_id,
        "uuid": instance_uuid,
        "name": display_name,
        "children": [],
        "parent": None,
        "type": "device",
        "class": definition_fqid,
        "position": {"x": 0, "y": 0, "z": 0},
        "config": dict(configuration),
        "data": {},
        "extra": {
            "unilab": {
                "package_cache_key": cache_key,
                "definition_fqid": definition_fqid,
            }
        },
    }


def _read_graph(
    graph_path: str | Path,
) -> tuple[Path, dict[str, Any], bytes]:
    """读取并结构校验一个非 symlink 的 node-link JSON 设备图。"""

    path = Path(graph_path).expanduser().absolute()
    if path.is_symlink() or not path.is_file():
        raise DeviceProvisioningError("设备图必须是存在的普通 JSON 文件")
    original = path.read_bytes()
    return path, _decode_graph(original, path), original


def _decode_graph(payload: bytes, source: Path) -> dict[str, Any]:
    """解码设备图并拒绝旧嵌套格式、重复身份和无效连接集合。"""

    try:
        raw = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeviceProvisioningError(f"设备图不是合法 UTF-8 JSON: {source}") from exc
    if not isinstance(raw, dict):
        raise DeviceProvisioningError("设备图根必须是 JSON object")
    if raw and "nodes" not in raw and "links" not in raw and "edges" not in raw:
        raise DeviceProvisioningError("设备图不是受支持的 node-link JSON 格式")
    nodes = raw.setdefault("nodes", [])
    link_key = _graph_link_key(raw)
    links = raw.setdefault(link_key, [])
    if not isinstance(nodes, list) or not all(isinstance(node, dict) for node in nodes):
        raise DeviceProvisioningError("设备图 nodes 必须是 object 数组")
    if not isinstance(links, list) or not all(isinstance(link, dict) for link in links):
        raise DeviceProvisioningError("设备图 links/edges 必须是 object 数组")
    ids = [str(node.get("id") or "") for node in nodes]
    if any(not item for item in ids) or len(ids) != len(set(ids)):
        raise DeviceProvisioningError("设备图节点 ID 不能为空或重复")
    uuids = [str(node.get("uuid") or "") for node in nodes if node.get("uuid")]
    if len(uuids) != len(set(uuids)):
        raise DeviceProvisioningError("设备图节点 UUID 重复")
    for value in uuids:
        _canonical_uuid(value, "设备图节点 UUID")
    return raw


def _graph_link_key(graph: Mapping[str, Any]) -> str:
    """选择并校验设备图唯一连接集合字段。"""

    if "links" in graph and "edges" in graph:
        raise DeviceProvisioningError("设备图不能同时包含 links 和 edges")
    return "edges" if "edges" in graph else "links"


def _commit_graph(path: Path, graph: dict[str, Any], original: bytes) -> Path:
    """先持久化可恢复备份，再在同目录原子替换设备图并同步目录。"""

    backup = _write_backup(path, original, _decode_graph(original, path))
    _atomic_replace(path, _encode_graph(graph), path.stat().st_mode)
    return backup


def _write_backup(path: Path, payload: bytes, graph: Mapping[str, Any]) -> Path:
    """按原图指纹创建不可覆盖的同目录备份并完成 fsync。"""

    fingerprint = _graph_fingerprint(graph).removeprefix("sha256:")[:16]
    backup = path.with_name(f"{path.name}.unilab-backup-{fingerprint}.json")
    if backup.exists():
        if backup.is_symlink() or backup.read_bytes() != payload:
            raise DeviceProvisioningError("设备图备份身份冲突")
        return backup
    try:
        with backup.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(path.parent)
    except OSError as exc:
        backup.unlink(missing_ok=True)
        raise DeviceProvisioningError(f"设备图备份失败: {exc}") from exc
    return backup


def _atomic_replace(path: Path, payload: bytes, original_mode: int) -> None:
    """使用同目录临时文件、文件 fsync 和目录 fsync 原子替换目标图。"""

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, stat.S_IMODE(original_mode))
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise DeviceProvisioningError(f"设备图原子写入失败: {exc}") from exc


def _fsync_directory(directory: Path) -> None:
    """同步包含设备图或备份的目录项，缩小断电丢失窗口。"""

    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _encode_graph(graph: Mapping[str, Any]) -> bytes:
    """把设备图编码为稳定、可审阅且以换行结尾的 UTF-8 JSON。"""

    return (
        json.dumps(graph, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    ).encode("utf-8")


def _graph_fingerprint(graph: Mapping[str, Any]) -> str:
    """计算设备图规范 JSON 的稳定 SHA-256 指纹。"""

    canonical = json.dumps(
        graph,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _canonical_uuid(value: str | None, label: str) -> str:
    """解析并返回规范 UUID 字符串，拒绝空值和非法身份。"""

    try:
        return str(UUID(str(value or "")))
    except ValueError as exc:
        raise DeviceProvisioningError(f"{label} 无效") from exc


__all__ = [
    "DeviceGraphMutationResult",
    "DeviceProvisioningError",
    "remove_device_instance",
    "restore_device_graph",
    "stage_device_instance",
    "update_device_instance",
]
