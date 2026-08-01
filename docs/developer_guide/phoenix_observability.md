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
- Phoenix 自身遥测和外部资源加载：关闭

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

## 降级行为

Phoenix 未安装、端口不可用或数据库迁移失败时，Uni-Lab-OS 继续启动并执行设备与
Workflow。`status` 返回 `state: "degraded"` 和可展示的 `last_error`；上报与查询返回
`503 observability_unavailable`。禁用时状态为 `disabled`。

Phoenix 采用 Elastic License 2.0。它作为可选独立进程运行，没有将 Phoenix 源码复制
到 GPL-3.0 的 Uni-Lab-OS 中；产品分发前仍需完成许可证合规审核。
