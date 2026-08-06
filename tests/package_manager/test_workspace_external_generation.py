"""工作区输入代聚合显式外部软件包目录（PackageCatalog）的合同。"""

from __future__ import annotations

import json
from pathlib import Path

from tests.package_manager.test_package_dependency_lock import _write_package
from unilabos.package_manager import (
    PackageDependencyManager,
    prepare_workspace_registry_runtime,
)


def _prepare_external_workspace(tmp_path: Path) -> tuple[Path, Path]:
    """建立主工作区、显式锁定外部包和引用外部设备的物理图（Graph）。

    参数：``tmp_path`` 是测试隔离父目录。
    返回：主工作区根和外部包根。
    异常：文件写入、软件包编译或依赖锁发布失败时传播原异常。
    """

    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external_lab"
    _write_package(
        workspace_root,
        distribution_name="workspace-lab",
        package_name="workspace_lab",
    )
    _write_package(
        external_root,
        distribution_name="external-lab",
        package_name="external_lab",
        device_ids=("reader",),
        resource_ids=("plate",),
    )
    workspace_root.joinpath("graph.json").write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "external-reader-a",
                        "class": "community.external_lab.reader",
                        "type": "device",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    PackageDependencyManager(workspace_root).add("../external_lab")
    return workspace_root, external_root


def test_runtime_generation_aggregates_only_explicit_locked_catalogs(
    tmp_path: Path,
) -> None:
    """工作区运行代必须把主目录与显式锁定外部目录一起完整校验。

    参数：``tmp_path`` 提供主包和外部包。
    返回：无；断言外部设备/资源完整可查询，物理图（Graph）可以有限选择外部设备，
    且运行代记录依赖声明与锁的稳定摘要。
    异常：若准备路径扫描环境、漏掉锁定包或在聚合前解析物理图则测试失败。
    """

    workspace_root, _external_root = _prepare_external_workspace(tmp_path)

    runtime = prepare_workspace_registry_runtime(
        {
            "workspace": str(workspace_root),
            "graph": "graph.json",
            "devices": None,
            "workflow_editable_package_root": None,
        }
    )

    assert runtime is not None
    assert tuple(
        catalog.namespace for catalog in runtime.registry_snapshot.package_catalogs
    ) == ("community.external_lab", "community.workspace_lab")
    assert tuple(item.fqid for item in runtime.registry_snapshot.resources) == (
        "community.external_lab.plate",
    )
    assert runtime.activation_plan.selected_definition_fqids == (
        "community.external_lab.reader",
    )
    assert runtime.dependency_revision.startswith("sha256:")


def test_dependency_declaration_and_lock_bytes_belong_to_input_digest(
    tmp_path: Path,
) -> None:
    """依赖声明或锁的字节变化必须推进稳定工作区输入摘要。

    参数：``tmp_path`` 提供一对合法依赖文件。
    返回：无；断言只增加 YAML 注释也会产生新依赖观察代，同时外部目录内容不变。
    异常：运行时代摘要遗漏依赖文件时测试失败。
    """

    workspace_root, _external_root = _prepare_external_workspace(tmp_path)
    arguments = {
        "workspace": str(workspace_root),
        "graph": "graph.json",
        "devices": None,
        "workflow_editable_package_root": None,
    }
    first = prepare_workspace_registry_runtime(dict(arguments))
    declaration_path = workspace_root / "unilabos.packages.yaml"
    declaration_path.write_text(
        declaration_path.read_text(encoding="utf-8") + "# stable generation marker\n",
        encoding="utf-8",
    )
    second = prepare_workspace_registry_runtime(dict(arguments))

    assert first is not None and second is not None
    assert first.registry_snapshot.fingerprint == second.registry_snapshot.fingerprint
    assert first.dependency_revision != second.dependency_revision

