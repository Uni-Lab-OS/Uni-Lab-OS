"""Workflow Python 作者语法的静态标记。

Authoring engine 只读取这些名字的 AST identity，绝不执行本模块或用户工作流。
这里的轻量运行时对象只服务于 Python 语言工具、文档示例和显式的误用提示；它们不
构成第二个工作流执行器。
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, TypeVar

WorkflowFunction = TypeVar("WorkflowFunction", bound=Callable[..., Any])


class MaterialFlowRole(str, Enum):
    """MaterialSource 的闭合物料角色 wire value。"""

    PRIMARY_SAMPLE = "primary_sample"
    ALIQUOT_SAMPLE = "aliquot_sample"
    REAGENT = "reagent"
    CONSUMABLE = "consumable"


MATERIAL_FLOW_ROLE_LABELS_ZH = MappingProxyType(
    {
        "primary_sample": "主样品",
        "aliquot_sample": "分装样品",
        "reagent": "试剂",
        "consumable": "耗材",
    }
)


def workflow_definition(
    *,
    workflow_uuid: str,
    displayname: str,
    description: str | None = None,
) -> Callable[[WorkflowFunction], WorkflowFunction]:
    """为静态编译器标记唯一 Workflow 函数。"""

    del workflow_uuid, displayname, description

    def decorate(function: WorkflowFunction) -> WorkflowFunction:
        return function

    return decorate


class _ResultView:
    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(f"Workflow authoring result {name!r} 只能由静态编译器解析")


class _DeviceSelector:
    def __getattr__(self, action: str) -> Callable[..., _ResultView]:
        def compile_only_action(**_kwargs: Any) -> _ResultView:
            raise RuntimeError(f"Workflow authoring action {action!r} 不能直接执行")

        return compile_only_action


def device(device_id: str | None = None) -> Any:
    """标记一个按 annotation 解析的设备模板 selector。"""

    del device_id
    return _DeviceSelector()


def resource_ref(resource_id: str) -> Any:
    """标记一个 compile-only ResourceSlot 引用，可用于 mount 或 Action 参数。"""

    del resource_id
    raise RuntimeError("Workflow authoring resource_ref() 只能由静态编译器解析")


def material_source(
    *,
    resource_template: Any,
    mode: str,
    mount: Any,
    material_uuid: str | None,
    site: str | None,
    slot_range: list[str] | None,
    flow_role: MaterialFlowRole,
) -> Any:
    """标记一个 compile-only MaterialSource selector。"""

    del resource_template, mode, mount, material_uuid, site, slot_range, flow_role
    raise RuntimeError("Workflow authoring material_source() 只能由静态编译器解析")


@dataclass(frozen=True, slots=True)
class _CompileOnlyBlock(AbstractContextManager[None]):
    name: str

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_args: object) -> None:
        return None


def group(*, name: str) -> AbstractContextManager[None]:
    """标记一个真实 Backend presentation group。"""

    return _CompileOnlyBlock(name=name)


def parallel() -> AbstractContextManager[None]:
    """标记一个不产生 Fork/Join Node 的 source-only parallel block。"""

    return _CompileOnlyBlock(name="parallel")


def workflow_output(**values: Any) -> dict[str, Any]:
    """标记根 Workflow 的显式命名输出。"""

    return dict(values)


__all__ = [
    "MATERIAL_FLOW_ROLE_LABELS_ZH",
    "MaterialFlowRole",
    "device",
    "group",
    "material_source",
    "parallel",
    "resource_ref",
    "workflow_definition",
    "workflow_output",
]
