"""集中解析 UniLab-OS 本地运行时 SQLite 存储路径。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RuntimeStoragePaths:
    """一次启动使用的三类本地权威/投影存储路径。"""

    inventory_db: str
    device_state_db: str
    workflow_history_db: str


@dataclass(frozen=True)
class WorkingDirectoryResolution:
    """一次启动解析得到的可写运行目录及遗留目录命中状态。"""

    # ``path`` 是最终采用的绝对运行目录。
    path: str
    # ``used_legacy_directory`` 表示解析器自动复用了旧 ``unilabos_data``。
    used_legacy_directory: bool


_DEFAULT_WORKING_DIRECTORY_NAME = ".unilabos"
_LEGACY_WORKING_DIRECTORY_NAME = "unilabos_data"


def resolve_working_directory(
    *,
    requested: str | None,
    config_path: str | None,
    current_directory: str | Path | None = None,
) -> WorkingDirectoryResolution:
    """解析公共启动命令使用的唯一可写运行目录。

    参数：``requested`` 是显式或工作区（Workspace）派生的 ``working_dir``；
    ``config_path`` 是可选部署配置路径；``current_directory`` 是测试可覆盖的当前
    目录。返回：规范绝对路径以及是否自动复用了旧 ``unilabos_data`` 目录。
    异常：路径参数不是字符串/路径或为空时抛出 ``TypeError``/``ValueError``。

    显式/工作区路径始终精确优先，不再隐式追加子目录。没有请求路径时，新安装
    使用隐藏的 ``.unilabos``；仅当新目录不存在且旧目录已经存在时复用旧目录，
    防止一次升级静默切换本地持久事实。
    """

    if requested is not None:
        if not isinstance(requested, str) or not requested.strip():
            raise ValueError("working_dir 必须是非空路径")
        return WorkingDirectoryResolution(
            path=os.path.abspath(os.path.expanduser(requested)),
            used_legacy_directory=False,
        )

    if current_directory is None:
        base_directory = Path.cwd()
    elif isinstance(current_directory, (str, Path)):
        base_directory = Path(current_directory).expanduser()
    else:
        raise TypeError("current_directory 必须是字符串或 Path")
    base_directory = Path(os.path.abspath(base_directory))

    has_config = bool(config_path and os.path.exists(config_path))
    if has_config:
        base_directory = Path(os.path.dirname(os.path.abspath(str(config_path))))
    if base_directory.name == _DEFAULT_WORKING_DIRECTORY_NAME:
        return WorkingDirectoryResolution(str(base_directory), False)
    if base_directory.name == _LEGACY_WORKING_DIRECTORY_NAME:
        return WorkingDirectoryResolution(str(base_directory), True)

    preferred_directory = base_directory / _DEFAULT_WORKING_DIRECTORY_NAME
    legacy_directory = base_directory / _LEGACY_WORKING_DIRECTORY_NAME
    if preferred_directory.exists():
        return WorkingDirectoryResolution(str(preferred_directory), False)
    if legacy_directory.is_dir():
        return WorkingDirectoryResolution(str(legacy_directory), True)
    if has_config:
        return WorkingDirectoryResolution(str(base_directory), False)
    return WorkingDirectoryResolution(str(preferred_directory), False)


def resolve_runtime_storage_paths(
    arguments: dict[str, Any],
    *,
    working_dir: str,
) -> RuntimeStoragePaths:
    """从唯一运行目录补全三类本地 SQLite 存储路径。

    参数：``arguments`` 是公共命令行（CLI）参数；``working_dir`` 是主进程已解析
    的绝对工作目录。
    返回：本轮启动唯一的运行时存储路径（RuntimeStoragePaths），并同步回参数字典。
    异常：参数形状或工作目录无效时抛出 ``TypeError``/``ValueError``。

    库存（Inventory）、设备状态与工作流历史不得再通过独立命令行参数分叉到不同
    目录；旧 ``unilabos_data`` 的兼容选择已由 ``resolve_working_directory`` 完成。
    """

    if not isinstance(arguments, dict):
        raise TypeError("启动参数必须是 dict")
    if not isinstance(working_dir, str) or not working_dir.strip():
        raise ValueError("working_dir 必须是非空路径")

    # ``runtime_root`` 是三类本地持久事实的唯一目录边界。
    runtime_root = Path(working_dir).expanduser().resolve()
    resolved = RuntimeStoragePaths(
        inventory_db=str(runtime_root / "inventory.db"),
        device_state_db=str(runtime_root / "device_state.db"),
        workflow_history_db=str(runtime_root / "workflow_history.db"),
    )
    arguments["edge_inventory_db"] = resolved.inventory_db
    arguments["edge_device_state_db"] = resolved.device_state_db
    arguments["edge_workflow_history_db"] = resolved.workflow_history_db
    return resolved


__all__ = [
    "RuntimeStoragePaths",
    "WorkingDirectoryResolution",
    "resolve_runtime_storage_paths",
    "resolve_working_directory",
]
