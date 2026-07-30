"""F002 TaskDagRunner 桥接层 hermetic 测试（覆盖 AC-1/AC-2/AC-5 在回调式栈上的等价）。

不连真实设备 / send_goal / ROS。用 FakeStack 建模生产回调式执行栈：
on_start_node 记录起跑并按每设备锁串行（同 device_action_key 非 always_free
串行），终态由测试**手动** finish(job_id) 经 runner.notify_terminal 跨接口回流。
验证 TaskDagRunner 把 DagExecutor(submit->awaitable) 正确接到回调/队列世界。
"""

from __future__ import annotations

import asyncio

from unilabos.scheduler.dag_model import NodeState, TaskDag
from unilabos.scheduler.debug_controller import DebugController
from unilabos.scheduler.task_dag_runner import TaskDagRunner

from tests.scheduler.fake_scheduler import settle


def _node(node_id: str, device_id: str, action: str = "a", **kw) -> dict:
    return {"node_id": node_id, "device_id": device_id, "action": action, **kw}


def _edge(src: str, tgt: str) -> dict:
    return {"source_node_uuid": src, "target_node_uuid": tgt}


def _dag(nodes, edges, task_id="t1") -> TaskDag:
    return TaskDag.from_message(
        {
            "task_id": task_id,
            "notebook_id": "nb",
            "server_info": {},
            "nodes": nodes,
            "edges": edges,
        }
    )


class FakeStack:
    """建模 ws 回调式栈：on_start_node 只记录「已起跑」，不真正阻塞。

    同 device_action_key 非 always_free 的第二个节点视为「排队」——不进入
    running，直到前一个 finish 后由本 fake 自动放行下一个（模拟 end_job 出队）。
    """

    def __init__(self, runner_getter):
        self._runner_getter = runner_getter
        self.started: list[str] = []
        self.running: set[str] = set()
        self.cancel_remaining_called = 0
        self._active_by_key: dict[str, str] = {}
        self._queue_by_key: dict[str, list[tuple[str, bool]]] = {}
        self._node_key: dict[str, str] = {}
        self._node_free: dict[str, bool] = {}

    def _key(self, node) -> str:
        return node.device_action_key

    def on_start_node(self, node) -> None:
        key = self._key(node)
        self._node_key[node.node_id] = key
        self._node_free[node.node_id] = node.always_free
        if node.always_free:
            self._run_now(node.node_id, key)
            return
        if key in self._active_by_key:
            self._queue_by_key.setdefault(key, []).append((node.node_id, False))
            return
        self._active_by_key[key] = node.node_id
        self._run_now(node.node_id, key)

    def _run_now(self, job_id: str, key: str) -> None:
        self.started.append(job_id)
        self.running.add(job_id)

    def finish(self, job_id: str, status: str = "success") -> None:
        """标记设备任务终态：回流 runner + 模拟同 key 出队放行下一个。"""
        self.running.discard(job_id)
        key = self._node_key.get(job_id, "")
        free = self._node_free.get(job_id, False)
        if not free and self._active_by_key.get(key) == job_id:
            del self._active_by_key[key]
            q = self._queue_by_key.get(key)
            if q:
                nxt, _ = q.pop(0)
                self._active_by_key[key] = nxt
                self._run_now(nxt, key)
        self._runner_getter().notify_terminal(job_id, status)

    def cancel_remaining(self) -> None:
        self.cancel_remaining_called += 1


def test_runner_diamond_concurrent_on_callback_stack():
    """菱形不同设备：A 后 B/C 并发起跑，二者后 D，全 SUCCESS，各恰好一次。"""
    dag = _dag(
        [_node("A", "d1"), _node("B", "d2"), _node("C", "d3"), _node("D", "d4")],
        [_edge("A", "B"), _edge("A", "C"), _edge("B", "D"), _edge("C", "D")],
    )
    holder = {}
    stack = FakeStack(lambda: holder["r"])
    runner = TaskDagRunner(dag, stack.on_start_node, on_cancel_remaining=stack.cancel_remaining)
    holder["r"] = runner

    async def scenario():
        run_task = asyncio.ensure_future(runner.run())
        await settle()
        assert stack.running == {"A"}
        stack.finish("A")
        await settle()
        assert stack.running == {"B", "C"}
        stack.finish("B")
        stack.finish("C")
        await settle()
        assert stack.running == {"D"}
        stack.finish("D")
        return await run_task

    result = asyncio.run(scenario())
    assert all(st == NodeState.SUCCESS for st in result.values())
    assert sorted(stack.started) == ["A", "B", "C", "D"]
    assert len(stack.started) == len(set(stack.started))
    assert stack.cancel_remaining_called == 0


def test_runner_same_device_serialized():
    """同 device_action_key 两 ready 节点经栈的队列串行、不重叠。"""
    dag = _dag(
        [
            _node("A", "d1"),
            _node("B", "d2", "stir"),
            _node("C", "d2", "stir"),
            _node("D", "d4"),
        ],
        [_edge("A", "B"), _edge("A", "C"), _edge("B", "D"), _edge("C", "D")],
    )
    holder = {}
    stack = FakeStack(lambda: holder["r"])
    runner = TaskDagRunner(dag, stack.on_start_node)
    holder["r"] = runner

    async def scenario():
        run_task = asyncio.ensure_future(runner.run())
        await settle()
        stack.finish("A")
        await settle()
        both = stack.running & {"B", "C"}
        assert len(both) == 1  # 同 key 只有一个在跑
        first = both.pop()
        stack.finish(first)
        await settle()
        second = ({"B", "C"} - {first}).pop()
        assert stack.running & {"B", "C"} == {second}
        stack.finish(second)
        await settle()
        assert stack.running == {"D"}
        stack.finish("D")
        return await run_task

    result = asyncio.run(scenario())
    assert all(st == NodeState.SUCCESS for st in result.values())


def test_runner_fail_fast_triggers_cancel_remaining():
    """某节点 failed：任务整体 failed，且触发 on_cancel_remaining 清理设备残余。"""
    dag = _dag(
        [_node("A", "d1"), _node("B", "d2"), _node("C", "d3"), _node("D", "d4")],
        [_edge("A", "B"), _edge("A", "C"), _edge("B", "D"), _edge("C", "D")],
    )
    holder = {}
    stack = FakeStack(lambda: holder["r"])
    runner = TaskDagRunner(dag, stack.on_start_node, on_cancel_remaining=stack.cancel_remaining)
    holder["r"] = runner

    async def scenario():
        run_task = asyncio.ensure_future(runner.run())
        await settle()
        stack.finish("A")
        await settle()
        stack.finish("B", status="failed")  # B 失败 -> fail-fast
        return await run_task

    result = asyncio.run(scenario())
    assert result["B"] == NodeState.FAILED
    assert "D" not in stack.started  # D 绝不起跑
    assert runner.dispatched_node_ids == frozenset({"A", "B", "C"})
    assert stack.cancel_remaining_called == 1


def test_runner_keeps_undeclared_device_metadata_out_of_workflow_outputs():
    """设备返回的诊断字段保留在 terminal_info，但不能污染无输出动作的契约。"""
    dag = _dag([_node("A", "d1", output_schema={})], [])
    holder = {}
    stack = FakeStack(lambda: holder["r"])
    runner = TaskDagRunner(dag, stack.on_start_node)
    holder["r"] = runner

    async def scenario():
        run_task = asyncio.ensure_future(runner.run())
        await settle()
        runner.notify_terminal(
            "A",
            "success",
            return_info={
                "return_value": {
                    "success": True,
                    "message": "physical action completed",
                }
            },
        )
        return await run_task

    result = asyncio.run(scenario())
    assert result == {"A": NodeState.SUCCESS}
    assert runner._executor.results["A"].outputs == {}  # noqa: SLF001


def test_runner_projects_only_declared_workflow_outputs():
    """有输出契约时只投影声明字段，忽略同一返回对象中的设备元数据。"""
    dag = _dag(
        [
            _node(
                "A",
                "d1",
                output_schema={"reading": {"type": "number", "required": True}},
            )
        ],
        [],
    )
    holder = {}
    stack = FakeStack(lambda: holder["r"])
    runner = TaskDagRunner(dag, stack.on_start_node)
    holder["r"] = runner

    async def scenario():
        run_task = asyncio.ensure_future(runner.run())
        await settle()
        runner.notify_terminal(
            "A",
            "success",
            return_info={
                "return_value": {
                    "reading": 1.25,
                    "success": True,
                    "message": "measurement completed",
                }
            },
        )
        return await run_task

    result = asyncio.run(scenario())
    assert result == {"A": NodeState.SUCCESS}
    assert runner._executor.results["A"].outputs == {"reading": 1.25}  # noqa: SLF001


def test_runner_cancel_resolves_pending():
    """外部 cancel_task：停止调度后继，未决节点解析为 CANCELLED，run() 正常返回。"""
    dag = _dag(
        [_node("A", "d1"), _node("B", "d2")],
        [_edge("A", "B")],
    )
    holder = {}
    stack = FakeStack(lambda: holder["r"])
    runner = TaskDagRunner(dag, stack.on_start_node)
    holder["r"] = runner

    async def scenario():
        run_task = asyncio.ensure_future(runner.run())
        await settle()
        assert stack.running == {"A"}  # A 在跑，尚未 finish
        runner.cancel()  # 模拟收到 cancel_task
        await settle()
        return await run_task

    result = asyncio.run(scenario())
    assert result["A"] == NodeState.CANCELLED
    assert "B" not in stack.started  # 后继绝不调度


def test_debug_terminate_resolves_callback_future_and_cleans_once():
    """调试终止不能只停 admission；已派发 callback future 也必须收敛。"""
    dag = _dag(
        [_node("A", "d1"), _node("B", "d2")],
        [_edge("A", "B")],
        task_id="debug-terminate",
    )
    holder = {}
    stack = FakeStack(lambda: holder["r"])
    debugger = DebugController(
        run_id=dag.task_id,
        node_ids=set(dag.nodes),
    )
    runner = TaskDagRunner(
        dag,
        stack.on_start_node,
        on_cancel_remaining=stack.cancel_remaining,
        debug_controller=debugger,
    )
    holder["r"] = runner

    async def scenario():
        run_task = asyncio.ensure_future(runner.run())
        await settle()
        assert stack.running == {"A"}
        projection = await runner.debug_command("terminate")
        result = await asyncio.wait_for(run_task, timeout=1)
        return projection, result

    projection, result = asyncio.run(scenario())
    assert projection["stopReason"] == "terminate"
    assert result == {
        "A": NodeState.CANCELLED,
        "B": NodeState.CANCELLED,
    }
    assert stack.cancel_remaining_called == 1


def test_debug_emergency_stop_triggers_immediate_run_scoped_cleanup_once():
    """急停先触发设备清理，再令 pending future/调度图收敛，且不重复清理。"""
    dag = _dag(
        [_node("A", "d1"), _node("B", "d2")],
        [_edge("A", "B")],
        task_id="debug-emergency-stop",
    )
    holder = {}
    stack = FakeStack(lambda: holder["r"])
    debugger = DebugController(
        run_id=dag.task_id,
        node_ids=set(dag.nodes),
    )
    runner = TaskDagRunner(
        dag,
        stack.on_start_node,
        on_cancel_remaining=stack.cancel_remaining,
        debug_controller=debugger,
    )
    holder["r"] = runner

    async def scenario():
        run_task = asyncio.ensure_future(runner.run())
        await settle()
        projection = await runner.debug_command("emergency_stop")
        assert stack.cancel_remaining_called == 1
        result = await asyncio.wait_for(run_task, timeout=1)
        return projection, result

    projection, result = asyncio.run(scenario())
    assert projection["stopReason"] == "emergency_stop"
    assert result == {
        "A": NodeState.CANCELLED,
        "B": NodeState.CANCELLED,
    }
    assert stack.cancel_remaining_called == 1


def test_emergency_cleanup_failure_is_retried_during_run_convergence():
    dag = _dag(
        [_node("A", "d1")],
        [],
        task_id="debug-emergency-retry",
    )
    holder = {}
    stack = FakeStack(lambda: holder["r"])
    attempts = 0

    def flaky_cleanup() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient cleanup failure")

    runner = TaskDagRunner(
        dag,
        stack.on_start_node,
        on_cancel_remaining=flaky_cleanup,
        debug_controller=DebugController(
            run_id=dag.task_id,
            node_ids=set(dag.nodes),
        ),
    )
    holder["r"] = runner

    async def scenario():
        run_task = asyncio.ensure_future(runner.run())
        await settle()
        await runner.debug_command("emergency_stop")
        return await asyncio.wait_for(run_task, timeout=1)

    result = asyncio.run(scenario())
    assert result == {"A": NodeState.CANCELLED}
    assert attempts == 2
