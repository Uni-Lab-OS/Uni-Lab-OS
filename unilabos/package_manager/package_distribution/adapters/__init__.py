"""包分发（Package Distribution）的现有外部系统 Adapter。"""

from .cloud import (
    HttpClientPublicationAdapter,
    PackageBuilder,
    PublicationPort,
    publish_build,
    upload_package,
)
from .directory import install_package
from .legacy_backend import LEGACY_CAPABILITY, LegacyTemplateBackendAdapter

__all__ = [
    "LEGACY_CAPABILITY",
    "HttpClientPublicationAdapter",
    "LegacyTemplateBackendAdapter",
    "PackageBuilder",
    "PublicationPort",
    "install_package",
    "publish_build",
    "upload_package",
]
