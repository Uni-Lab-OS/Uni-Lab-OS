"""QG01 工作区（Workspace）物料外形资产投影合同。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from unilabos.package_manager import (
    WorkspaceSource,
    compile_workspace_material_shapes,
    compile_workspace_startup,
)


class _Registry:
    """提供工作区设备与资源装饰器静态定义的注册表（Registry）替身。"""

    def __init__(self, definitions: list[dict[str, Any]]) -> None:
        """保存本轮测试要投影的装饰器定义。

        参数：``definitions`` 是带声明文件路径和模型字段的设备定义。
        返回：无；读取方法返回隔离的列表副本。
        异常：无。
        """

        # ``definitions`` 只表达已由注册表（Registry）静态发现的包内定义。
        self.definitions = definitions

    def obtain_registry_device_info(self) -> list[dict[str, Any]]:
        """返回已静态发现的设备定义。

        参数：无。返回：测试控制的设备定义副本。异常：无。
        """

        return list(self.definitions)

    def obtain_registry_resource_info(self) -> list[dict[str, Any]]:
        """返回空资源定义集合。

        参数：无。返回：空列表。异常：无。
        """

        return []


def _write_workspace(workspace_root: Path) -> Path:
    """建立含一个显式外形声明和一个未绑定文件的最小工作区。

    参数：``workspace_root`` 是测试授权的工作区根。
    返回：设备装饰器声明文件路径。
    异常：文件系统写入失败时原样抛出。
    """

    # ``package_root`` 是本轮唯一允许发现定义和资产的导入包根。
    package_root = workspace_root / "szlab_poly_studio"
    # ``declaration_file`` 是外形资产相对路径的唯一解析基准。
    declaration_file = package_root / "devices" / "pump" / "device.py"
    declared_shape = declaration_file.parent / "models" / "shape.yml"
    undeclared_shape = package_root / "devices" / "hidden" / "models" / "shape.yml"
    declared_shape.parent.mkdir(parents=True)
    undeclared_shape.parent.mkdir(parents=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    declaration_file.write_text("# static decorator declaration\n", encoding="utf-8")
    (workspace_root / "pyproject.toml").write_text(
        '[project]\nname = "szlab-poly-studio"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    declared_shape.write_text(
        "schema_version: 1\n"
        "shape:\n"
        "  id: pump\n"
        "  display_name: 泵\n"
        "  applies_to:\n"
        "    - category: gantry_pump\n"
        "  envelope: [100, 80, 60]\n"
        "  parts:\n"
        "    - type: box\n"
        "      style: body\n"
        "      from: [0, 0, 0]\n"
        "      to: [100, 80, 60]\n",
        encoding="utf-8",
    )
    undeclared_shape.write_text(
        "schema_version: 1\n"
        "shape:\n"
        "  id: hidden\n"
        "  applies_to: [{category: hidden}]\n"
        "  parts: [{type: box, from: [0, 0, 0], to: [1, 1, 1]}]\n",
        encoding="utf-8",
    )
    return declaration_file


def test_workspace_shapes_follow_decorator_binding_and_public_wire_contract(
    tmp_path: Path,
) -> None:
    """外形投影必须只读取装饰器绑定文件并生成前端公共 wire 数据。

    参数：``tmp_path`` 隔离显式工作区及其外形文件。
    返回：无；断言未绑定文件不被目录扫描器隐式发现。
    异常：投影缺失、读取越界或 wire 形状错误时测试失败。
    """

    workspace_root = tmp_path / "workspace"
    declaration_file = _write_workspace(workspace_root)
    # ``startup_plan`` 固定发行包身份和工作区文件授权边界。
    startup_plan = compile_workspace_startup(WorkspaceSource(workspace_root))
    # ``registry`` 只发布一个装饰器绑定，未绑定 hidden shape 不得进入结果。
    registry = _Registry(
        [
            {
                "id": "szlab_mixer_pump",
                "file_path": str(declaration_file),
                "model": {
                    "shape": {
                        "format": "unilab.shape/v1",
                        "entry": "models/shape.yml",
                    }
                },
            }
        ]
    )

    shapes = compile_workspace_material_shapes(startup_plan, registry)

    assert shapes == (
        {
            "id": "pump",
            "bundle": "szlab-poly-studio",
            "displayName": "泵",
            "categories": ["gantry-pump"],
            "categoryTokens": [],
            "priority": 0,
            "envelope": [100.0, 80.0, 60.0],
            "units": "mm",
            "shadow": "box",
            "sort": "center",
            "parts": [
                {
                    "type": "box",
                    "style": "body",
                    "from": [0, 0, 0],
                    "to": [100, 80, 60],
                }
            ],
        },
    )


def test_workspace_shape_entry_cannot_escape_declaring_package(
    tmp_path: Path,
) -> None:
    """装饰器外形入口不得通过父目录段逃出声明文件所在包。

    参数：``tmp_path`` 隔离工作区和潜在越界目标。
    返回：无；断言编译关闭失败。
    异常：实现接受越界路径时测试失败。
    """

    workspace_root = tmp_path / "workspace"
    declaration_file = _write_workspace(workspace_root)
    startup_plan = compile_workspace_startup(WorkspaceSource(workspace_root))
    registry = _Registry(
        [
            {
                "id": "unsafe_pump",
                "file_path": str(declaration_file),
                "model": {
                    "shape": {
                        "format": "unilab.shape/v1",
                        "entry": "../../../../outside.yml",
                    }
                },
            }
        ]
    )

    with pytest.raises(ValueError, match="外形资产入口"):
        compile_workspace_material_shapes(startup_plan, registry)
