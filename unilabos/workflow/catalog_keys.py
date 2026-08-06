"""Workflow Template Catalog 业务键的唯一规范化规则。"""

from __future__ import annotations


def normalize_catalog_business_name(value: str) -> str:
    """与 Catalog import、lookup 和旧库审计共享 Unicode 规范化。"""

    return value.strip().lower()


__all__ = ["normalize_catalog_business_name"]
