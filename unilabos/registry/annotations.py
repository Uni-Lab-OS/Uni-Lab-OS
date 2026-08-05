"""工作流（Workflow）与动作（Action）源码可使用的有限注解辅助类型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

JSONScalar: TypeAlias = None | bool | int | float | str
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]


@dataclass(frozen=True, slots=True, init=False)
class AllowedResourceTemplates:
    """保留源码中的 ResourceTemplate symbol，不在注解层解析 UUID。"""

    resource_templates: tuple[object, ...]

    def __init__(self, *resource_templates: object) -> None:
        object.__setattr__(self, "resource_templates", tuple(resource_templates))


@dataclass(frozen=True, slots=True)
class MaterialLock:
    """显式声明动作输入物料占位符（ResourceSlot）不取得物料锁。"""

    free: bool

    def __post_init__(self) -> None:
        if self.free is not True:
            raise ValueError("MaterialLock 只接受显式 free=True")
