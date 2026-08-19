"""工作区宿主（Workspace Host）启动 OS 时的可视化与 ROS 日志默认值契约。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from unilabos.workspace_host.discovery import ensure_local_token
from unilabos.workspace_host.host import WorkspaceHost, _startup_readiness_timeout
from unilabos.workspace_host.launch import (
    _windows_dll_compatible_path_entries,
    resolve_backend_launch,
    resolve_edge_launch,
)
from unilabos.workspace_host.model import WorkspacePaths


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """创建最小可启动工作区。

    Args:
        tmp_path: pytest 为当前用例提供的隔离临时目录。

    Returns:
        包含资源图（ResourceGraph）与本地配置的工作区根目录。
    """

    root = tmp_path / "workspace"
    (root / "deployment" / "graphs").mkdir(parents=True)
    (root / "deployment" / "graphs" / "graph.json").write_text(
        "{}\n", encoding="utf-8"
    )
    (root / "deployment" / "local_config.py").write_text(
        "# fixture\n", encoding="utf-8"
    )
    return root


def test_workspace_host_scopes_slow_cold_start_to_backend_and_edge(
    workspace: Path,
) -> None:
    """证明慢冷启动只扩大 Backend/Edge，不扩大其他宿主请求超时。

    Args:
        workspace: 当前用例的最小可启动工作区。

    Returns:
        无返回值；通用超时或 Edge 冷启动期限不符合契约时断言失败。
    """

    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    host = WorkspaceHost(paths, ensure_local_token(paths))
    try:
        assert host.readiness_timeout == 90.0
        assert _startup_readiness_timeout("backend", host.readiness_timeout) == 180.0
        assert _startup_readiness_timeout("edge", host.readiness_timeout) == 180.0
        assert _startup_readiness_timeout("plc", host.readiness_timeout) == 90.0
        assert _startup_readiness_timeout("edge", 240.0) == 240.0
    finally:
        host.close()


def test_windows_dll_path_filter_excludes_inaccessible_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows ROS 2 导入不应被 PATH 中无权限的目录中断。"""

    usable = tmp_path / "usable"
    inaccessible = tmp_path / "inaccessible"
    usable.mkdir()
    inaccessible.mkdir()

    class DllDirectoryHandle:
        def close(self) -> None:
            return None

    def add_dll_directory(path: str) -> DllDirectoryHandle:
        if os.path.normcase(path) == os.path.normcase(str(inaccessible)):
            raise PermissionError(5, "拒绝访问", path)
        return DllDirectoryHandle()

    monkeypatch.setattr(os, "add_dll_directory", add_dll_directory, raising=False)

    result = _windows_dll_compatible_path_entries(
        os.pathsep.join((str(usable), str(inaccessible)))
    )

    assert result == [str(usable)]


@pytest.mark.skipif(os.name != "nt", reason="验证 Windows Conda ROS 2 环境")
def test_os_launch_disables_rviz_and_keeps_workspace_ros_logs(
    workspace: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """证明边缘运行时（Edge Runtime）默认禁用 RViz，且 ROS 2 日志不写入用户主目录。

    Args:
        workspace: 当前用例的最小可启动工作区。
        tmp_path: pytest 为当前用例提供的隔离临时目录。
        monkeypatch: pytest 提供的进程状态隔离工具。

    Returns:
        无返回值；启动参数或日志目录不满足契约时断言失败。
    """

    python_environment = tmp_path / "python-environment"
    ros_prefix = python_environment / "Library"
    (ros_prefix / "share" / "ament_index").mkdir(parents=True)
    rviz_runtime = ros_prefix / "opt" / "rviz_ogre_vendor" / "bin"
    rviz_runtime.mkdir(parents=True)
    monkeypatch.setattr(sys, "prefix", str(python_environment))

    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    ensure_local_token(paths)

    backend = resolve_backend_launch(
        paths,
        graph_path="deployment/graphs/graph.json",
        backend_port=48_301,
        hostlink_port=48_302,
    )
    edge = resolve_edge_launch(
        paths,
        {"address": backend.address, "metadata": backend.metadata},
    )

    visual_index = edge.command.index("--visual")
    assert edge.command[visual_index + 1] == "disable"

    for plan in (backend, edge):
        ros_log_dir = Path(plan.environment["ROS_LOG_DIR"])
        assert ros_log_dir == paths.logs / "ros" / plan.generation
        assert ros_log_dir.is_dir()
        ros_log_dir.relative_to(paths.root)
        assert Path(plan.environment["AMENT_PREFIX_PATH"]) == ros_prefix
        assert Path(plan.environment["ROS_ETC_DIR"]) == ros_prefix / "etc" / "ros"
        assert plan.environment["AMENT_PYTHON_EXECUTABLE"] == sys.executable
        assert str(rviz_runtime) in plan.environment["PATH"].split(os.pathsep)
