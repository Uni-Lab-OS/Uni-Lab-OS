# Electron Trace 日志与 Arize Phoenix OSS

Uni-Lab-OS 可以管理一个仅监听 loopback 的 Arize Phoenix OSS 进程。Electron
通过 Uni-Lab-OS 上报和查询 trace，不直接依赖 Phoenix 的端口、数据库或 REST
Interface。Phoenix 使用独立 SQLite 数据库，不与 Workflow、Material 或 Scheduler
数据库混用。

## 安装与启用

使用 Uni-Lab-OS 要求的 Python 3.11 环境安装可选依赖：

```bash
pip install -e '.[observability]'
```

在实验室配置文件中启用：

```python
class ObservabilityConfig:
    enabled = True
    auto_start = True
    project_name = "uni-lab-electron"
    retention_days = 30
```

默认配置如下：

- Phoenix HTTP：`127.0.0.1:6006`
- Phoenix gRPC：`127.0.0.1:4317`
- SQLite：`<BasicConfig.working_dir>/observability/phoenix/phoenix.sqlite3`
- Phoenix 日志：`<BasicConfig.working_dir>/observability/phoenix/phoenix.log`
- Phoenix 自身遥测、外部资源加载、模型与代码沙箱提供商：关闭
- 本地 sentinel：阻止 Phoenix 17.5 在关闭 WASM 后仍于启动期预下载运行时

也可以通过现有配置环境变量机制设置，例如：

```bash
export UNILABOS_OBSERVABILITYCONFIG_ENABLED=true
export UNILABOS_OBSERVABILITYCONFIG_RETENTION_DAYS=30
```

`host` 只接受 loopback 地址。若 `auto_start = False`，Uni-Lab-OS 只连接已经运行
在配置地址上的 Phoenix，不创建子进程。

## Electron 上报

推荐在 Electron main process 尽早注册 `@arizeai/phoenix-otel`，不要在 renderer
直接持有上报地址：

```typescript
import { register } from "@arizeai/phoenix-otel";

const traceProvider = register({
  projectName: "uni-lab-electron",
  url: "http://127.0.0.1:18003/api/v1/observability/otlp",
  batch: true,
});
```

`url` 也可以直接填写完整路径：

```text
http://127.0.0.1:18003/api/v1/observability/otlp/v1/traces
```

Electron 退出前应调用 `await traceProvider.shutdown()`，避免批量处理器中尚未发送的
span 丢失。原始文本日志仍应保留为启动失败和崩溃的保底信息；需要关联排障的操作、
错误和关键状态应记录为 span、event 或 attribute。不要上报 token、密码、完整设备参数
或其他敏感内容。

## Uni-Lab-OS Interface

除 OTLP 上报外，响应使用统一的 `code/data/error` envelope。

```text
GET  /api/v1/observability/status
POST /api/v1/observability/otlp/v1/traces
GET  /api/v1/observability/traces
GET  /api/v1/observability/traces/{trace_id}
```

trace 列表支持以下查询参数：

- `limit`：1 到 1000，默认 50
- `cursor`：Phoenix 返回的不透明分页游标
- `start_time`、`end_time`：带时区的 ISO 8601 时间
- `sort`：`start_time` 或 `latency_ms`
- `order`：`asc` 或 `desc`
- `include_spans`：`true` 或 `false`
- `session_identifier`：可重复传入，最多 20 个

trace 详情按 `trace_id` 查询 spans，支持 `limit` 和 `cursor`。Electron 不应调用
Phoenix 的管理 Interface，也不应直接读取 SQLite。即使 Uni-Lab-OS 主服务器监听
`0.0.0.0`，observability 路由也只接受来自 loopback 的请求。

## Scheduler、ROS2 与驱动 Trace

启用 observability 后，Uni-Lab-OS 自身使用同一 Phoenix project 上报以下业务 span：

- `workflow.node.dispatch`：Scheduler 下发节点动作；
- `ros2.action.send_goal`、`ros2.action.goal_response`、`ros2.action.result`、
  `ros2.action.cancel`、`ros2.action.cancel_response`：HostNode 的 ROS Action 生命周期；
- `ros2.action.execute`：设备 ActionServer 接收并执行 goal；
- `device.driver.execute`：真正调用设备驱动方法，包括同步线程池和异步方法。

span 只记录 WorkflowTask、WorkflowNodeJob、WorkflowNode、设备、动作和 ROS goal 的
稳定标识及结果状态，不记录完整动作参数、物料树、token 或密码。高频 feedback 不为
每条消息创建 span，避免 trace 放大。

Host 与设备在同一进程时，通过 `job_id = ROS goal UUID` 的本地 context map 传递
W3C `traceparent/tracestate`。远端 `UniLabJsonCommand` 把 carrier 放在已有的
`unilabos_param.trace_context` 中；原生 ROS Action 的 Goal 没有统一扩展字段，因此
Host 会先调用设备私有的 `_register_trace_context` ROS service，收到确认后再发送
goal。旧设备没有该 service 或 trace 注册超时时仅丢失远端父子关系，动作仍正常发送。
Host 的远端原生 Action 登记在专用线程池中按“登记后发 goal”的顺序执行，不占用
Scheduler 的唯一事件 worker；登记失败或 500ms 超时后仍发送业务 goal。只有存在有效
carrier 时才进入该线程池；action server 等待有 5 秒上限，Host 停机时取消排队任务并在
发送前再次检查停机状态，避免停机后继续触发物理动作。

side-channel 只接受 ROS goal UUID、node job UUID、task UUID、动作名和 W3C carrier，
其中 ROS goal UUID 只用于匹配 Action，WorkflowNodeJob UUID 保持原任务领域身份，
二者不会在嵌套设备调用中混用；未知字段会被拒绝。
待消费 context 有数量和 60 秒 TTL 上限。同步驱动进入 `ThreadPoolExecutor` 时显式
复制/恢复 trace context，保证驱动 span 与 ROS span 保持父子关系。设备驱动继续调用
另一设备时，JSON 与原生 ROS Action 也会继续传播 carrier；原生调用使用同一个新生成的
goal UUID 完成 side-channel 登记和 goal 发送。

异常 span 只记录经过白名单清洗的 `error.type` 和 ERROR 状态，明确关闭 OpenTelemetry
默认的 exception message 与 stacktrace 采集，避免驱动异常把动作参数或凭据带入 Phoenix。

## 降级行为

Phoenix 未安装、端口不可用或数据库迁移失败时，Uni-Lab-OS 继续启动并执行设备与
Workflow。`status` 返回 `state: "degraded"` 和可展示的 `last_error`；上报与查询返回
`503 observability_unavailable`。只有 Phoenix OTEL Adapter 缺失或初始化失败时，OS
内部 span 自动退化为 no-op，Electron 的上报、查询和 Phoenix `ready` 状态不受影响。
禁用时状态为 `disabled`。

Phoenix 采用 Elastic License 2.0。它作为可选独立进程运行，没有将 Phoenix 源码复制
到 GPL-3.0 的 Uni-Lab-OS 中；产品分发前仍需完成许可证合规审核。
