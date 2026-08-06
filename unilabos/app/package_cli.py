"""软件包命令行（Package CLI）的过渡兼容导出。

新代码应从 ``unilabos.package_manager`` 导入；本模块仅保留一个兼容周期。
"""

from unilabos.package_manager.cli import PackageCLIError, cmd_package
from unilabos.package_manager.inspection import (
    build_action_value_mappings,
    build_archive,
    build_package_info,
    build_resources,
    build_resources_from_registry,
    discover_registry_paths_from_project,
    inspect_package,
    normalize_name,
    read_external_registry_devices,
    read_pyproject,
    read_registry_yaml_devices,
    resolve_class_namespace,
    scan_package_devices,
)
from unilabos.package_manager.installation import install_package
from unilabos.package_manager.publication import upload_package

__all__ = [
    "PackageCLIError",
    "build_action_value_mappings",
    "build_archive",
    "build_package_info",
    "build_resources",
    "build_resources_from_registry",
    "cmd_package",
    "discover_registry_paths_from_project",
    "inspect_package",
    "install_package",
    "normalize_name",
    "read_external_registry_devices",
    "read_pyproject",
    "read_registry_yaml_devices",
    "resolve_class_namespace",
    "scan_package_devices",
    "upload_package",
]
