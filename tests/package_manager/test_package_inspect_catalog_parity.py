"""``package inspect`` 与软件包目录（PackageCatalog）同源合同。"""

from __future__ import annotations

import json
from pathlib import Path

from tests.package_manager.test_package_catalog_compiler import _write_package


def test_package_inspect_uses_the_same_catalog_compiler(
    tmp_path: Path,
) -> None:
    """证明 ``package inspect`` 输出与公开静态编译器完全一致。

    参数：``tmp_path`` 提供隔离软件包和输出目录。
    返回：无；断言设备定义、目录摘要和规范目录文件来自同一编译结果。
    异常：旧扫描路径产生不同定义或摘要时断言失败。
    """

    from unilabos.package_manager import (
        WorkspaceSource,
        compile_package_source,
        inspect_package,
    )

    # ``workspace_root`` 是命令行与注册表（Registry）共同读取的软件包来源。
    workspace_root = tmp_path / "workspace"
    output_root = tmp_path / "output"
    _write_package(workspace_root)
    # ``expected_catalog`` 是公共静态编译缝给出的行为参照。
    expected_catalog = compile_package_source(WorkspaceSource(workspace_root))

    # ``inspection`` 是公共 package 子命令产生的兼容输出汇总。
    inspection = inspect_package(
        str(workspace_root),
        namespace=None,
        out_dir=str(output_root),
    )
    # ``catalog_document`` 是命令落盘、可供工具读取的规范目录文档。
    catalog_document = json.loads(
        Path(inspection["catalog_path"]).read_text(encoding="utf-8")
    )

    assert sorted(inspection["devices"]) == ["reactor"]
    assert inspection["catalog_digest"] == expected_catalog.catalog_digest
    assert catalog_document == expected_catalog.to_dict()
