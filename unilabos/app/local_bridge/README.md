# Local workflow bridge

`local_bridge` 是统一前端与 OS 权威之间的薄桥。它承载统一 HTTP/WS API 和 OS schedule
WebSocket，但不拥有 DAG 调度、调试、节点终态或可变物料状态。

## 两个传输面

| 面 | 默认地址 | 用途 |
|---|---|---|
| OS schedule WS | `127.0.0.1:8890/api/v1/ws/schedule` | `task_dag` 下发，`job_status`/Material snapshot 回流 |
| OS internal HTTP | `127.0.0.1:8002/internal/v1` | bridge 读取当前动作合同与已构建 Registry 模板目录 |
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

## Edge 模板目录

模板目录与当前物料图是两套不同的只读投影：

```text
GET /api/v1/resource-templates
GET /api/v1/resource-templates/{template_uuid}
GET /api/v1/resource-templates/{template_uuid}/assets/{asset_key}
```

- 列表一次返回所有轻量 summary；搜索和设备/耗材过滤由前端本地完成。
- 详情按 UUID 懒加载，才包含 geometry、container layout、configuration 和显式资源。
- Registry 的 resource 默认公开；device 默认内部，仅显式
  `catalog.visibility: public` 的设备进入目录。
- UUID、content hash、catalog revision 和 ETag 都是稳定、可验证的内容身份。
- bridge 默认从 `--execution-http-url http://127.0.0.1:8002` 读取内部接口，
  不依赖 schedule WS 是否已建立。
- 内存缓存 TTL 为 5 秒。超时后带 ETag 重验证；上游临时不可用且已有缓存时返回
  `stale: true` 并禁用所有创建动作，没有缓存则返回结构化 503。
- 可选 `--internal-api-token` 与 OS 的 `UNILABOS_INTERNAL_API_TOKEN` 配套。

公共成功响应仍使用 `{code,data,message}`；错误使用根级
`{error:{code,message,retryable}}`。`refresh=true` 可用于明确发起重验证。
浏览器只连接 8014，不能直接连接 8002。

## Edge Runtime 动作目录

真实模式下，统一 Runtime 不能使用 demo 动作目录，也不能根据工作流内容猜测动作。OS 在
HostNode 就绪后，从当前内存 `_action_value_mappings` 投影：

```text
GET :8002/internal/v1/runtime-actions
  → host_ready
  → bridge 原子替换内存 Action Catalog
  → GET :8014/api/runtime/local/actions
```

目录项以设备实例 ID 组成 `action_ref`，例如 `host_node.test_latency`。输入、输出、
resource claims、effects 与 timing 都来自 Registry action schema/contract。OS 重连时强制
重新验证 ETag；同步失败后立即清空上一会话的 live catalog，仅保留显式 Profile 合同并将
目录标记为不可用。不能用 stale catalog 许可新工作流运行。

`host_node.test_latency` 还使用 schedule WS 上的应用层 `ping`/`pong`。bridge 只回显
`ping_id`、`client_timestamp` 并写入接收时的 `server_timestamp`；畸形 ping 被拒绝。
这与 WebSocket 自身的 keepalive ping 不属于同一协议。

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
`--profile`、`--execution-http-url`、`--internal-api-token`。
`-g/--graph` 只允许与 `--offline` 一起使用；真实模式必须接受
`unilab -g` 所在 OS 的内存快照。默认只允许 loopback。

调试/E2E 可以额外设置 `--offline-node-delay SECONDS`，为每个模拟设备节点加入
非阻塞执行时长，以确定性观察 `running → pause_pending → paused` 和运行中急停。
该参数只允许用于 `--offline`，默认 `0`，不得据此给真实设备路径增加测试延时。

## 文件导航

- `server.py`：两个传输面的组合入口和 shared runtime 装配。
- `local_api.py`：统一 v1 与 legacy HTTP 投影。
- `schedule_ws.py`：OS schedule 会话、`task_dag`/`job_status` 与物料快照。
- `workflow_to_dag.py`：旧 UI 图形状到冻结 `TaskDag` wire 的边界转换。
- `offline_os.py`：复用 OS executor 的进程内对端。
- `bind_security.py`：loopback 监听安全检查。
- `material_api.py`：OS 图到只读 Material Aggregate 行的投影。
- `material_models.py`：本地模型登记、公开元数据与安全资源路径解析。
- `resource_template_api.py`：Registry internal HTTP 客户端、ETag/TTL 缓存、
  stale 降级和资源转发。
- `runtime_action_api.py`：当前 OS Runtime Action Catalog 客户端、严格响应校验与
  会话重验证。
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
- 不能从当前 Material Graph 反推模板，也不能把模板目录并入 Material Graph store。
- 不能恢复 Cloud/前端静态模板作为 Edge fallback。
- 不能在 stale 目录或 unresolved 模板上开放创建。
- 不能让模板目录依赖 schedule WS，或由 bridge 再扫描一遍 Registry YAML。
- 不能把 Registry 已存在的方法硬编码进前端或 demo catalog 来绕过
  `ACTION_NOT_FOUND`。
- 不能在 OS 重连或动作目录同步失败后继续使用上一会话的 live Action Catalog。

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
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 "$UNILAB_PY" -m pytest \
  tests/app/test_material_api.py \
  tests/registry/test_template_catalog.py \
  tests/app/test_resource_template_internal_api.py \
  tests/app/test_resource_template_proxy.py
```

然后运行前端 material E2E，验证真实图、模型与 mesh 子资源，而不是只检查 HTTP 200。
