"""OS 本地调度器（Scheduler）的稳定任务排序接口。

入参 = ready tasks + 资源锁状态 + 优先级；出参 = 有序 task 列表。
``StableLocalOrderer`` 按权重降序、提交时间升序和稳定节点身份排序；OS 不调用
独立 ``uni-lab-scheduler`` 服务。
"""

from __future__ import annotations

from typing import List, Protocol, Set

from unilabos.app.scheduler.models import ReadyTask


class OrderingContext:
    """一次重排的资源上下文。"""

    def __init__(self, busy_device_action_keys: Set[str]):
        # 当前被占用的 device_action_key（已下发未完结 job 持有的锁）
        self.busy_device_action_keys = busy_device_action_keys


class TaskOrderer(Protocol):
    def order(self, ready: List[ReadyTask], ctx: OrderingContext) -> List[ReadyTask]:
        """返回下发顺序（可含全部 ready；service 层负责跳过锁忙的节点）。"""
        ...


class StableLocalOrderer:
    """稳定排序 stub：权重降序 → 提交时间升序 → workflow_id/node id 字典序。"""

    def order(self, ready: List[ReadyTask], ctx: OrderingContext) -> List[ReadyTask]:
        return sorted(
            ready,
            key=lambda t: (
                -t.priority_weight,
                t.submitted_at,
                t.workflow_id,
                t.node.id,
            ),
        )


__all__ = [
    "OrderingContext",
    "StableLocalOrderer",
    "TaskOrderer",
]
