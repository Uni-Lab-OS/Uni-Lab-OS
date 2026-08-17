"""组合工作流（CompositeWorkflow）静态编译的 JSON 值规范化内核。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from unilabos.workflow.models import validate_uuid


class CompositeFailure(RuntimeError):
    """把内部失败收敛成稳定诊断而不泄漏快照内容。"""

    def __init__(self, code: str, path: str) -> None:
        """保存公共错误码和 JSON Pointer 路径。"""

        self.code = code
        self.path = path
        super().__init__(code)


def canonical_uuid(value: Any, code: str, path: str) -> str:
    """校验规范 UUID 并把失败映射为稳定组合诊断。"""

    try:
        identity = validate_uuid(value)
    except (TypeError, ValueError):
        raise CompositeFailure(code, path) from None
    if identity != value:
        raise CompositeFailure(code, path)
    return identity


def plain_mapping(value: Any, path: str) -> dict[str, Any]:
    """复制必填 JSON 对象并在形状非法时关闭失败。"""

    if not isinstance(value, Mapping):
        raise CompositeFailure("composite_boundary_mapping_invalid", path)
    return plain(value)


def plain_sequence(value: Any, path: str) -> list[Any]:
    """复制必填 JSON 数组并在形状非法时关闭失败。"""

    if not isinstance(value, list):
        raise CompositeFailure("composite_boundary_mapping_invalid", path)
    return plain(value)


def schema_object(value: Any) -> dict[str, Any]:
    """把目录中的对象或 JSON 文本 Schema 规范为分离字典。"""

    if isinstance(value, Mapping):
        return plain(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            decoded = None
        if isinstance(decoded, dict):
            return decoded
    raise CompositeFailure("composite_catalog_mismatch", "/catalog/schema")


def plain(value: Any) -> Any:
    """递归复制冻结映射和元组为普通 JSON 容器。"""

    if isinstance(value, Mapping):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


__all__ = [
    "CompositeFailure",
    "canonical_uuid",
    "plain",
    "plain_mapping",
    "plain_sequence",
    "schema_object",
]
