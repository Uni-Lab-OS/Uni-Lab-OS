"""跨 ROS 发行版的 GetCartesianPath 请求字段适配器。"""

from __future__ import annotations

from typing import Any


class CartesianPathFields:
    """隐藏 Humble 与新发行版笛卡尔请求字段布局差异。"""

    def __init__(self, request: Any) -> None:
        """绑定单个可复用的 ``GetCartesianPath.Request``。"""

        self._request = request

    def get(self, field: str, default: float | bool = 0.0) -> Any:
        """读取顶层或嵌套请求字段，缺失时返回显式缺省值。"""

        owner = self.owner(field)
        return default if owner is None else getattr(owner, field)

    def set(self, field: str, value: object) -> None:
        """写入消息实际暴露的全部同名字段，完全缺失时失败关闭。"""

        written = False
        if hasattr(self._request, field):
            setattr(self._request, field, value)
            written = True
        nested = getattr(self._request, "request", None)
        if nested is not None and hasattr(nested, field):
            setattr(nested, field, value)
            written = True
        if not written:
            raise AttributeError(f"GetCartesianPath.Request 没有字段 {field}")

    def owner(self, field: str) -> object | None:
        """返回真实持有字段的顶层或嵌套请求对象。"""

        if hasattr(self._request, field):
            return self._request
        nested = getattr(self._request, "request", None)
        if nested is not None and hasattr(nested, field):
            return nested
        return None

    def revolute_jump_threshold(self) -> float:
        """读取新字段，旧发行版则回退到统一 ``jump_threshold``。"""

        owner = self.owner("revolute_jump_threshold")
        if owner is not None:
            return float(getattr(owner, "revolute_jump_threshold"))
        return float(self.get("jump_threshold", 0.0))

    def set_revolute_jump_threshold(self, value: float) -> None:
        """写入新字段，旧发行版则写入统一 ``jump_threshold``。"""

        if self.owner("revolute_jump_threshold") is not None:
            self.set("revolute_jump_threshold", value)
            return
        self.set("jump_threshold", value)


__all__ = ["CartesianPathFields"]
