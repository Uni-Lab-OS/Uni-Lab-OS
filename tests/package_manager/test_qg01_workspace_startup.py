"""QG01 工作区（Workspace）公共启动合同测试。"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def _workspace_api() -> ModuleType:
    """读取本轮工作区（Workspace）公开模块并报告清晰 RED。

    参数：无。
    返回：同时公开 ``WorkspaceSource`` 与 ``prepare_workspace_startup`` 的模块。
    异常：模块或任一公开成员尚未实现时，以测试失败说明缺失合同。
    """

    try:
        workspace_module = importlib.import_module("unilabos.package_manager")
    except ModuleNotFoundError:
        pytest.fail("QG01 缺少 unilabos.package_manager", pytrace=False)
    missing_members = [
        member_name
        for member_name in ("WorkspaceSource", "prepare_workspace_startup")
        if not hasattr(workspace_module, member_name)
    ]
    if missing_members:
        pytest.fail(
            "QG01 缺少工作区公开成员: " + ", ".join(missing_members),
            pytrace=False,
        )
    return workspace_module


def _write_szlab_shaped_workspace(workspace_root: Path) -> dict[str, Path]:
    """创建包含设备、工作流源码和图的最小 SZLab 形状工作区。

    参数：``workspace_root`` 是测试显式授权的工作区根目录。
    返回：按 ``package``、``workflow`` 与 ``graph`` 命名的测试文件路径。
    异常：文件系统写入失败时原样抛出，使测试不能使用不完整夹具。
    """

    # ``workflow_uuid`` 是测试工作流（Workflow）的稳定身份，不代表运行任务。
    workflow_uuid = "5e7ce142-bf5a-5d30-8666-fdf5374941f1"
    package_root = workspace_root / "szlab_poly_studio"
    workflow_path = package_root / "workflows" / "material_transfer.py"
    graph_path = workspace_root / "deployment" / "graphs" / "szlab-local-debug.json"
    workflow_path.parent.mkdir(parents=True)
    graph_path.parent.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    workflow_path.write_text(
        "from unilabos.workflow.authoring import workflow\n"
        f'@workflow(workflow_uuid="{workflow_uuid}", displayname="物料转移")\n'
        "def material_transfer() -> None:\n"
        "    return None\n",
        encoding="utf-8",
    )
    (workspace_root / "pyproject.toml").write_text(
        "[project]\n"
        'name = "szlab-poly-studio"\n'
        'version = "0.1.0"\n',
        encoding="utf-8",
    )
    (workspace_root / "package.yaml").write_text(
        "package:\n"
        "  name: szlab_poly_studio\n"
        "workflows:\n"
        f"  - workflow_uuid: {workflow_uuid}\n"
        "    source: szlab_poly_studio/workflows/material_transfer.py\n",
        encoding="utf-8",
    )
    graph_path.write_text(
        '{"nodes":[{"id":"robot","class":'
        '"community.szlab_poly_studio.robot"}],"links":[]}',
        encoding="utf-8",
    )
    return {
        "package": package_root,
        "workflow": workflow_path,
        "graph": graph_path,
    }


def test_workspace_source_reads_only_regular_files_below_explicit_root(
    tmp_path: Path,
) -> None:
    """证明工作区来源只能读取显式根目录内的普通文件。

    参数：``tmp_path`` 隔离合法工作区与越界文件。
    返回：无；断言合法源码可读且父目录逃逸失败关闭。
    异常：若公开实现接受越界路径，测试断言失败。
    """

    workspace_api = _workspace_api()
    workspace_root = tmp_path / "workspace"
    fixture_paths = _write_szlab_shaped_workspace(workspace_root)
    source = workspace_api.WorkspaceSource(workspace_root)

    assert source.source_kind == "workspace"
    assert source.read_bytes("package.yaml") == (
        workspace_root / "package.yaml"
    ).read_bytes()
    with pytest.raises(ValueError):
        source.read_bytes("../outside.py")
    assert fixture_paths["workflow"].is_file()


def test_prepare_workspace_startup_projects_one_szlab_package_without_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """证明工作区一次投影为设备扫描、工作流授权和相对图路径。

    参数：``tmp_path`` 创建 SZLab 形状夹具；``monkeypatch`` 隔离 ``sys.path``。
    返回：无；断言只产生启动配置，不创建工作流任务（WorkflowTask）。
    异常：声明身份、目录或图路径无效时实现应抛出稳定 ``ValueError``。
    """

    workspace_api = _workspace_api()
    workspace_root = tmp_path / "workspace"
    fixture_paths = _write_szlab_shaped_workspace(workspace_root)
    monkeypatch.setattr(sys, "path", list(sys.path))
    # ``startup_arguments`` 是公共 CLI 完成解析后的启动参数投影。
    startup_arguments: dict[str, Any] = {
        "workspace": str(workspace_root),
        "devices": None,
        "workflow_editable_package_root": None,
        "graph": "deployment/graphs/szlab-local-debug.json",
    }

    startup_plan = workspace_api.prepare_workspace_startup(startup_arguments)

    assert startup_plan is not None
    assert startup_plan.import_package == "szlab_poly_studio"
    assert startup_arguments["devices"] == [str(fixture_paths["package"])]
    assert startup_arguments["workflow_editable_package_root"] == [
        str(workspace_root)
    ]
    assert startup_arguments["_community_namespaces"] == {
        str(fixture_paths["package"]): "community.szlab_poly_studio"
    }
    assert startup_arguments["graph"] == str(fixture_paths["graph"])
    assert str(workspace_root) == sys.path[0]
    assert "workflow_task" not in startup_arguments


def test_prepare_workspace_startup_rejects_parallel_legacy_device_roots(
    tmp_path: Path,
) -> None:
    """证明工作区不能与遗留 ``--devices`` 形成第二套包发现权威。

    参数：``tmp_path`` 创建合法工作区和冲突设备目录。
    返回：无；断言冲突在注册表（Registry）扫描前失败关闭。
    异常：若实现未拒绝双发现路径，测试断言失败。
    """

    workspace_api = _workspace_api()
    workspace_root = tmp_path / "workspace"
    fixture_paths = _write_szlab_shaped_workspace(workspace_root)
    # ``legacy_device_root`` 代表调用者另行提供的遗留设备扫描权威。
    legacy_device_root = tmp_path / "legacy-devices"
    legacy_device_root.mkdir()
    startup_arguments: dict[str, Any] = {
        "workspace": str(workspace_root),
        "devices": [str(legacy_device_root)],
        "workflow_editable_package_root": None,
        "graph": str(fixture_paths["graph"]),
    }

    with pytest.raises(ValueError, match="--workspace.*--devices"):
        workspace_api.prepare_workspace_startup(startup_arguments)


def test_public_cli_parser_accepts_workspace_argument(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """证明公共 ``unilab`` 解析器接受显式 ``--workspace``。

    参数：``tmp_path`` 提供不需要实际启动的参数值；``monkeypatch`` 隔离进程参数。
    返回：无；断言解析结果保留调用者声明的工作区路径。
    异常：未知参数应由 argparse 触发 ``SystemExit``，本测试会因此失败。
    """

    from unilabos.app.main import parse_args

    workspace_root = tmp_path / "workspace"
    monkeypatch.setattr(
        sys,
        "argv",
        ["unilab", "--workspace", str(workspace_root), "--skip_env_check"],
    )

    parsed_arguments = parse_args().parse_args()

    assert parsed_arguments.workspace == str(workspace_root)


def test_local_workspace_namespace_satisfies_community_graph_without_remote_package(
    tmp_path: Path,
) -> None:
    """证明 SZLab 本地命名空间阻止工作区图误走远端社区包解析。

    参数：``tmp_path`` 提供隔离工作目录和本地包目录。
    返回：无；断言已提供命名空间原样进入注册表（Registry）扫描映射。
    异常：若本地命名空间仍被判断为缺失，准备函数会抛出社区包错误。
    """

    from unilabos.app.community_packages import prepare_community_packages

    package_root = tmp_path / "szlab_poly_studio"
    package_root.mkdir()
    # ``local_namespaces`` 是设备扫描目录到社区类名前缀的唯一显式映射。
    local_namespaces = {
        str(package_root.resolve()): "community.szlab_poly_studio"
    }
    graph_document = {
        "nodes": [
            {
                "id": "robot",
                "class": "community.szlab_poly_studio.robot",
            }
        ],
        "links": [],
    }

    result = prepare_community_packages(
        graph_document,
        working_dir=tmp_path,
        available_namespaces=local_namespaces,
    )

    assert result.namespaces == local_namespaces
    assert result.devices_dirs == []

