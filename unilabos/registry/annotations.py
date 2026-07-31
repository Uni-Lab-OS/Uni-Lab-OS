"""Workflow 与 Action 源码可使用的有限注解辅助类型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

JSONScalar: TypeAlias = None | bool | int | float | str
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]


@dataclass(frozen=True, slots=True, init=False)
class AllowedResourceTemplates:
    """保留源码中的 ResourceTemplate symbol，不在注解层解析 UUID。"""

    resource_templates: tuple[object, ...]

    def __init__(self, *resource_templates: object):
        object.__setattr__(self, "resource_templates", tuple(resource_templates))
