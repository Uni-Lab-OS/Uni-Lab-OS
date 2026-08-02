"""Authoring 源码的一基 UTF-16 坐标。

公开 SourceRange 与 Monaco/JavaScript 使用同一列单位。CPython AST 的 UTF-8 byte
offset 和 tokenize/SyntaxError 的 code-point offset 都必须先经过这里转换。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

_LINE_BREAK = re.compile(r"\r\n|\r|\n")


def require_utf8_text(value: str) -> str:
    """验证字符串可以无损编码为 UTF-8，并原样返回。"""

    if not isinstance(value, str):
        raise TypeError("source text must be a string")
    value.encode("utf-8", errors="strict")
    return value


def source_lines(source: str) -> list[str]:
    """按 Python 通用换行边界返回包含末尾空行的逻辑行。"""

    require_utf8_text(source)
    return _LINE_BREAK.split(source)


def utf16_length(value: str) -> int:
    """返回一个合法 Unicode 字符串占用的 UTF-16 code unit 数。"""

    require_utf8_text(value)
    return len(value.encode("utf-16-le")) // 2


def codepoint_offset_to_utf16_column(line: str, offset: int) -> int:
    """把零基 code-point offset 转成一基 UTF-16 column。"""

    require_utf8_text(line)
    if type(offset) is not int or not 0 <= offset <= len(line):
        raise ValueError("code-point offset is outside the source line")
    return utf16_length(line[:offset]) + 1


def utf8_offset_to_utf16_column(line: str, offset: int) -> int:
    """把 CPython AST 的零基 UTF-8 byte offset 转成公开 column。"""

    encoded = require_utf8_text(line).encode("utf-8")
    if type(offset) is not int or not 0 <= offset <= len(encoded):
        raise ValueError("UTF-8 offset is outside the source line")
    try:
        prefix = encoded[:offset].decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise ValueError("UTF-8 offset splits one source character") from None
    return utf16_length(prefix) + 1


def source_ranges_fit(
    source: str,
    ranges: Iterable[Mapping[str, Any]],
) -> bool:
    """验证一组一基 UTF-16 range 端点位于源码内。"""

    try:
        lines = source_lines(source)
        line_lengths = [utf16_length(line) for line in lines]

        def position_fits(line: Any, column: Any) -> bool:
            return (
                type(line) is int
                and type(column) is int
                and 1 <= line <= len(line_lengths)
                and 1 <= column <= line_lengths[line - 1] + 1
            )

        for item in ranges:
            if not isinstance(item, Mapping):
                return False
            if not (
                position_fits(item.get("start_line"), item.get("start_column"))
                and position_fits(item.get("end_line"), item.get("end_column"))
            ):
                return False
        return True
    except (TypeError, UnicodeError, ValueError):
        return False


__all__ = [
    "codepoint_offset_to_utf16_column",
    "require_utf8_text",
    "source_lines",
    "source_ranges_fit",
    "utf8_offset_to_utf16_column",
    "utf16_length",
]
