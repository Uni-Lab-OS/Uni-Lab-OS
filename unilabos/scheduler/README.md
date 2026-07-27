# DAG scheduler and debugger

本目录是 OS 的 DAG 执行语义中心：依赖走图、节点 admission、资源锁、结果落库、
调试器和恢复游标都在这里。任何 bridge、runtime 或前端都不得复制一套调度状态机。

## 分层

```text
TaskDag / DagWalk
        │  ready nodes
        ▼
DebugController             ← pause / breakpoint / step
        │  admitted nodes
        ▼
ReadyPolicy + ResourceLock  ← resource claims and fairness
        │
        ▼
DagExecutor / submit        ← physical device action
        │
        ▼
Result + SQLite journal     ← authoritative projection
```

- `dag_model.py`：`TaskDag`、节点/边和状态模型，解析时拒绝环和无效引用。
- `dag_executor.py`：`DagWalk` 依赖状态机与 `DagExecutor` 异步驱动。
- `debug_controller.py`：run-scoped、admission 前的调试状态机。
- `ready_policy.py`、`resource_lock.py`：确定性 admission 与资源生命周期。
- `result_store.py`：输入 materialization、输出校验和结果 envelope。
- `task_dag_runner.py`：把 executor 接到设备提交路径。
- `event_store` 位于 `unilabos/runtime`，是持久化事件事实源。

## 完整 DAG 与起始点

`TaskDag` 始终包含整张不可变图。存在 `debug.start_node_id` 时：

1. `DagWalk` 从起始点计算可达集合。
2. 集合外节点与跨界边标记 `SKIPPED`。
3. 所选节点成为新的执行边界，外部入边不再抑制它。
4. 初始 skipped 状态照常写入节点终态和 journal。

不能在 bridge 或前端裁图，也不能只在 UI 中置灰而让 OS 仍调度被排除节点。

## 调试语义

- 断点在节点 admission 前命中；此时节点仍未运行、未获取新 lease、未进入设备队列。
- `pause` 停止新 admission；已经运行的物理动作自然收敛后进入 quiescent pause。
- `continue` 临时越过当前断点一次并继续。
- `step` 只放行一个逻辑 ready 节点，在其终态后再次暂停。
- v1 的 `step_over` 和 `step_into` 是 `step` 的显式别名。
- `run_to` 在目标即将 admission 时暂停。
- `terminate`/`emergency_stop` 结束 debug admission；实际设备取消/急停仍必须走上层安全路径。
- `start_node_id` 创建 run 后不可修改；断点可以用 `set_breakpoints` 更新。

调试控制必须在资源 admission 之前。把断点判断挪到 `submit()` 后会造成设备已经动作却被
UI 显示为“尚未执行”，属于安全错误。

## 状态与失败

- 节点正常路径：`PENDING → READY → RUNNING → SUCCESS|FAILED|CANCELLED|SKIPPED`。
- 每个节点至多提交一次；边的 target 只能在依赖终态与分支选择满足后 ready。
- 分支未选路径递归 `SKIPPED`；失败按 fail-fast 收敛未终态节点。
- HTTP/WS ack 不是终态。未知派发和重启恢复必须保留 fence/reconcile 语义。
- 每个 terminal event 只能由唯一所有者原子写入，读取投影不得产生新终态。

## 绝对不能做

- 不能新增另一套走图、资源锁或 debugger 实现。
- 不能在持有新资源或设备入队后才暂停。
- 不能把 step 实现成固定延时或“取当前列表下一行”。
- 不能让继续操作永久删除命中的断点。
- 不能吞掉 `SKIPPED`、`dispatch_unknown`、lease 或 journal 错误。
- 不能靠系统墙钟 sleep 验证并发和单步语义。

## 验证

```bash
UNILAB_PY=/home/changjunhan/.micromamba/envs/unilab/bin/python
"$UNILAB_PY" -m pytest tests/scheduler
"$UNILAB_PY" -m pytest tests/runtime
```

至少覆盖：拒环、完整控制 DAG、起始点可达性、breakpoint-before-admission、并发节点 drain、
恰好一个节点的 step、分支 skip、失败/取消、资源互斥、事件原子性和重启恢复。
