"""工作流作者源码可导入的静态标记（Authoring Markers）。

创作编译器只读取这些调用的 AST（抽象语法树），不会导入或执行作者源码。
这里的运行时对象仅让编辑器和类型检查器可以解析名称，不承担调度语义。
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

_Function = TypeVar("_Function", bound=Callable[..., Any])


@dataclass(frozen=True, slots=True)
class DeviceSelector:
    """作者源码中的设备选择声明，仅保存可选固定设备身份。"""

    device_id: str | None = None

    def __getattr__(self, action_name: str) -> Callable[..., Any]:
        """为编辑器返回一个不可执行的动作占位调用。

        参数说明：``action_name`` 是作者访问的动作业务名；返回函数一旦被真正
        调用就抛出错误，防止误把作者标记当成设备执行接口。
        """

        def unavailable_action(*_args: Any, **_kwargs: Any) -> Any:
            """拒绝在 Python 运行时执行创作动作；参数仅用于兼容调用形状。"""

            raise RuntimeError("工作流作者动作只能由创作编译器静态解析")

        unavailable_action.__name__ = action_name
        return unavailable_action


@dataclass(frozen=True, slots=True)
class _AuthoringBlock(AbstractContextManager[None]):
    """仅供类型检查使用的 group/parallel 上下文标记。"""

    def __enter__(self) -> None:
        """进入静态创作块；运行时不产生领域状态。"""

        return None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: Any,
    ) -> bool:
        """退出静态创作块。

        参数说明：三个参数是上下文管理器异常信息；始终返回 ``False``，不吞掉
        运行时异常。
        """

        del exc_type, exc_value, traceback
        return False


def workflow_definition(**metadata: Any) -> Callable[[_Function], _Function]:
    """声明工作流定义（Workflow Definition）的静态装饰器。

    参数说明：``metadata`` 包含工作流 UUID、展示名和描述；返回保持原函数不变
    的装饰器。可信编译只读取 AST，不信任这里的运行结果。
    """

    del metadata

    def decorate(function: _Function) -> _Function:
        """返回原作者函数；``function`` 仅供编辑器和类型检查器使用。"""

        return function

    return decorate


def device(device_id: str | None = None) -> DeviceSelector:
    """声明一个设备选择器（Device Selector）。

    参数说明：``device_id`` 缺失表示运行时分配，非空字符串表示固定设备；返回
    仅供静态创作的选择器对象。
    """

    if device_id is not None and (not isinstance(device_id, str) or not device_id):
        raise ValueError("固定设备身份必须是非空字符串")
    return DeviceSelector(device_id)


def group(*, name: str) -> AbstractContextManager[None]:
    """声明展示分组（Group）。

    参数说明：``name`` 是分组展示名；返回不承载执行屏障的静态上下文标记。
    """

    if not isinstance(name, str) or not name.strip():
        raise ValueError("分组名称不能为空")
    return _AuthoringBlock()


def parallel() -> AbstractContextManager[None]:
    """声明源码并行结构（Parallel）；返回静态上下文标记。"""

    return _AuthoringBlock()


def workflow_output(**outputs: Any) -> dict[str, Any]:
    """声明工作流输出（Workflow Output）。

    参数说明：``outputs`` 把输出名绑定到工作流输入或节点结果；返回浅拷贝仅供
    编辑器体验，可信编译器静态读取关键字参数。
    """

    return dict(outputs)


__all__ = [
    "DeviceSelector",
    "device",
    "group",
    "parallel",
    "workflow_definition",
    "workflow_output",
]
