# Round R1B：durable runtime state kernel 设计

日期：2026-08-01

实现分支：`migration/r1b-durable-runtime-kernel`

基线：`d461b93450dfbaf36957562938ba4df108aabfbf`

Wayfinder：`Uni-Lab-OS/Uni-Lab-Core#150`、`deepmodeling/Uni-Lab-OS#303`

## 1. 本轮结果与停止线

R1B 在 OS 内建立唯一的 durable runtime state kernel。它消费 R1A 已持久化的
`step/pause/resume/cancel` command，统一校验 Task/Job 状态迁移，并把业务状态、
append-only journal 和前端 outbox 放在同一 SQLite 事务提交。它还提供 feedback
历史、执行不确定性、人工 reconcile 和启动恢复的持久语义。

本轮完成：

- `WorkflowRuntimeCoordinator` 是 Task/Job runtime mutation 的唯一公开领域入口；
- pending command 按 `(create_time, uuid)` FIFO 消费；
- Task/Job/control/cleanup 状态迁移经过冻结矩阵校验；
- 每次前端可见 mutation 同事务追加 journal 和唯一
  `workflow.runtime.changed` invalidation；
- feedback history 以 sequence 和 idempotency key 双重幂等持久化，并提供冻结的
  frontend REST 历史查询；
- in-flight Job 可以进入 `execution_unknown`，必须显式 reconcile；
- 进程启动把遗留 in-flight Job 转为 unknown，绝不盲重放 dispatch；
- production composition 只拥有一个 command/recovery worker，并在 Store 关闭前
  完成 stop/join。

停止线：

- 不实现 R2 的 DAG readiness、Edge activation、admission、Reservation/Claim、
  Job 自动 dispatch 或 Task terminal aggregation；
- 不实现 D1 的 device transport、driver、RobotCommand、物料 ChangeSet 或真实结果
  commit；
- 不实现 manual confirmation、intervention、timeout policy、retry、Task output、
  debugger Hold 或旧 `/runtime/runs` 兼容；
- 不修改前端，不新增 Backend-to-Edge HTTP/WebSocket 路由；
- `step` 只形成 durable permit，R1B 不解释“下一个 ready Node”；该 permit 只能由
  R2 coordinator 消费。

## 2. Authority 与模块边界

新增深模块 `unilabos.workflow.runtime`：

```python
WorkflowRuntimeCoordinator(store)
  .consume_next_command(task_uuid) -> command | None
  .start_task(task_uuid) -> task
  .transition_task(task_uuid, status, *, error_info=None) -> task
  .transition_job(job_uuid, status, *, feedback_data=None,
                  return_info=None, error_info=None, reason=None) -> job
  .commit_job_feedback(job_uuid, samples) -> result
  .mark_job_unknown(job_uuid, reason) -> job
  .resolve_job_uncertainty(job_uuid, status, *, reason) -> job
  .recover_startup() -> recovery_result

WorkflowRuntimeWorker(coordinator, poll_interval_seconds=...)
  .start()
  .stop()
  .join(timeout=...)
  .is_alive()
```

规则：

1. Coordinator 封装业务事务；Store 是唯一 SQL authority，不能从 Service、HTTP、
   worker 或 future scheduler 绕过 Coordinator 直接改 Task/Job runtime 字段。
2. `WorkflowService` 和 `/api/v1` 只保留 Task/Job/command/feedback 的 frontend read
   projection 与 command create。feedback commit 是内部 executor seam，不新增前端
   写路由。
3. Worker 只做 startup recovery 和 pending command 消费；它不是 DAG scheduler，
   不把 pending Job 改为 dispatched。
4. `ProductionWorkflowComposition` 创建且只创建一个 Worker；ready publication 前先
   同步完成 startup recovery，再启动轮询。关闭顺序是 source monitor → runtime
   worker → Store。若 worker 未能停止，composition 保留 authority lease 且不关闭
   Store，与现有 source monitor 的失败隔离规则一致。
5. future R2/D1 只能调用 Coordinator 的公开 transition seam；不得再造平行状态表、
   事件流或 runtime truth。

## 3. 冻结状态机

### 3.1 WorkflowTask

| from | allowed to |
|---|---|
| `pending` | `running`, `canceled` |
| `running` | `succeeded`, `failed`, `canceling`, `timeout` |
| `canceling` | `canceled`, `failed`, `timeout` |
| `succeeded` / `failed` / `canceled` / `timeout` | 无 |

Task terminal set 是 `succeeded/failed/canceled/timeout`。`started_at` 只在第一次进入
`running` 时写入；`finished_at` 只在进入 terminal 时写入。R1B 不自动从 Job 集合推导
Task terminal；R2/O1 后续通过同一 coordinator 提交聚合结果。

### 3.2 WorkflowNodeJob

| from | allowed to |
|---|---|
| `pending` | `dispatched`, `failed`, `skipped`, `canceled` |
| `dispatched` | `running`, `cancel_requested`, `succeeded`, `failed`, `canceled`, `timeout`, `execution_unknown` |
| `running` | `intervention_required`, `cancel_requested`, `succeeded`, `failed`, `canceled`, `timeout`, `execution_unknown` |
| `intervention_required` | `running`, `cancel_requested`, `failed`, `timeout`, `execution_unknown` |
| `cancel_requested` | `canceled`, `failed`, `timeout`, `execution_unknown` |
| `execution_unknown` | `running`, `succeeded`, `failed`, `canceled`, `timeout` |
| `succeeded` / `failed` / `skipped` / `canceled` / `timeout` | 无 |

Job terminal set 是 `succeeded/failed/skipped/canceled/timeout`。首次进入 `running` 写
`started_at`；进入 terminal 写 `finished_at`。进入 `execution_unknown` 写
`uncertainty_reason`，离开时清空。`transition_job` 不做 DAG readiness、不分配设备、
不修改其他 pending Job。

### 3.3 control 与 cleanup

- `control_status`：`active`、`paused`、`waiting_intervention`、
  `waiting_reconciliation`；
- `cleanup_status`：`none`、`pending`、`canceling`、`settled`、
  `requires_attention`；
- 进入 reconciliation 前把 `active/paused` 保存到
  `reconciliation_resume_control_status`；所有 unknown Job 解决后恢复该值，缺省
  `active`，并清空保存字段；
- 非法迁移返回 `StoreConflict`，必须是 zero-write：不得更新时间、journal、event
  或 command 状态。

## 4. Durable schema

### 4.1 `workflow_runtime_journal`

append-only，禁止 UPDATE/DELETE runtime 代码路径：

| column | contract |
|---|---|
| `sequence` | `INTEGER PRIMARY KEY AUTOINCREMENT`，全局单调 cursor |
| `workflow_task_uuid` | required FK |
| `workflow_node_job_uuid` | nullable FK |
| `workflow_task_command_uuid` | nullable FK |
| `kind` | `task_transition/job_transition/command_consumed/feedback_committed/uncertainty_opened/uncertainty_resolved/startup_recovered` |
| `from_status` / `to_status` | nullable status snapshot |
| `data` | required valid JSON object |
| `create_time` | required UTC timestamp |

索引固定为 `(workflow_task_uuid, sequence)` 和
`(workflow_node_job_uuid, sequence)`。

### 4.2 `workflow_task_step_permit`

| column | contract |
|---|---|
| `workflow_task_command_uuid` | PK/FK；一个 step command 最多一个 permit |
| `workflow_task_uuid` | required FK |
| `target_node_uuid` | nullable；保留 R1A 已校验的 target |
| `status` | `available/consumed`；R1B 只创建 available |
| `create_time` / `consumed_at` | durable lifecycle |

索引固定为 `(workflow_task_uuid, status, create_time,
workflow_task_command_uuid)`。permit 是内部 scheduler contract，不进入 frontend DTO。

### 4.3 `workflow_node_job_feedback_history`

字段镜像冻结 Backend frontend read model：BaseModel fields、
`workflow_node_job_uuid`、`sequence`、`feedback_type`、`data`、`observed_at`、
`received_at`、`published_at`、`idempotency_key`。约束：

- `sequence > 0`，`feedback_type` 和 `idempotency_key` 去首尾空白后非空；
- `data` 必须是 JSON object；
- active row 对 `(job_uuid, sequence)` 和 `(job_uuid, idempotency_key)` 分别唯一；
- 同 sequence/key 且全部样本内容相同是 replay，不产生新 row/journal/event；任一键
  复用但内容不同是 409/`StoreConflict`；
- 只有 sequence 大于 Job 当前 `feedback_sequence` 的样本更新 Job
  `feedback_sequence/feedback_data` summary；历史仍按 sequence 全部保留；
- OS 内部 commit 与 frontend invalidation 同事务完成，因此新样本的
  `published_at` 在提交时写入，不保留 Backend Edge notification 的第二阶段发布。

前端读路由：

```text
GET /api/v1/workflow-node-jobs/{job_uuid}/feedback
    ?after_sequence=<non-negative int64>&limit=<1..500>
```

默认 `after_sequence=0`、`limit=100`；items 按 sequence ASC；响应 data 是
`{items, next_cursor, has_more}`。不存在 Job 为 404，非法 UUID/cursor/limit 为 400。

### 4.4 `frontend_event` 是唯一 outbox

不新增 runtime event 表。现有 `frontend_event` 是唯一 durable SSE outbox；journal
只供审计/recovery，不能被前端消费。

每个 committed、前端可见的 coordinator mutation，按受影响 Task 每个事务恰好追加
一个 event：

```text
event: workflow.runtime.changed
data: {"workflow_task_uuid":"<uuid>"}
```

payload 是 invalidation，不带 patch、Job UUID、status、feedback 或 command result。
客户端必须重新读取 Task/Jobs/feedback。幂等 replay 和失败事务不追加 event；一个
事务同时修改多个同 Task Job 仍只有一个 event。cursor/replay 继续复用现有全局
`frontend_event.id` 与 `GET /api/v1/events`。

## 5. Command 消费语义

`consume_next_command(task_uuid)` 在单事务中选取该 Task 最老 pending command，按
`(create_time, uuid)` 排序，一次最多消费一条。worker 在后续 tick 继续 drain；这个
边界使每条 command 都有独立 journal/result 和可重放提交点。

成功结果固定为 `{"outcome":"applied"}`；消费时因 Task 已 terminal 或 command
对当前状态非法时，command 进入 `rejected`，结果固定为
`{"outcome":"rejected","error_code":"invalid_transition"}`。两者都写
`consumed_at`、`command_consumed` journal 和一次 runtime invalidation；SQLite 失败
则整笔回滚，command 保持 pending。

四种 command：

1. `pause`
   - Task 必须非 terminal；
   - 普通状态设 `control_status=paused`；
   - 若当前 `waiting_reconciliation`，保持该可见状态，只把
     `reconciliation_resume_control_status` 设为 `paused`。
2. `resume`
   - Task 必须非 terminal；
   - 普通状态设 `control_status=active`；
   - 若当前 `waiting_reconciliation`，保持该可见状态，只把 resume 值设为
     `active`。
3. `step`
   - Task 必须非 terminal；
   - 创建以 command UUID 为主键的 available permit；不改变 Task/Job status；
   - R1B 不消费 permit，也不解释 target readiness。
4. `cancel`
   - terminal Task 拒绝；
   - pending Task：所有 pending Job → `canceled`，Task → `canceled`，
     `control_status=paused`，`cleanup_status=settled`；
   - running/canceling Task：pending Job → `canceled`；
     dispatched/running/intervention_required Job → `cancel_requested`；
     execution_unknown 保持 unknown；Task 保持/进入 `canceling`；
   - 若存在 unknown，`cleanup_status=requires_attention`；否则存在待取消 Job 时为
     `canceling`；若已无 active/unknown Job，Task → `canceled` 且 cleanup settled；
   - command 成功表示取消意图已 durable，不表示设备已经完成清理。

一条 cancel 可修改多个 Job，但整个事务只追加一个 runtime invalidation；journal 为
每个实际 Job/Task transition 分别留痕，最后追加 command_consumed。

## 6. Feedback、unknown 与启动恢复

### 6.1 Feedback commit

`commit_job_feedback` 接受单个或 batch sample，先完整验证并按 sequence 排序，再在
一个事务中处理。batch 内重复键也遵守同一幂等/冲突规则；部分写入禁止。Job 必须
存在且非 terminal。至少新增一个 sample 时，为每个 sample 追加
`feedback_committed` journal，并为 Task 追加一个 runtime invalidation。

### 6.2 打开和解决 execution uncertainty

`mark_job_unknown(job_uuid, reason)` 只接受
`dispatched/running/intervention_required/cancel_requested`：

- Job → `execution_unknown`，保存非空 reason；
- Task 保存进入前的 active/paused 恢复值并进入 `waiting_reconciliation`；
- cleanup → `requires_attention`，`attention_reason` 写入可行动原因；
- 同事务 journal + 一次 runtime invalidation。

`resolve_job_uncertainty(job_uuid, status, reason=...)` 只接受 frozen matrix 中从
unknown 可达的 status。仍有其他 unknown Job 时 Task 保持 waiting；最后一个 unknown
解决后恢复 control。若 Task 正在 canceling，仍有待取消 Job 则 cleanup
`canceling`，无 active Job 则 Task `canceled`/cleanup `settled`；否则 cleanup 回到
`none`。R1B 不自动把普通 running Task 聚合为 succeeded/failed。

### 6.3 Startup recovery

production composition 每次取得 authority lease 后、对外宣布 ready 前调用
`recover_startup()`：

- 扫描 `dispatched/running/intervention_required/cancel_requested` Job；
- 每个 Job 原子转为 `execution_unknown`，reason 固定为
  `runtime_restarted_in_flight`；
- 对应 Task 进入 `waiting_reconciliation/requires_attention`；
- 每个 affected Job 追加 `startup_recovered` journal，每个 affected Task 追加一次
  runtime invalidation；
- pending Job、terminal Job、pending command、available step permit 保持原样；
- 不重发 dispatch、不猜测 outcome、不自动取消物理动作。

恢复操作幂等：第二次运行没有 in-flight 候选时 zero-write。

## 7. RED、实现与验收证据

独立测试作者只经公开 seam 与真实 SQLite/HTTP/SSE 编写 RED 测试。允许直接查询
SQLite 的场景仅限 schema 约束、同事务/zero-write、journal append-only 和 reopen
持久性证据；不得 mock Store、Coordinator 内部 transition 或事务。

必测纵向场景：

1. Task/Job 每条 allowed/forbidden transition，timestamps 与 zero-write；
2. 四种 command 的 FIFO、replay、terminal race、cancel 多 Job 原子性和 step permit；
3. journal 与 exact `workflow.runtime.changed` payload/cardinality、事务回滚、SSE
   cursor replay；
4. feedback batch、sequence/idempotency replay/conflict、latest summary、REST 分页和
   SQLite reopen；
5. unknown open/resolve、多 unknown 恢复控制态和 cancel cleanup；
6. startup recovery 的 no-blind-replay、幂等和 event/journal 顺序；
7. production composition 的 singleton worker、ready-before-recovery、stop/join、
   failed-stop lease retention；
8. 完整 `tests/workflow`、全仓 pytest、Ruff/静态检查与 diff hygiene。

本轮没有浏览器 UI 改动。阶段报告若不存在可见页面，将以至少五组可复核的
API/SSE/SQLite-reopen E2E transcript/artifact 代替截图，并明确标记“无浏览器截图”，
不得伪造 UI 证据。

## 8. 后续接力

- R2 消费 available step permit、做 DAG readiness/admission，并只通过本模块提交
  Task/Job transition；
- D1 把 driver feedback/outcome 投到本模块，不取得状态或 outbox authority；
- FE runtime slice 实现 `WorkflowTaskController`：先建立 SSE cursor，再读取一致 REST
  snapshot；收到 runtime invalidation 后重新读取 Task/Jobs/feedback；
- OS 与 FE 候选 SHA 冻结后，在 Core #150 写 integration spec，覆盖 HTTP/SSE 时序、
  cursor reconnect、OS restart、partial-read failure 和 stale response fencing。
