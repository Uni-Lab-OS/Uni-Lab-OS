# Runtime service and journal

本目录把已验证的工作流 revision、scheduler、事件 journal 和公开运行投影组合成
OS 的运行权威。HTTP/WS 层只调用这里，不应自行维护 run/node 状态。

## 职责

- `service.py`：保存/校验工作流、创建运行、查询 run/node/event、取消、调试命令和 reconcile。
- `workflow_store.py`：带 revision 冲突检查的工作流文档存储。
- `event_store.py`：SQLite 事件、节点投影、outbox、lease 与恢复数据。
- `reconcile.py`：处理未知派发/终态和资源 fence。
- `profile_loader.py`、`profile_composition.py`：声明式 runtime profile 与 driver 组合。
- `estimated_timeline.py`：基于权威节点状态的时间线投影。
- `paths.py`：runtime 数据路径。

## 创建运行

公开输入为：

```json
{
  "source": {
    "format": "workflow_revision_v2",
    "revision": {}
  },
  "parameters": {},
  "client_request_id": "optional-idempotency-key",
  "debug": {
    "pause_on_start": true,
    "start_node_id": "optional-node",
    "breakpoints": ["node-id"]
  }
}
```

Runtime 会校验完整 revision 和 debug node id，编译成完整 `TaskDag`，然后交给唯一
scheduler。`start_node_id` 不修改 source，也不允许创建 run 后变更。

## 权威投影

- run 状态来自 scheduler handle 与 journal，不来自 transport ack。
- node 列表保留 source node id、attempt、state、result 与错误。
- event `seq` 单调递增，`after_sequence`/`after_seq` 用于断线续传。
- debug projection 包含 status、breakpoints、start node、paused-before node 和 state version。
- 状态读取 API 只投影，不能顺手补写 terminal event。

对于 `dispatch_unknown`、`reconciling` 或有 open unknown fence 的运行，禁止自动重发物理动作。
必须经显式 reconcile，并在同一 journal 中保留审计记录。

## 事件所有权

`DagExecutor`/设备回调负责节点终态，run terminal 也只能由确定的一层落一次。Runtime service
可以转换公开事件名和聚合状态，但不能因轮询或 GET 请求再次写入终态。

事件广播只是 journal 的传输视图：

- WS 断开不丢事实，客户端可从最后 `seq` REST 补拉。
- WS/HTTP 返回成功不代表节点完成。
- 过期 runtime epoch 的回调不能修改新 epoch 的运行。
- terminal commit、lease release/fence 和 outbox 必须保持原子一致性。

## 异常、取消和恢复

- validation/command conflict 使用稳定、结构化错误，不返回伪成功。
- cancel 停止后继 admission，并调用设备/runtime 取消已经派发的动作。
- 无法确认的物理终态保留 unknown fence，不能简单标为 cancelled 后释放资源。
- 重启从 journal 恢复提交、节点、lease 和 cursor；不能只恢复 UI 状态。

## 绝对不能做

- 不能让 local API、WS session 或前端成为运行事实源。
- 不能在 GET projection 中写 event。
- 不能从 transport ack 推断 `success`。
- 不能自动重放 `dispatch_unknown` 的物理动作。
- 不能跨 run 共用 debug controller 或可变 `start_node_id`。
- 不能用系统 Python 运行测试或迁移 runtime SQLite。

## 验证

```bash
UNILAB_PY=/home/changjunhan/.micromamba/envs/unilab/bin/python
"$UNILAB_PY" -m pytest tests/runtime
```

接口投影有改动时还要运行 `tests/app`，并检查创建/查询/事件 replay、调试命令、
取消、terminal 唯一性、unknown fence、reconcile 和重启恢复。
