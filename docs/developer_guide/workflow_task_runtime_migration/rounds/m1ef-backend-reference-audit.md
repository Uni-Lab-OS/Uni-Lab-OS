# M1EF：最新 Backend 实现参考审计

日期：2026-08-02

用途：为 M1EF（Job Claim / fencing、Material/Site ChangeSet、uncertainty recovery）
implementation spec 提供逐项 Backend 一手代码参考。本文件是只读审计，不改变 Backend
合同，也不把 Backend 当前分支漂移自动提升为 OS/FE 共享协议。

## 1. 审计基线与使用规则

| 角色 | 仓库 / revision | 结论 |
|---|---|---|
| 最新 Backend production reference | `Uni-Lab-OS/uni-lab-backend` remote default `main@2a3591eaff21d808557e6a645f9092b152fb3504`，2026-08-01 19:35:47 +08:00，`refactor(scheduler): split runtime by responsibility` | 本轮 M1EF 的数据模型、事务、设备锁、timeout/cancel 和 recovery 的最新实现参考。`origin/HEAD -> origin/main`。 |
| 当前 Backend `feat/workflow` tip | `5b49ba7a35ac87515c27cc532a795ddb1be3fedf`，parent 是上述 `2a3591e…` | 相对 `main@2a3591e…` 只增加 Edge protocol diagram 两个文档文件，production tree 与 `main` 相同；因此以下 production 引用统一固定到 `2a3591e…`。[commit](https://github.com/Uni-Lab-OS/uni-lab-backend/commit/5b49ba7a35ac87515c27cc532a795ddb1be3fedf) |
| 已冻结的 frontend-facing authority | `feat/workflow@09609a27e652c9e56ede636a2883a4fd241e4400` | 依 OS `AGENTS.md`，仍是 FE 共享路由/DTO 的只读权威；当前 remote `feat/workflow` 已改写历史且不再包含该 commit，不改变 exact-SHA 冻结规则。[commit](https://github.com/Uni-Lab-OS/uni-lab-backend/commit/09609a27e652c9e56ede636a2883a4fd241e4400) |

审计通过 `git fetch --prune origin` 后，在 detached clean worktree 中读取上述 exact SHA；
Backend 没有被修改。后续每一个 M1EF 决策都必须同时写清四项：

1. 最新 Backend 已经具备什么；
2. 可直接复用的字段、状态或事务模式；
3. OS 为什么必须补强或有意不同；
4. exact SHA、production path、symbol/迁移和行号。

`09609a2` 只回答“FE 当前可以依赖什么共享接口”；`2a3591e` 回答“Backend 最新实现
已经证明了什么运行时模式”。没有单独协议裁决时，不得用后者静默改写前者。

## 2. 四个关键问题的结论

| 问题 | 审计结论 | M1EF 决策含义 |
|---|---|---|
| Backend 是否已有 durable Job Claim + monotonic fence？ | **部分基础已有，完整能力没有。** Backend 已有窄义、持久、Job-owned 的单设备 `ExecutionLockLease`：包含 Job/Task owner、设备独占 `lock_key`、`reserved/running/released/uncertain` 状态及 command-bound access token；但它不是 M1EF 广义 Claim，lease 没有 monotonic fencing token/generation、attempt、完整 Material/Site member set。所谓 `LocalJobClaim` 只是一次行锁事务返回的 Task+Job snapshot，不是持久物理执行权。 | 可以复用 lease 状态机、唯一活跃锁和安全释放原则；必须新增 `job_uuid + attempt` owner、单调 fence、complete member set，并让所有现实状态提交验证当前 fence。不能把 access token 或 `LocalJobClaim` 改名后当成 fence。 |
| Backend 是否已有 Material/Site ChangeSet idempotency？ | **没有。** Backend 的 Job outcome receipt 是持久且幂等的，但 payload 仅落 `return_info/error_info` 并更新 Job；Material state append 与 Site placement 都没有 `job_uuid`、claim/fence、idempotency key 或 ChangeSet receipt。 | 复用 Job result 的“先锁、同 key 同 payload replay、冲突 replay 拒绝、消费后标记”模式；M1EF 仍需独立、确定性的 Material/Site ChangeSet receipt，并在一个 inventory UoW 内验证 fence、提交现实事实与 receipt，再以 durable saga 投影 workflow Job terminal。 |
| Backend 是否已有 `dispatch_unknown` / restart reconciliation？ | **有很强的相邻能力，但没有 literal `dispatch_unknown`。** Backend 使用 `execution_unknown` + `ExecutionLockLease.uncertain`，保留 durable command/result；timeout/cancel 不确定时不释放 lease，重启扫描 active task/unconsumed result，Edge hello 用 `running_jobs` 对账，可恢复 running、接受 late durable outcome 或保留 terminal ghost。 | M1EF 应复用 `execution_unknown/uncertain/waiting_reconciliation/requires_attention` 词汇和“未知时绝不 TTL 解锁”原则。它仍需补齐 claim member/fence/ChangeSet reality 的 restart audit，以及明确的 operator resolution；不得把 Edge 在线对账误当成 Material/Site 现实已提交。 |
| Backend 是否分开 Task Reservation 与 Job Claim？ | **没有。** `DeviceExecutionReservation` 实际是在 Job dispatch 时获得的一条 device execution lease，同时带 task/job FK；没有 admission 阶段、Task lifetime 的 Material Reservation aggregate。 | 保留 OS M1R 已落地的 Task Reservation；M1EF 在其上增加短期 Job Claim。两者生命周期、owner、冲突集合和释放条件必须分离，不能迁移成一张 Backend lease 表。 |

## 3. M1EF 决策参考矩阵

| M1EF decision topic | 最新 Backend 状态 | 可复用 vocabulary / fields / pattern | OS 必须补强或有意不同 | 一手来源 |
|---|---|---|---|---|
| Material shared entity | `material` 持久化 `resource_template_uuid`、`parent_uuid`、`class`、`barcode`、`name`、`config`、`data`，并继承 UUID、timestamps、description、metadata、soft delete。 | 共享字段名、`barcode`、composition 的 `parent_uuid`、`DeletedAt`。 | 保持此前“Backend 字段优先”的裁决；OS-owned disposition/version/Reservation/Claim/ChangeSet 继续明确标为 runtime extension，不能伪装成 Backend 字段。 | [`model.Material`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/domain/model/resource.go#L57-L70)；[`BaseModel`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/domain/model/base.go#L10-L16) |
| Site shared entity and occupancy | `site` 持有 owner `material_uuid`、JSON `allowed_resource_template_uuids`、`occupied_material_uuid` 和 xyz/尺寸；数据库唯一约束保证一个 Material 最多占一个 Site。 | owner / occupant 分离、位置/尺寸字段、occupied Material UUID。 | 继续保留已决策的 OS allowlist 关联表，而 public projection 使用 Backend 数组字段；claim complete set 必须纳入会被改变 occupancy 的 Site。 | [`model.Site`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/domain/model/resource.go#L93-L110)；[`000003_site`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/migrations/postgres/000003_site.up.sql#L1-L42) |
| ResourceSlot boundary | 最新 production model/migration 没有 `ResourceSlot`。WorkflowNode 仍可直接绑定 concrete `material_uuid`。 | `material_uuid` 作为 resolved stable identity。 | ResourceSlot codec/resolve 属于 OS I1/M1R；M1EF 只接受已经解析、已 Reservation 的 stable Material UUID 集合，不能从字段名或 Python value 再推断资源。 | [`WorkflowNode.MaterialUUID`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/domain/model/workflow.go#L242-L265)；完整树 absence command 见 §7。 |
| Task Reservation | Backend 没有 admission-time Task Material Reservation。`DeviceExecutionReservation` 只是返回 lease+Edge 的 dispatch primitive。 | `task_uuid`、冲突即不 dispatch、持久 owner FK。 | OS M1R Task Reservation 继续是 admission lifetime aggregate；M1EF 不替换它，只验证 active Reservation 覆盖 Job claim 所需业务 Material。 | [`DeviceExecutionReservation`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/repository/types.go#L181-L186)；[`ReserveDeviceExecution`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/repository/postgres/workflow_device_execution.go#L18-L90) |
| Durable Job execution lease | `execution_lock_lease` 是持久 device-exclusive lease：`lock_key/material_uuid/task_uuid/job_uuid/state/acquired_at/released_at`；active `lock_key` 和 active `job_uuid` 都唯一。 | `reserved/running/released/uncertain`；Task/Job ownership；active partial unique indexes；无安全证据不释放。 | 将此模式深化为 Claim，而不是平行地再保留模糊 device lock：增加 attempt、monotonic fence、device/business Material/Site complete-set members 和明确 reason/audit。 | [`ExecutionLockLease`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/domain/model/workflow.go#L367-L380)；[`000024_device_execution`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/migrations/postgres/000024_device_execution.up.sql#L17-L51) |
| Monotonic fencing | production tree 中没有 `fence/fencing/fenced` 字段或逻辑。Job access token 由 `job_uuid + command_uuid` 派生并以 hash 持久化，仅认证特定 Edge command。 | command identity、token hash、exact command authorization。 | fence 必须是 authority-side durable monotonic value，每次完整 Claim acquire/reacquire 增长；所有 ChangeSet/late callback 必须验证它。不得把 bearer/access token 当作顺序 fence。 | [`BindDeviceJobDispatch`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/repository/postgres/workflow_device_execution.go#L129-L169)；[`authorizeJobOutcome`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/service/edge/job.go#L170-L217)；absence command 见 §7。 |
| Claim acquisition transaction and lock order | dispatch transaction 先锁 Edge agent，再观察 binding，然后插入 active lease；冲突时 `ON CONFLICT DO NOTHING`，Job 不 dispatch。Coordinator 依 Edge UUID、Job UUID 稳定顺序锁定。 | complete transaction / zero-write、稳定 lock order、contention 非 validation error、one active lease。 | M1EF 必须把 Material/Site member UUID 排序并定义统一 lock order；Claim 是 complete set 的 all-or-none acquisition，不能逐个成员留下 partial state。 | [`ReserveDeviceExecution`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/repository/postgres/workflow_device_execution.go#L18-L90)；[`lockRequiredEdgesAndJobs`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/scheduler/coordinator.go#L1126-L1175)；[deadlock regression test](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/scheduler/device_dispatcher_integration_test.go#L315-L396) |
| Dispatch atomicity | Device dispatcher 在 coordinator 的 Task transaction 内 reserve lease、创建 durable `job.start` command、生成 token、将 Job bind 为 dispatched。 | lease + command + Job transition 原子提交；device lock key `device/{material_uuid}/exclusive`。 | OS 已分为 `inventory.db` 与 `workflow.db`；M1EF 必须先在 inventory UoW 提交 Claim，再以 durable saga 投影 workflow dispatch intent，最后才在两个 transaction 外调用 driver/ROS/network。 | [`DeviceJobDispatcher.TryDispatch`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/scheduler/dispatch/dispatcher.go#L186-L283)；[atomic integration test](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/scheduler/device_dispatcher_integration_test.go#L21-L137) |
| Direct / ad-hoc device action | `CreateDeviceActionRun` 不创建 Workflow/WorkflowNode definition，而是创建 `execution_kind=ad_hoc_device_action` 的 Task、一次性 plan 和唯一 `device_action` Job；随后由同一 Coordinator + `DeviceJobDispatcher` 调度，因此复用同一 `execution_lock_lease`、Edge command/token、timeout/cancel/reconciliation 链。 | direct 与 workflow device action 使用同一 Job/lease authority；request fingerprint/idempotency 只解决 Task create replay。 | M1EF Claim/fence 不能只接 workflow graph path；所有 `executor_kind=device_action`（包括 direct/ad-hoc）都必须经过同一 Claim acquire/commit/recovery port。ad-hoc request fingerprint 不能替代执行 fence 或 ChangeSet receipt。 | [`CreateDeviceActionRun`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/service/workflow/device_action_run.go#L50-L100)；[`buildDeviceActionExecution`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/service/workflow/device_action_run.go#L180-L279)；[same scheduler/Edge chain integration test](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/scheduler/device_dispatcher_integration_test.go#L139-L219)；[`000045_ad_hoc_device_action`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/migrations/postgres/000045_ad_hoc_device_action.up.sql#L1-L35) |
| Attempt / retry | Job 有 `attempt`，唯一键是 `(task_uuid,node_uuid,attempt)`；创建 Task 时所有 Job 为 attempt 1。Execution policy 只接受 timeout，明确拒绝 `retry_count`；Scheduler 没有 automatic retry/re-attempt。 | `attempt` 是 owner identity 的一部分；未知 execution 不自动重试。 | M1EF 可以支持未来 attempt > 1 的 fence identity，但本轮不得顺带发明 retry policy。旧 attempt 永远不能以新 fence 提交；自动 retry 留给独立 R2 decision/spec。 | [`WorkflowNodeJob.Attempt`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/domain/model/workflow.go#L332-L365)；[`000016_workflow_node_job`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/migrations/postgres/000016_workflow_node_job.up.sql#L1-L30)；[`WorkflowNodeExecutionPolicy`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/domain/model/workflow_execution_policy.go#L14-L38) |
| Durable Job result idempotency | `workflow_node_job_result` 对 Job 唯一，也对 `(job_uuid,idempotency_key)` 唯一；commit 锁 Job/result，验证 command+token；同一 replay 返回既有 receipt，冲突 replay 拒绝。 | `idempotency_key`、`outcome`、committed/consumed timestamps、durable receipt before coordinator consume。 | 作为 ChangeSet receipt 的直接事务范式；但 ChangeSet key/fingerprint、baseline、claim UUID/attempt/fence 和 affected member set 必须新增，不能只把 Material delta 塞进 `return_info`。 | [`WorkflowNodeJobResult`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/domain/model/workflow.go#L382-L397)；[`CommitJobOutcome`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/service/edge/job.go#L74-L168)；[`000024_device_execution`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/migrations/postgres/000024_device_execution.up.sql#L53-L87) |
| Result application and lease release | Coordinator 锁 unconsumed result，更新非 terminal Job，settle start command；terminal evidence 成立后释放 active/uncertain lease并 consume receipt。 | “durable terminal fact first, release second”；late result 可成为 recovery 证据。 | OS 的 `workflow.db` 与 `inventory.db` 已由 M1R 明确分库，不能复制 Backend 的单库原子事务。M1EF 必须让 ChangeSet、Material/Site facts、receipt、ledger/outbox 在一个 inventory UoW 原子提交，再由 EdgeScheduler 以 durable saga 幂等投影 Job terminal，最后释放 Claim；如果缺少可信 reality evidence，Claim 进入 `uncertain`，不释放给下一 Job。 | [`applyWorkflowNodeJobResult`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/repository/postgres/workflow_job_result.go#L79-L216)；[late result/release test](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/scheduler/device_dispatcher_integration_test.go#L823-L899) |
| Material state writes | `AppendMaterialState` 以 Material row lock 串行 append history；若新记录为 latest，投影 `state_data` 到 `material.data`。 | row lock、append-only observed fact、latest projection。 | M1EF ChangeSet 不能直接调用为多个实体逐项 append：它缺少 ChangeSet receipt、expected version、claim/fence 和跨 Material/Site all-or-none 语义。 | [`AppendMaterialState`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/repository/postgres/material_state.go#L16-L57)；[`MaterialStateHistory`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/domain/model/state.go#L9-L20) |
| Site placement write | Material aggregate update 先清旧 Site occupant，再以 conditional occupancy update 设置新 Site，冲突即 rollback。 | occupancy 的数据库约束和 conditional update。 | ChangeSet 应复用该 invariant；所有 Material/Site updates 与 receipt 在 inventory UoW 原子提交，随后以 saga 投影 Job terminal、释放 Claim，不能在 Action 后按多个 HTTP CRUD 调用补偿。 | [`UpdateMaterialAggregate`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/repository/postgres/resource.go#L178-L263) |
| Timeout / safe cancel | unsent start command 只有 `sent_count=0` 才能撤销并释放 lease；如果 transport 可能已发送，就进入 `execution_unknown`，lease 变 `uncertain`。cancel ACK 不是设备已停止的 terminal evidence。 | `dispatch_deadline/execution_deadline/cancel_ack_deadline/cancel_complete_deadline`；safe-unsent fast path；uncertain on ambiguous transport。 | Claim release必须沿用 evidence rule；timeout/cancel 不能因本地 deadline 到期自动释放 Material/Site。M1EF 要把 unknown reason、fence state 和 operator action 写入持久 audit。 | [`WithdrawUnsentDeviceJob`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/repository/postgres/workflow_device_execution.go#L171-L268)；[`MarkDeviceJobExecutionUnknown`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/repository/postgres/workflow_timeout.go#L182-L274)；[`reconcileTimeouts`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/scheduler/timeout.go#L14-L313) |
| Restart / Edge reconciliation | recovery scan 包含 active Task、cleanup、unconsumed result/feedback。Edge hello 上报 `running_jobs`；DB active 但 Edge missing -> unknown，可信 reconnect 可 restore running/cancel_requested，terminal ghost 保留 requires-attention。 | `execution_unknown`、`uncertain`、`waiting_reconciliation`、`requires_attention`、`uncertainty_reason`、`reconciliation_resume_status`。 | M1EF startup recovery 要额外扫描 active/uncertain Claims、ChangeSet receipt 与 Material/Site version；Edge reconnect 只能提供执行证据，不能越权声明物料现实。必须定义人工 resolve/commit-or-abandon seam。 | [`ListWorkflowTasksForRecovery`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/repository/postgres/workflow_execution.go#L15-L75)；[`ReconcileHello`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/service/edge/reconciliation.go#L16-L198)；[`RestoreExecutionUnknownJob`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/repository/postgres/edge_reconciliation.go#L48-L203)；[reconciliation E2E](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/scheduler/edge_reconciliation_integration_test.go#L15-L348) |
| Soft delete under active execution | Base entities使用 GORM soft delete；Material tree delete 清 occupancy 和 WorkflowNode material binding。该 delete path 未检查 active execution lease/Task reservation/Claim/fence。 | `deleted_at`、active partial indexes、历史可保留。 | M1EF 必须禁止删除任何 active Reservation、active/uncertain Claim 或未解决 ChangeSet 所保护的 Material/Site；不能直接继承 Backend delete behavior。 | [`BaseModel.DeletedAt`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/domain/model/base.go#L10-L16)；[`DeleteMaterialTree`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/repository/postgres/resource.go#L376-L464) |
| Manual reconciliation surface | Edge admin handler 只有 edges/devices read projection；未发现 manual unlock、operator Material/Site reality commit API。 | operator read projection 可以独立于 execution mutation。 | M1EF spec 必须决定最小 operator resolution port、权限、审计和幂等语义；在 decision 冻结前不能用“手工改 DB”或无 fence 的 unlock endpoint 补位。 | [`RegisterEdgeAdminRoutes`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/web/handler/edge_admin.go#L18-L22)；production absence search 见 §7。 |

## 4. Backend 已证明的并发与恢复性质

以下不是仅有表结构，而是最新 Backend tests 已覆盖的行为；M1EF 测试可复用其场景词汇，
但需要把断言扩展到完整 Material/Site claim-set 与 fence：

- 同一 device contention：两个 Task 只能一个进入 dispatched，另一个保持 pending；
  [test](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/scheduler/device_dispatcher_integration_test.go#L221-L253)。
- lock order：并发 readiness/reconciliation 不死锁；
  [test](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/scheduler/device_dispatcher_integration_test.go#L315-L396)。
- dispatch commit 前故障完全 rollback，恢复后可再次调度；
  [test](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/scheduler/fault_injection_integration_test.go#L20-L82)。
- durable outcome 已存在时，乱序 `started` 不得把 Job 倒退；
  [test](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/scheduler/fault_injection_integration_test.go#L84-L177)。
- Edge 断开、取消、late outcome、重连恢复和 terminal ghost 均保留持久不确定性；
  [test](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/scheduler/edge_reconciliation_integration_test.go#L15-L348)。

M1EF 在上述 tests 的基础上至少增加：同一业务 Material/Site 跨 Job contention、attempt N 的
late callback 被 attempt N+1 fence 拒绝、ChangeSet 同 key replay/不同 payload conflict、
terminal commit crash/reopen、unknown Claim 重启不释放、人工 resolution 不误释放另一 Job 的
member。

## 5. `ExecutionLockLease` 与 M1EF `ExecutionClaim` 的边界

Backend 的 accepted ADR 已明确：设备锁必须持久化；锁与 Job/command/status 原子提交；只有
durable terminal 或明确安全取消才能释放；断连/取消不确定时转 `uncertain`，不得用 TTL
自动释放。process mutex 和 advisory lock 都不能替代持久事实。
[ADR-0003](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/docs/adr/0003-use-persistent-device-execution-locks.html#L63-L93)

这应成为 M1EF 的底线，但不等同于完整 Claim：

| Backend lease | M1EF Claim |
|---|---|
| 一个 `material_uuid` 设备锁 | selected device + mutable business Material + occupancy 可变 Site 的 complete set |
| owner 有 Task/Job，无 attempt | owner 固定为 `job_uuid + attempt` |
| 状态可 `uncertain` | `reserved/running/uncertain/released`，所有未 released state 都持有 fence，并保留 resolution audit |
| command access token 防伪 | authority-side monotonic fence 防 stale writer |
| terminal result 后释放 | inventory ChangeSet + receipt 原子提交、workflow Job terminal 幂等投影完成后才释放；未知 reality 保持 `uncertain` |

因此最小迁移方向是**深化**现有 durable lease 思路，而不是从零另造一套无关锁，也不是把
Backend 表原样复制后宣称 M1EF 已完成。

## 6. 冻结 FE 合同与最新 Backend 的差异

冻结 `09609a2` 仍然是 OS `AGENTS.md` 指定的 frontend-facing authority；最新 Backend
只能作为 M1EF 内部事务参考。已确认的显著漂移如下：

| 面向 FE 的差异 | `09609a2` 冻结状态 | 最新 `2a3591e` 状态 | M1EF 处理 |
|---|---|---|---|
| WorkflowTask input/output | Task model 与 create request 含 `input`，Task model 含 `output`。[model](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/09609a27e652c9e56ede636a2883a4fd241e4400/internal/model/workflow.go#L95-L119) [handler](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/09609a27e652c9e56ede636a2883a4fd241e4400/internal/http/handler/workflow.go#L350-L357) | migrations `000037`/`000040` 删除 Task input/output，latest create request 不含 input。[drop input](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/migrations/postgres/000037_remove_workflow_task_input.up.sql#L1) [drop output](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/migrations/postgres/000040_remove_workflow_task_output.up.sql#L1) [handler](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/web/handler/workflow.go#L357-L363) | 不采纳该漂移；I1/O1 和当前 FE 合同保持独立 gate。M1EF 不触碰 Task I/O surface。 |
| Material identifier field | frozen Material HTTP request 使用 `code`。[handler](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/09609a27e652c9e56ede636a2883a4fd241e4400/internal/http/handler/resource.go#L78-L113) | latest shared model/HTTP 使用 `barcode`。[handler](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/web/handler/resource.go#L74-L132) | 依已作出的 M1 Backend-field decision 使用 `barcode`，但这是明确的 supersession，不是以 latest route 偷换 frozen contract。 |

## 7. 缺失能力的可复现搜索证据

下列命令均在 detached `2a3591eaff21d808557e6a645f9092b152fb3504` production tree
执行；范围限定 `internal/**` 与 `migrations/postgres/**`，并排除 tests。退出码 `1` 表示无匹配：

```bash
git grep -n -E 'ResourceSlot|resource_slot' 2a3591eaff21d808557e6a645f9092b152fb3504 -- 'internal/**' 'migrations/postgres/**' ':!**/*_test.go'
git grep -n -E 'MaterialChangeSet|material_change_set|ChangeSet|changeset|change_set' 2a3591eaff21d808557e6a645f9092b152fb3504 -- 'internal/**' 'migrations/postgres/**' ':!**/*_test.go'
git grep -n -E 'fence|fencing|fenced|generation' 2a3591eaff21d808557e6a645f9092b152fb3504 -- 'internal/**' 'migrations/postgres/**' ':!**/*_test.go'
git grep -n -E 'material_reservation|MaterialReservation|task_reservation|TaskReservation' 2a3591eaff21d808557e6a645f9092b152fb3504 -- 'internal/**' 'migrations/postgres/**' ':!**/*_test.go'
git grep -n -E 'dispatch_unknown' 2a3591eaff21d808557e6a645f9092b152fb3504 -- 'internal/**' 'migrations/postgres/**' ':!**/*_test.go'
```

`job_claim|JobClaim|claim_uuid|claim_owner` 的同范围搜索只有 `LocalJobClaim` 相关命中：

- [`repository.LocalJobClaim`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/repository/types.go#L188-L191)；
- [`ClaimLocalJob`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/repository/postgres/workflow_local_execution.go#L108-L218)；
- [`WorkflowRuntimeRepository` contract](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/repository/workflow_runtime_contract.go#L70-L77)。

它在一个数据库事务内把 local Job 从 dispatched 转为 running 并返回 Task+Job 内容，没有
claim row、member set 或 fence；所以不构成 M1EF durable physical Claim。

同理，production tree 中没有 manual unlock / Material reality reconciliation handler；现有
Edge admin 注册面仅为 `GET /edges` 与 `GET /edges/{edge_uuid}/devices`。

## 8. 给 M1EF implementation spec 的强制输入

基于本审计，M1EF spec 可以启动，但以下决策不能跳过：

1. Claim schema：owner `job_uuid + attempt`、monotonic fencing token、complete member set
   （device Material、mutable business Material、occupancy-mutating Site）、状态与 reason；
2. Task Reservation → Job Claim invariant：业务 Material 必须已由同 Task active Reservation
   覆盖；device/Site 是否进入 Reservation 要明确维持“否”，避免扩大 Task lifetime lock；
3. 统一 lock order 和 all-or-none acquisition；冲突输出稳定 409/blocked reason，不能 partial claim；
4. ChangeSet canonical codec、idempotency key/fingerprint、expected baseline/version、receipt 和
   replay/conflict 语义；
5. 分库 saga：ChangeSet Material/Site writes、receipt、ledger/outbox 在单一 inventory UoW
   atomic；Job terminal 在 workflow UoW 幂等投影；Claim release 只能在两边事实均已证明后由
   durable command 完成，任何 crash window 都可 replay；
6. unknown/restart：采用 Backend `execution_unknown/uncertain` 安全底线，补上 fence/member/
   ChangeSet recovery matrix，禁止 TTL release；
7. operator resolution：谁可以将 uncertain reality 决议为 commit/abandon、需要什么 evidence、
   如何防 stale operator command，以及如何审计；
8. soft delete：受 active Reservation、active/uncertain Claim、未解决 ChangeSet 保护的实体拒绝删除；
9. retry 停止线：本轮只让 attempt 成为 Claim identity，不实现 scheduler automatic retry；
10. API 停止线：不因最新 Backend route 漂移而改写冻结 FE API；普通 FE 只消费 read projection，
    operator mutation 另立 protocol gate。

结论：Backend 已经覆盖了“设备 Job 的 durable exclusive lease、结果幂等收据、运输不确定性和
Edge 重连恢复”的主体安全框架，M1EF 不应从零设计这些原则；但它尚未覆盖“Task Reservation
与 Job Claim 分层、完整 Material/Site claim-set、单调 fence、Material/Site ChangeSet 原子幂等
提交和 reality resolution”。这四项正是 M1EF 必须实现的增量边界。
