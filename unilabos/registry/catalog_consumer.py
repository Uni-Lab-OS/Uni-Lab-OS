"""PackageCatalog 的 Registry/TemplateCatalog 进程内 Adapter。

Package-specific implementation 归 ``unilabos.package_manager``；此模块只把
既有 Registry seam 保持为稳定 import path。
"""

from unilabos.package_manager.consumers import (
    action_catalog_from_package_catalog,
    register_package_catalog,
    workflow_template_imports_from_package_catalog,
)

__all__ = [
    "action_catalog_from_package_catalog",
    "register_package_catalog",
    "workflow_template_imports_from_package_catalog",
]
