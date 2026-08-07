"""集中解析 UniLab-OS 本地运行时 SQLite 存储路径。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RuntimeStoragePaths:
    """一次 Edge 启动使用的本地权威与投影存储路径。"""

    inventory_db: str
    device_state_db: str
    workflow_history_db: str


def _resolve_database_path(
    configured_path: Any,
    *,
    default_path: Path,
    allow_off: bool,
) -> str:
    """解析一个 SQLite 路径，同时保留设备状态与历史存储的关闭哨兵。

    参数：``configured_path`` 是可选显式命令行路径；``default_path`` 是统一运行
    目录内的默认文件；``allow_off`` 表示是否接受 ``off``。返回：绝对数据库路径
    或原样 ``off``。异常：显式值不是字符串时抛出 ``TypeError``。
    """

    if configured_path is None:
        return str(default_path)
    if not isinstance(configured_path, str):
        raise TypeError("运行时数据库路径必须是字符串")
    explicit_path = configured_path.strip()
    if not explicit_path:
        return str(default_path)
    if allow_off and explicit_path.lower() == "off":
        return "off"
    return str(Path(explicit_path).expanduser().resolve())


def resolve_runtime_storage_paths(
    arguments: dict[str, Any],
    *,
    working_dir: str,
) -> RuntimeStoragePaths:
    """以唯一运行目录补全本地库存、设备状态和工作流历史路径。

    参数：``arguments`` 是公共命令行参数投影；``working_dir`` 是主进程最终采用
    的绝对工作目录。返回：本轮启动唯一的运行时存储路径
    （RuntimeStoragePaths），并同步回参数字典。异常：参数形状、工作目录或显式
    数据库路径无效时抛出 ``TypeError``/``ValueError``。

    未显式覆盖的存储都位于 ``working_dir``。显式绝对/相对路径继续兼容，但不会
    再继承遗留 ``~/.unilabos/*.db``；解析过程只计算路径，不创建、覆盖或删除任何
    库存权威（Inventory Authority）文件。
    """

    if not isinstance(arguments, dict):
        raise TypeError("启动参数必须是 dict")
    if not isinstance(working_dir, str) or not working_dir.strip():
        raise ValueError("working_dir 必须是非空路径")

    # ``runtime_root`` 是本轮三类持久事实的共同目录边界，不承担数据库迁移。
    runtime_root = Path(working_dir).expanduser().resolve()
    resolved = RuntimeStoragePaths(
        inventory_db=_resolve_database_path(
            arguments.get("edge_inventory_db"),
            default_path=runtime_root / "inventory.db",
            allow_off=False,
        ),
        device_state_db=_resolve_database_path(
            arguments.get("edge_device_state_db"),
            default_path=runtime_root / "device_state.db",
            allow_off=True,
        ),
        workflow_history_db=_resolve_database_path(
            arguments.get("edge_workflow_history_db"),
            default_path=runtime_root / "workflow_history.db",
            allow_off=True,
        ),
    )
    arguments["edge_inventory_db"] = resolved.inventory_db
    arguments["edge_device_state_db"] = resolved.device_state_db
    arguments["edge_workflow_history_db"] = resolved.workflow_history_db
    return resolved


__all__ = ["RuntimeStoragePaths", "resolve_runtime_storage_paths"]
