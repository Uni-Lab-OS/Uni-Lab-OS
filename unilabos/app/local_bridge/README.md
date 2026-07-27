# Local workflow bridge

`local_bridge` 是统一前端与 OS 权威之间的薄桥。它承载统一 HTTP/WS API 和 OS schedule
WebSocket，但不拥有 DAG 调度、调试、节点终态或可变物料状态。

## 两个传输面

| 面 | 默认地址 | 用途 |
|---|---|---|
| OS schedule WS | `127.0.0.1:8890/api/v1/ws/schedule` | `task_dag` 下发，`job_status`/Material snapshot 回流 |
| Unified HTTP/WS | `127.0.0.1:8014` | 新前端使用的 `unilab/v1` |

旧 Cloud panel `/ws/workflow/{id}` 与 8891 listener 已完全删除。8890 是 OS 内部
schedule wire，不是 UI 接口；前端只使用 8014 上的统一 v1。

## 本地物料接口

真实 OS 通过既有 `unilab -g/--graph` 选择设备图，并在内存中以同一个
`ResourceTreeSet` 持续维护当前物料状态：

```text
GET /api/v1/materials
GET /api/v1/materials/{material_uuid}
GET /api/v1/material-models
GET /api/v1/material-models/assets/{asset_path}
```

这是给前端 `material.readGraph` 使用的**只读聚合投影**。Material GET 会通过 schedule
通道向 OS 查询当前内存快照；bridge 只原子替换投影缓存，不重读启动文件，也不拥有写入口。
OS 未连入或尚未发布快照时返回明确的 503，不能用空图或旧文件冒充当前状态。

OS 路由与 Go backend 的逐项语义、字段差异和内部调用链见
[`MATERIAL_API.md`](MATERIAL_API.md)。尤其注意：两边的
`GET /api/v1/materials` 路径相同，但 OS 返回前端可还原的聚合视图，backend 当前返回
持久化 material 行；二者不具备自动可替换的写语义。

## Unified v1 工作流接口

```text
GET|PUT /api/v1/workflows/{workflow_id}/graph
POST    /api/v1/workflows:validate
POST    /api/v1/authoring/compile
POST    /api/v1/authoring/generate-python
POST    /api/v1/authoring/validate
POST    /api/v1/runtime/runs
GET     /api/v1/runtime/runs/{run_id}
GET     /api/v1/runtime/runs/{run_id}/nodes
GET     /api/v1/runtime/runs/{run_id}/nodes/{node_id}
GET     /api/v1/runtime/runs/{run_id}/events?after_seq=...
POST    /api/v1/runtime/runs/{run_id}/commands
POST    /api/v1/runtime/runs/{run_id}/cancel
POST    /api/v1/runtime/runs/{run_id}/reconcile
WS      /api/v1/runtime/events?run_id=...&after_seq=...
```

`local_api.py` 只做 request validation、problem response 和 RuntimeService 投影。
它不能根据 UI 颜色、选中节点或本地计时器更新运行状态。

## 真实与 offline 模式

真实模式等待 OS 主动接入 schedule WS；整张 `TaskDag` 经现有设备提交路径执行。

offline 模式：

```bash
UNILAB_PY=/home/changjunhan/.micromamba/envs/unilab/bin/python
"$UNILAB_PY" -m unilabos.app.local_bridge.server \
  --offline -g unilabos/test/experiments/plr_test_converted.json
```

`OfflineOS` 只替代进程和传输边界，仍复用同一个 `TaskDag`、`DagExecutor`、资源锁、
journal 和 debugger。它不能实现一套“更简单”的前端专用 scheduler，也不能伪造设备成功
来代替 contract test 明确注入的结果。

可选参数包括 `--host`、`--schedule-port`、`--api-port`、`--journal-path`、
`--profile`。`-g/--graph` 只允许与 `--offline` 一起使用；真实模式必须接受
`unilab -g` 所在 OS 的内存快照。默认只允许 loopback。

## 文件导航

- `server.py`：两个传输面的组合入口和 shared runtime 装配。
- `local_api.py`：统一 v1 与 legacy HTTP 投影。
- `schedule_ws.py`：OS schedule 会话、`task_dag`/`job_status` 与物料快照。
- `workflow_to_dag.py`：旧 UI 图形状到冻结 `TaskDag` wire 的边界转换。
- `offline_os.py`：复用 OS executor 的进程内对端。
- `bind_security.py`：loopback 监听安全检查。
- `material_api.py`：OS 图到只读 Material Aggregate 行的投影。
- `material_models.py`：本地模型登记、公开元数据与安全资源路径解析。
- `MATERIAL_API.md`：OS/backend 物料接口与调用链对照。

## 绝对不能做

- 不能把 bridge 变成第二个 backend/scheduler。
- 不能在 legacy 路由新增仅供某个组件使用的运行语义。
- 不能裁剪完整 DAG 来实现起始点。
- 不能把 `job_status`/HTTP ack 当作未收到的设备终态。
- 不能让 offline 与真实模式使用不同的调试或资源规则。
- 不能默认监听 `0.0.0.0` 或绕过 loopback 检查。
- 不能恢复 8891、`workflow_ws.py` 或 `/ws/workflow/{uuid}`。
- 不能从运行中的 graph 文件重读并覆盖 OS 内存状态。
- 不能把只读 Material Graph 投影扩展成第二个物料数据库。
- 不能组合 backend 行级 CRUD 来伪装具备 revision/幂等/补偿的统一写命令。
- 不能把当前图中的 Well/TipSpot 兼容投影固化为长期领域 Site。

## 验证

```bash
UNILAB_PY=/home/changjunhan/.micromamba/envs/unilab/bin/python
"$UNILAB_PY" -m pytest tests/app
"$UNILAB_PY" -m pytest tests/runtime
```

前端联调还要运行 `uni-lab-fe` 的 workflow E2E，确认完整 DAG、authoring 往返、
事件续传、起始点、断点、单步、异常和终止均来自真实 v1 投影。

物料接口修改至少运行：

```bash
"$UNILAB_PY" -m pytest tests/app/test_material_api.py
```

然后运行前端 material E2E，验证真实图、模型与 mesh 子资源，而不是只检查 HTTP 200。
