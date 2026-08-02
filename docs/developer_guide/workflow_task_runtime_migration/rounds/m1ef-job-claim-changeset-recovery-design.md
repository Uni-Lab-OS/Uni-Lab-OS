# M1EF：Job Claim、fencing、Material ChangeSet 与恢复设计

日期：2026-08-02

状态：published repository implementation spec；human-approved；production RED 尚未开始

权威票据：

- OS delivery：[`Uni-Lab-OS/Uni-Lab-OS#15`](https://github.com/Uni-Lab-OS/Uni-Lab-OS/issues/15)
- OS M1 umbrella：[`Uni-Lab-OS/Uni-Lab-OS#6`](https://github.com/Uni-Lab-OS/Uni-Lab-OS/issues/6)
- Core M1 Decision：[`Uni-Lab-OS/Uni-Lab-Core#155`](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/155)
- Core contention/restart gate：[`Uni-Lab-OS/Uni-Lab-Core#156`](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/156)
- M1R Accepted delivery：[`Uni-Lab-OS/Uni-Lab-OS#14`](https://github.com/Uni-Lab-OS/Uni-Lab-OS/issues/14)

实现基点：private `Uni-Lab-OS/Uni-Lab-OS:dev@90f04339424ac2094a089ee30f9c2bfff6e050de`。
其中 M1R/D1A Accepted production tree 来自 `5588b6b697c50533057a0200e0ca8b5174443ca5`；
后续 commit 仅修正 workflow process lease 的可移植 break signal，不改变 Inventory、Scheduler、D1A
或本 spec 合同。

Backend 参考：

- 最新 production reference：
  `Uni-Lab-OS/uni-lab-backend:main@2a3591eaff21d808557e6a645f9092b152fb3504`；
- 当前 `feat/workflow@5b49ba7a35ac87515c27cc532a795ddb1be3fedf` 只比该 SHA 多文档，
  production tree 相同；
- 已冻结 frontend-facing authority 仍是
  `feat/workflow@09609a27e652c9e56ede636a2883a4fd241e4400`，不得被 latest Backend
  漂移静默改写；
- 完整逐文件审计见
  [M1EF Backend 参考审计](m1ef-backend-reference-audit.md)。

本轮每项 OS 决策都先列出 latest Backend 的对应 production 内容，再说明采用、深化或有意不复制
的部分。快速索引如下：

| Decision | latest Backend primitive | OS 处理 |
|---|---|---|
| 1 | 单库 `WorkflowRuntime` + 唯一 dispatcher | 保留唯一 coordinator，按 M1R 改为两库 durable saga |
| 2 | workflow/direct action 共用 execution lease | D1A 与未来 Workflow action 共用 Inventory Claim authority |
| 3 | `reserved/running/uncertain/released` lease | 采用状态词汇，深化 attempt、member set 与 monotonic fence |
| 4 | transaction 内 device lease contention/lock order | 深化为 device/business Material/Site complete set |
| 5 | 窄 repository methods + durable result receipt | 深化现有 `InventoryService` closed commands/results |
| 6 | zero-send proof、execution unknown、无 TTL release | 原样采用安全底线，扩展到 Material/Site fence |
| 7 | Job outcome receipt + Material row lock | 合并深化为 fenced、atomic、multi-entity ChangeSet receipt |
| 8 | 单库 result→terminal→lease release | 按两库边界拆成 C1～C7 可重放 saga |
| 9 | startup/Edge hello/late outcome reconciliation | 采用执行证据，增加 Material reality 与人工 resolution gate |
| 10 | additive migration + soft delete | 保留 M1R v5 数据，增加 in-use/delete/recovery guard |

## 1. Outcome 与 atomic delivery

M1EF 在 M1R 已接受的
`unilabos.app.scheduler.inventory.InventoryService + inventory.db` 上一次性交付：

1. `job_uuid + attempt` 拥有的 complete-set Job Execution Claim；
2. 永不复用的 monotonic fencing token；
3. selected device Material、mutable business Material 与 occupancy-changing Site 的统一占用；
4. deterministic/idempotent Material/Site ChangeSet receipt；
5. `workflow.db ↔ inventory.db` 的 dispatch、terminal、release、restart durable saga；
6. `execution_unknown`、`uncertain`、device/human reconciliation；
7. D1A direct device action 对同一 Inventory Claim authority 的迁移。

Claim、ChangeSet 和 recovery 不可拆成可独立发布的中间态。只交付 Claim 会允许 Action 执行后
无安全 reality commit；只交付 ChangeSet 会缺少 stale writer fence；不同时迁移 D1A 会保留第二套
device occupancy authority。

本轮不实现 M2B selector policy、R2 ExecutionPlan、automatic retry、D1 driver vendor protocol、
O1 Workflow output、Template Catalog/Registry 或 PackageCatalog 重构。M1EF 提供这些阶段消费的
安全 primitive 和 durable command seam。

现有 legacy DAG 的 `@action(lock_resource)` / `_job_resource_locks` 在 R2 typed claim-set wiring 前可
暂留为 compatibility scheduling hint，但它不构成 durable Claim、不能使任何 M1EF gate 通过，也不得
参与 release/recovery/Material reality。M1EF 不再扩展其 value-shape guessing；R2 将所有真实
Workflow Action 接入 Inventory Claim 后再删除该 hint。D1A 已有 formal Task/Job/device identity，必须
在本轮先完成真实 Claim 迁移，不能等待 R2。

## 2. Decision 1：Authority、数据库与 Scheduler 边界

**最新 Backend 参考。** Backend 的 `WorkflowRuntime` 在同一 PostgreSQL transaction 内持有
Task/Job/Edge command 与 execution lease；`DeviceJobDispatcher` 是唯一 dispatch owner。
参见
[`WorkflowRuntime`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/repository/workflow_runtime_contract.go#L11-L60)
与
[`DeviceJobDispatcher.TryDispatch`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/scheduler/dispatch/dispatcher.go#L186-L283)。

**OS 决策。** 保持 M1R 的分库设计，不恢复 shared connection、SQLite `ATTACH` 或
`RuntimeAuthorityUnitOfWork`：

```text
EdgeScheduler
  sole readiness / admission / dispatch / completion coordinator
       │
       ├── workflow.db transaction
       │     WorkflowTask / WorkflowNodeJob / command / projection / runtime outbox
       │
       └── durable Inventory command/result/outbox/ack saga
             │
             ▼
          InventoryService
             │
             └── inventory.db transaction
                   Material / Site / Reservation / Claim / ChangeSet / ledger / outbox
```

- `EdgeScheduler` 是唯一跨库 coordinator 和唯一调用 driver 的 owner；
- `InventoryService` 是唯一 Material/Claim/ChangeSet public seam，不遍历 DAG、不调用 driver；
- `InventoryStore`、SQLite connection 和 row helper 不对 Scheduler、Workflow、HTTP 或 driver 暴露；
- 任一 transaction 内禁止调用另一个 Store、Registry、ROS、driver、HTTP、SSE publish、sleep 或
  PLR refresh；
- 同一 workspace process lease 继续阻止两个 OS writer；外部 Go Scheduler 不进入本轮。

这有意不同于 Backend 的单库原子提交，因此所有跨库原子性要求改写为可重放 saga，不得在文档或
代码中继续声称 ChangeSet 与 Job terminal 同 transaction。

## 3. Decision 2：唯一 Claim authority 与 D1A 收敛

**最新 Backend 参考。** Backend 的 workflow device action 与 direct/ad-hoc device action 都进入
同一个 Coordinator、`DeviceJobDispatcher` 和 `execution_lock_lease`；direct request 的
idempotency fingerprint 不会建立第二套设备锁。参见
[`CreateDeviceActionRun`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/service/workflow/device_action_run.go#L50-L100)、
[`buildDeviceActionExecution`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/service/workflow/device_action_run.go#L180-L279)
和
[`000045_ad_hoc_device_action`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/migrations/postgres/000045_ad_hoc_device_action.up.sql#L1-L35)。

**OS 决策。** `inventory.db.material_claim` 是 M1EF 后唯一 durable physical execution right。
当前 `workflow.db.device_action_task.claim_status` 降为 D1A read/projection state，不再独立决定设备
是否可用：

| D1A projection | Inventory authority |
|---|---|
| `pending` | 尚无 Claim |
| `claimed` | Claim `reserved` 或 `running` |
| `unknown` | Claim `uncertain` |
| `released` | Claim `released` |

D1A-S1 的无业务物料 Action 仍必须取得只含 selected device Material 的 device-only Claim，并在
terminal outcome 时提交 deterministic no-op ChangeSet receipt。它不要求 Task Material Reservation；
一旦 Claim 含 business Material，全部 existing business members 必须由同一 Task 的 active
Reservation 覆盖。

`workflow.db.device_action_task` 精确增加下列 logical projection；均不建跨库 FK：

```text
inventory_claim_uuid                  TEXT NULL
inventory_fencing_token               INTEGER NULL CHECK (> 0)
inventory_claim_set_fingerprint        TEXT NULL
material_changeset_uuid                TEXT NULL
material_changeset_fingerprint         TEXT NULL
material_changeset_outbox_sequence     INTEGER NULL CHECK (> 0)
workflow_terminal_fingerprint          TEXT NULL
```

Claim acquire 前全部为空；取得 Claim 后前三项必须全有；ChangeSet commit 后中间三项必须全有；
Job terminal projection 后最后一项必须有。`claim_status` 只按上表映射 Inventory state，不能覆盖
inventory truth。启动恢复、busy-device provider 和 manual unlock 都先读 Inventory Claim；workflow
projection 落后时由 saga 修复。现有 `force_unlock` 可以清理本地 manager/queue 锁，但不得直接清除
reserved/running/uncertain Inventory Claim；只要 Inventory Claim 仍 live，Scheduler busy guard 继续
阻止后续 dispatch。Material reality resolution 必须进入带 evidence、owner、attempt、token 的
reconciliation command。

ResourceTreeSet bootstrap 已把 device node 投影为 `material_kind=device` Material。D1A 的
`device_id` 只允许由 `InventoryService.resolve_executor_material(device_id)` 在 Inventory projection
boundary 读取 `meta_data.source=resource-tree-set`、exact `source_node_id` 且
`material_kind=device/deleted_at IS NULL` 的唯一行；零行或多行均 fail closed，不做 name/path/barcode
fallback。Claim identity、member、index 和 fence 一律使用返回的 Material UUID，不用 device
name/path 作最终键。

## 4. Decision 3：Claim schema 与 Backend-shaped lifecycle

**最新 Backend 参考。** Backend `ExecutionLockLease` 已冻结 Task/Job owner、device Material、
`reserved/running/uncertain/released` 和 active partial unique index，但没有 attempt、member set 或
monotonic fence。参见
[`ExecutionLockLease`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/domain/model/workflow.go#L367-L380)
与
[`000024_device_execution`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/migrations/postgres/000024_device_execution.up.sql#L17-L51)。

**OS 决策。** 使用下列表，不复制 Backend 表，也不恢复旧
`material_execution_claim` 命名：

```text
material_claim
  uuid                         TEXT PRIMARY KEY
  workflow_task_uuid           TEXT NOT NULL      # cross-DB logical reference
  workflow_node_job_uuid       TEXT NOT NULL      # cross-DB logical reference
  attempt                      INTEGER > 0
  set_fingerprint              TEXT NOT NULL
  fencing_token                INTEGER > 0 UNIQUE
  state                        reserved|running|uncertain|released
  uncertainty_reason           TEXT NULL
  acquired_at                  TEXT NOT NULL
  create_time                  TEXT NOT NULL
  running_at                   TEXT NULL
  release_proof_kind           TEXT NULL
  release_proof_fingerprint    TEXT NULL
  release_reason               TEXT NULL
  terminal_changeset_uuid      TEXT NULL
  workflow_terminal_fingerprint TEXT NULL
  release_command_uuid         TEXT NULL UNIQUE
  released_at                  TEXT NULL
  update_time                  TEXT NOT NULL
  UNIQUE(workflow_node_job_uuid, attempt)

material_claim_member
  claim_uuid                   TEXT -> material_claim.uuid
  resource_kind                device_material|business_material|site
  resource_uuid                TEXT NOT NULL
  acquired_version             INTEGER > 0
  expected_version             INTEGER > 0
  released_at                  TEXT NULL
  PRIMARY KEY(claim_uuid, resource_kind, resource_uuid)

material_claim_fence_sequence
  sequence                     INTEGER PRIMARY KEY AUTOINCREMENT
  claim_uuid                   TEXT NOT NULL UNIQUE

material_resource_fence
  resource_kind                device_material|business_material|site
  resource_uuid                TEXT NOT NULL
  fencing_token                INTEGER > 0
  claim_uuid                   TEXT NOT NULL
  update_time                  TEXT NOT NULL
  PRIMARY KEY(resource_kind, resource_uuid)
```

`material_claim_member` 对 `(resource_kind, resource_uuid)` 建 `released_at IS NULL` partial unique
index；`uncertain` member 仍保持 active，只有 `released` 时统一写 `released_at`。resource fence 在
release 后保留最新 token，不能删除或回退。sequence 对已 commit 的 token 只分配不复用，允许编号
空洞。只有已 commit、因而可能被观察到的 token 被禁止复用或倒退；SQLite rollback 后从未公开的
rowid 是否再次分配不是安全语义，测试不得把未提交数字当作已发 fence。

state transition 只允许：

```text
reserved  -> running | uncertain | released(not_submitted proof only)
running   -> uncertain | released(terminal-settled proof only)
uncertain -> running | released(reconciled terminal/not-submitted proof only)
released  -> <none>
```

进入 `released` 时 `release_proof_kind/release_proof_fingerprint/release_command_uuid/released_at`
必须全有。`not_submitted` proof 不得带虚假 ChangeSet；`terminal_settled` 与
`reconciled_terminal` 必须同时冻结 `terminal_changeset_uuid` 和
`workflow_terminal_fingerprint`。这些约束由 schema CHECK 与 service state machine 双层执行。

本 spec 采用 latest Backend 的 state vocabulary，并明确 supersede 旧 M1 草案的
`active/fenced/released` 实现命名：

- `reserved`：complete Claim 已提交，尚无可靠的 physical-start evidence；
- `running`：driver/Edge 已给出匹配 Job 的 accepted/running evidence；
- `uncertain`：可能已 dispatch、cancel 未确认、result reality 未确认或 post-action persistence
  未 settle；
- `released`：只在安全 pre-dispatch failure，或 ChangeSet + Job terminal saga 已 settle 后进入。

所有 `reserved/running/uncertain` Claim 都持有 fence；“fenced”是性质，不再是独立 state。

## 5. Decision 4：complete claim-set、Reservation invariant 与 lock order

**最新 Backend 参考。** Backend 在一个 transaction 中先锁 Edge agent/binding，再以
`ON CONFLICT DO NOTHING` 插入唯一 active lease；冲突时 Job 保持 pending。并发 coordinator 还按稳定
Edge/Job UUID 顺序加锁。参见
[`ReserveDeviceExecution`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/repository/postgres/workflow_device_execution.go#L18-L90)
与
[`lockRequiredEdgesAndJobs`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/scheduler/coordinator.go#L1126-L1175)。

**OS 决策。** `acquire_job_claim()` 在一个 `BEGIN IMMEDIATE` transaction 中：

1. canonicalize `workflow_task_uuid/job_uuid/attempt` 与 exact typed roots；
2. 验证 selected device Material 存在、未删除且 `material_kind=device`；
3. 展开每个 mutable business Material root 的当前 composition subtree；
4. 加入每个 occupancy-changing Site、当前 occupant 和 caller 明确的目标 occupant；
5. deduplicate，并按 `device_material -> business_material -> site`、再按 UUID 排序；
6. 验证每个 existing business member 都在同 Task active Reservation 内；device 与 Site 不扩大为
   Task-lifetime Reservation；
7. 验证 Material/Site disposition、soft delete、version、allowlist、occupancy 与 live Claim；
8. 分配 fence token，原子写 header、全部 members、resource fences、ledger、outbox、processed result；
9. commit 后返回完整 Claim；任何冲突均 header/member/fence/ledger/outbox zero-write。

claim set 只能来自 A1/R2 typed contract 或 D1A 明确的 device-only request；禁止扫描字段名、Action
名称、port ordinal、barcode、PLR object identity 或 Python runtime value。Site 当前 occupant 若未被同
Task Reservation 覆盖，属于确定性 `claim_set_not_reserved`，不能偷偷扩大 Reservation。

结果使用 closed `acquired|blocked|rejected`：

- 其他 Claim、executor busy 或暂时 state contention 为 `blocked`，Job 保持 pending；
- malformed set、stale Task Reservation、kind/template mismatch 为 `rejected`；
- authority/DB 不可用抛 `material_authority_unavailable`，dispatch fail closed；
- 同 `(job_uuid, attempt)` 同 set replay 原 Claim/token；不同 set 为 conflict；
- 新 attempt 只有旧 Claim 已 released 才可取得更大 token。本轮不自动创建 attempt 2。

## 6. Decision 5：InventoryService 与 durable commands

**最新 Backend 参考。** Backend `WorkflowRuntime` 把 reserve、bind dispatch、timeout、withdraw、result
consume 分成窄 repository methods；Job result 以 idempotency key 和 durable receipt replay。参见
[`WorkflowRuntime`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/repository/workflow_runtime_contract.go#L42-L77)
与
[`WorkflowNodeJobResult`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/domain/model/workflow.go#L382-L397)。

**OS 决策。** 深化现有 public Interface，不增加第二 facade：

```python
class InventoryService:
    def resolve_executor_material(device_id: str) -> MaterialRecord: ...
    def acquire_job_claim(command: JobClaimAcquireCommand) -> JobClaimResult: ...
    def mark_job_claim_running(command: JobClaimStateCommand) -> JobClaimResult: ...
    def mark_job_claim_uncertain(command: JobClaimUncertainCommand) -> JobClaimResult: ...
    def commit_material_changeset(
        command: MaterialChangeSetCommand,
    ) -> MaterialChangeSetReceipt: ...
    def release_job_claim(command: JobClaimReleaseCommand) -> JobClaimResult: ...
    def resolve_job_claim(command: JobClaimResolutionCommand) -> JobClaimResult: ...
    def get_job_claim(job_uuid: str, attempt: int) -> JobClaimRecord: ...
    def list_unsettled_claims(...) -> tuple[JobClaimRecord, ...]: ...
```

`inventory/commands.py` 增加 closed versioned types：

```text
material.claim.acquire
material.claim.running
material.claim.uncertain
material.changeset.commit
material.claim.release
material.claim.resolve
```

每条 command 都有 `schema_version=1`、canonical `command_uuid`、`idempotency_key` 和 canonical
payload hash。`processed_command` 保持同 command/same payload replay、same key/different payload
conflict；blocked result 可用原 command retry 并在条件变化后前进，不能被误冻结成永久 completed。

本轮不新增普通 FE mutation route。只有 `EdgeScheduler` 调用 Claim/ChangeSet mutation methods；
D1A bridge 只把 formal Task/Job/device/outcome intent 适配给 Scheduler并接受 workflow projection
callback，不能自行成为跨库 coordinator。未来 R2/D1 也只能从 Scheduler 进入该注入 Interface。
operator HTTP/CLI surface 需另立 protocol gate。

## 7. Decision 6：dispatch、cancel 与 Claim lifecycle

**最新 Backend 参考。** Backend 只有 `job.start` 的 `sent_count=0` 才能证明未发送并释放 lease；
transport 可能已发送、cancel ACK 超时或执行结果不明时，Job 进入 `execution_unknown`、lease 进入
`uncertain`，没有 TTL 自动释放。参见
[`WithdrawUnsentDeviceJob`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/repository/postgres/workflow_device_execution.go#L171-L268)
与
[`MarkDeviceJobExecutionUnknown`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/repository/postgres/workflow_timeout.go#L182-L274)。

**OS 决策。** dispatch 顺序固定为：

```text
Inventory Claim commit (reserved)
  -> workflow.db project claim UUID/token
  -> workflow.db Job pending -> dispatched commit
  -> COMMIT workflow transaction
  -> call Scheduler backend / driver outside both transactions
  -> accepted/running evidence
  -> Inventory Claim reserved -> running command
  -> workflow running projection
```

driver 调用前必须已有 durable `dispatched` fact；因此该 fact 之后的崩溃或普通 exception 不能自动
release Claim。只有 backend 返回可审计的 `not_submitted` proof，且 workflow dispatch journal/transport
state 同时证明零发送时，才可执行 pre-dispatch release。裸 exception、timeout、process death、取消
request 或 cancel ACK 均不是安全释放证据。

`mark_job_claim_uncertain`：

- 保留 member/resource fence；
- 写 stable `uncertainty_reason`、ledger/outbox；
- 对可能已被动作改变的 business members，将 `disposition=active` 改为 `reconciling`、实际 bump
  version，并把 member `expected_version` 更新为该内部 transition 后的 version；
- device-only D1A Claim 不虚构 business disposition；
- 同 command replay no-op，旧 attempt/token zero-write conflict。

Task cancel 只记录 intent并请求设备停止。只要 physical action 可能执行，Claim 保持 running/uncertain；
Task Reservation 也保持 active。`InventoryService.release_task()` 增加 active/uncertain Claim guard：
存在未 settle Claim 时返回 retryable `blocked`，Scheduler 保留 cleanup pending 并在 Claim release 后重试。
`TaskMaterialReleaseResult.status` 因此精确扩展为 `blocked|released`；同 command/same payload 的
`blocked` 不是 completed receipt，条件变化后必须能推进到 `released`。相应
`workflow_task_material_release_projection.status` CHECK 改为 `blocked|released`，只允许同 command
从 blocked 单向更新到 released；不得用新 command identity 绕过 Claim guard。

`workflow_terminal_fingerprint` 固定为 canonical JSON 的 SHA-256，输入仅含 `job_uuid`、`attempt`、
terminal Job status、ChangeSet UUID/fingerprint/outcome、canonical `return_info` 与 `error_info`；排除
`finished_at/update_time` 等 wall clock。相同 receipt replay 必须得到相同 fingerprint。

## 8. Decision 7：Material/Site ChangeSet schema 与幂等语义

**最新 Backend 参考。** Backend Job outcome 已实现 Job 唯一 receipt、idempotency key、command/token
验证和 late result consume；Material state append 则只有 Material row lock/latest projection，没有
Job/claim/fence/ChangeSet identity。参见
[`CommitJobOutcome`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/service/edge/job.go#L74-L168)、
[`applyWorkflowNodeJobResult`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/repository/postgres/workflow_job_result.go#L79-L216)
和
[`AppendMaterialState`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/repository/postgres/material_state.go#L16-L57)。

**OS 决策。** inventory.db 增加：

```text
material_changeset
  uuid                         TEXT PRIMARY KEY
  workflow_task_uuid           TEXT NOT NULL
  workflow_node_job_uuid       TEXT NOT NULL
  attempt                      INTEGER > 0
  claim_uuid                   TEXT NOT NULL -> material_claim.uuid
  fencing_token                INTEGER > 0
  effect_identity              TEXT NOT NULL
  deterministic_fingerprint    TEXT NOT NULL
  outcome                      succeeded|failed|canceled|timeout
  result_json                  JSON object NOT NULL
  create_time                  TEXT NOT NULL
  UNIQUE(workflow_node_job_uuid, attempt, effect_identity)

material_changeset_effect
  changeset_uuid               TEXT -> material_changeset.uuid
  effect_key                   TEXT NOT NULL
  resource_kind                business_material|site
  resource_uuid                TEXT NOT NULL
  operation                    create|update|reparent|soft_delete|set_occupancy
  expected_version             INTEGER NULL
  before_json                  JSON object NOT NULL
  after_json                   JSON object NOT NULL
  PRIMARY KEY(changeset_uuid, effect_key)
```

M1EF v1 只接受每个 `job_uuid + attempt` 的一个 terminal effect identity `terminal`；字段保留明确
identity，避免以后用随机 command UUID 充当业务 effect。canonical fingerprint 包含 owner、attempt、
claim UUID/token、全部按稳定 key 排序的 operation、expected version、before/after canonical value 与
outcome；排除 authority 分配的 create/update/delete audit time、trace text 和 transport noise。

`commit_material_changeset()` 在一个 inventory transaction 中：

1. 查 receipt；同 owner/effect/same fingerprint 返回原 receipt，不写第二次；不同 fingerprint conflict；
2. 验证 Claim state 为 running/uncertain、owner/attempt/token exact match；
3. 验证每个 existing affected entity 是 Claim member，version 等于 member current expected version；
4. 验证 composition cycle、Site owner/occupant/allowlist、soft delete、disposition 等 invariant；
5. 只更新真实变化，实际变化才 bump aggregate version；
6. 写 receipt、effect audit、inventory ledger、outbox 与 processed result后一起 commit。

no-op action仍写 receipt，不 bump Material/Site version、不伪造 aggregate ledger。已确认 physical reality
必须提交，即使 A1 scalar/result validation 随后令 Job failed；无法确认 reality 则不写 receipt、不发布
stale output，Claim 转/保持 uncertain。

本轮提供 ChangeSet repository primitive 和 D1A device-only no-op wiring；从完整 Workflow Action
Mutation Session 生成业务 changeset 属于 D1 后续，但不得再绕开本 primitive 直接写 Material/Site。

## 9. Decision 8：跨库 terminal saga 与 crash windows

**最新 Backend 参考。** Backend 在单库中先持久化 Job result receipt，再由 Coordinator apply result、
settle command、release lease；late result 可解决 `execution_unknown`。参见
[`workflow_node_job_result`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/migrations/postgres/000024_device_execution.up.sql#L53-L87)
和
[`workflow_job_result.go`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/repository/postgres/workflow_job_result.go#L14-L235)。

**OS 决策。** terminal 顺序固定为：

```text
driver/device/human terminal evidence
  -> Inventory ChangeSet + receipt + facts + ledger/outbox COMMIT
  -> workflow.db Job result/terminal + Task projection COMMIT
  -> Inventory Claim release COMMIT
  -> if all Task Claims settled: Task Reservation release saga
  -> ack inventory outbox/result
```

Job 可以先成为 terminal projection 而 Claim 尚未 release，但 Task `cleanup_status` 必须保持
pending/requires-attention，Task Reservation release 会被 Inventory guard 阻止。Claim release command
必须携带 exact receipt fingerprint、Job outcome projection fingerprint、attempt 和 token；不能仅凭
`Task.status=terminal` 释放。Inventory 在同一 release transaction 把 ChangeSet UUID、
`workflow_terminal_fingerprint`、release command UUID/reason 和 released time 写入 Claim header，
再统一释放 members；同 proof replay 原 result，不同 proof zero-write conflict。

强制 fault injection：

| window | durable state | restart/replay outcome |
|---|---|---|
| C1：Claim commit 后、workflow claim projection 前 | inventory reserved Claim | replay同 Claim/token，再完成 projection；取消且有 no-dispatch proof 才释放 |
| C2：workflow Job dispatched 后、driver call 前/中 | Job dispatched，Claim reserved | 进入 execution_unknown/uncertain；不能凭“可能尚未调用”释放 |
| C3：driver accepted 后、Claim running 前 | Job dispatched/running evidence 可能不齐 | device query/late result/replay推进 running或uncertain，不重新派发 |
| C4：ChangeSet commit 后、Job terminal 前 | receipt + reality 已提交，Claim live | replay receipt，幂等投影相同 Job terminal |
| C5：Job terminal 后、Claim release 前 | terminal projection + live Claim | replay exact release；其他 Job仍被挡住 |
| C6：Claim release 后、Task Reservation release 前 | Job settled，Task Reservation active | replay terminal cleanup/release_task，不误释放其他 Task |
| C7：任一 inventory outbox 后、ack 前 | facts/result 已提交 | replay同 event/result，不产生第二 effect、feedback 或 runtime event |

所有 saga command UUID 从 Task/Job/attempt/effect/phase deterministic derivation；随机 retry command
不得绕过 idempotency。

## 10. Decision 9：startup recovery 与 device/human reconciliation

**最新 Backend 参考。** Backend startup 扫描 active Task/unconsumed result；Edge hello 上报 running
Jobs，DB/Edge 不一致进入 `execution_unknown/uncertain/waiting_reconciliation/requires_attention`，
可信 late outcome 或 reconnect 可恢复，terminal ghost 不自动解锁。参见
[`ReconcileHello`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/service/edge/reconciliation.go#L16-L198)
与
[`RestoreExecutionUnknownJob`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/repository/postgres/edge_reconciliation.go#L48-L203)。

**OS 决策。** process lease 后、对外 ready/worker/dispatch 前：

1. 打开并验证/migrate inventory schema；
2. Workflow runtime 把 restart 时的 in-flight Job 投影为 `execution_unknown`，结束 workflow tx；
3. EdgeScheduler 读取 Workflow recovery facts与 Inventory unsettled Claims；
4. 用 durable commands 将对应 Claim 变为/保持 uncertain，并标记 business Material reconciling；
5. 审计 claim header/member/resource fence/receipt/version 完整性；
6. replay C1～C7 saga；
7. 从 durable Inventory facts rebuild MaterialGraph/ResourceTreeSet projection；
8. 只有无 corruption 且所有 dispatcher guard ready 才启动 dispatch。

恢复矩阵：

- reserved Claim + pending Job +完整 saga intent：重放 claim projection；Task 已取消且证明从未 dispatch 才释放；
- reserved/running Claim + dispatched/running/cancel_requested/execution_unknown Job：running 或 uncertain，
  永不自动 release；
- ChangeSet receipt + nonterminal Job：投影 receipt 中冻结的 terminal outcome；
- terminal Job +无匹配 receipt：uncertain/requires-attention，不能伪造 no-op receipt；
- released Claim + nonterminal Job：authority corruption，fail closed；
- member/resource-fence 缺失、额外 member、token mismatch 或 version 倒退：authority corruption，
  不重建成 available。

最小 internal `resolve_job_claim` command 必须包含：operator/device identity、evidence kind、observed time、
evidence fingerprint、resolution、expected Claim state、job UUID、attempt、token 和 stable reason。closed
resolution：

- `confirmed_running`：uncertain -> running；
- `confirmed_not_dispatched`：仅在 coordinator 同时提供 durable no-send proof 时 release；
- `confirmed_terminal`：必须携带 terminal ChangeSet/outcome，进入 C4～C6；
- `quarantine_and_fail`：以 ChangeSet 把受影响 business Material 置 quarantined，再投影 Job failed并释放；
- `unresolved`：保持 uncertain/reconciling，只更新审计，不释放。

禁止 `force release`、TTL、手工 SQL 或“Edge online”直接证明 Material reality。当前 Backend 也没有
manual Material reality API；M1EF 先冻结 internal port，任何 FE/admin mutation route 另立协议。

## 11. Decision 10：schema upgrade、soft delete、projection 与共享 API

**最新 Backend 参考。** Backend 使用顺序 migration 添加 device execution/ad-hoc fields，并以
`deleted_at` 保留历史；但其 Material delete path不检查 M1EF Claim/fence。参见
[`000024_device_execution`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/migrations/postgres/000024_device_execution.up.sql#L1-L87)、
[`000045_ad_hoc_device_action`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/migrations/postgres/000045_ad_hoc_device_action.up.sql#L1-L35)
与
[`DeleteMaterialTree`](https://github.com/Uni-Lab-OS/uni-lab-backend/blob/2a3591eaff21d808557e6a645f9092b152fb3504/internal/repository/postgres/resource.go#L376-L464)。

**OS 决策。** `InventoryStore.SCHEMA_VERSION` 从 5 升到 6。M1R 之前放弃 legacy inventory 数据的
裁决不表示后续每轮继续删库；M1EF 必须保留已经接受的 v5 Material/Site/Reservation/lot/ledger：

- exact v5 schema 才允许在一个 exclusive transaction additive migrate 到 exact v6；
- migration 创建 Claim/ChangeSet tables/indexes，写 schema receipt，再设置 user_version=6；
- migration crash 后要么完整 v5，要么完整 v6；mixed/corrupt/未知 version 继续 fail closed；
- 禁止 fallback、dual-read、dual-write 或第二 database；
- v5 本身没有 Inventory Claim，所以不生成虚假 claim history。

同一 candidate 还必须对 `workflow.db` 做 additive schema upgrade：增加 Decision 2 的 D1A logical
projection columns，并把 Task Material release projection status 从 `released` 扩成
`blocked|released`。这仍是 WorkflowStore 自己的 transaction；不得在 upgrade 中打开或修改
`inventory.db`。两库任一先升级后进程崩溃时，startup recovery 都必须按 C1～C7 幂等收敛，不能要求
两个 schema migration 跨库原子。

现存 workflow.db D1A rows 单独恢复：`pending/released` 不导入 active Claim；`claimed/unknown` 必须按
唯一 device Material mapping 创建或复用直接进入 uncertain 的 recovery Claim。映射缺失、同设备多个
live holder 或 set 冲突时 OS 不对 physical dispatch ready，保留旧 busy projection并要求人工处理。

Material/Site soft delete 新增 guard：active Task Reservation、reserved/running/uncertain Claim、未完成
reconciliation 或未 settle receipt 任一存在时返回 `409 material_in_use`。不得清 occupancy、relation 或
fence 来使删除成功。

ResourceTreeSet、MaterialGraph、D1A claim status 和 FE DTO 都是 read projection。Inventory commit 后才
targeted refresh；refresh 失败标 projection stale并阻止 dispatch，从 durable truth rebuild，不反写 DB。
本轮保持冻结 FE Task/Job/Material routes，不采纳 latest Backend 已删除 Task input/output 等无关漂移。

## 12. 逐文件实施范围

| 文件 | M1EF change | 不允许 |
|---|---|---|
| `inventory/domain.py` | Claim/ChangeSet records、closed states/commands/results/errors | 第二 Material identity、driver type |
| `inventory/store.py` | exact v5→v6 migration、Claim/ChangeSet schema、transaction helpers | workflow.db connection、transaction callback |
| `inventory/service.py` | acquire/state/commit/release/resolve/recovery deep methods | DAG walk、ROS/driver/network |
| `inventory/commands.py` | versioned command decode/dispatch/result replay | unversioned dict guessing |
| `inventory/sync.py` | Claim/receipt outbox replay/ack/read projection | cache authority |
| `inventory/api.py` | 只补必要 read projection；默认无 operator mutation route | 直接 Store/SQL |
| `inventory/material_projection.py` | device_id→device Material fail-closed projection/rebuild | 用 device name 作为 Claim identity |
| `app/scheduler/service.py` | per-Task/Job saga serialization、claim guard、C1～C7 replay；legacy resource lock 只保留未扩展 hint | 第二 DAG engine、best-effort cleanup、把 legacy lock 当 Claim |
| `workflow/device_action_task.py` | D1A formal intent/result adapter 与 workflow projection callback | 直接调用 Inventory mutation、独立 physical claim authority |
| `workflow/runtime.py` | 只写 workflow unknown/cleanup facts；transaction 后通知 Scheduler reconcile | 在 workflow tx 内调用 Inventory |
| `workflow/store.py` | D1A inventory claim logical projection字段及 migration | inventory FK/SQL |

`dag_state.py` 保持唯一 DAG engine；`unilabos.workflow` 不获得 Inventory business rules；外部 Go
Scheduler、Registry、PackageCatalog、TemplateCatalog 与 FE production 不改。

## 13. RED、测试与 exact-SHA gate

本 spec commit 后，production code 前由恰好一个独立 test-author 提交 tests-only RED；随后
implementation owner 实现；最后恰好一个未参与实现/test-author 的 reviewer 对同一 exact tested SHA
同时做 Standards/Spec review。

### 13.1 Schema/migration

- empty v6 exact schema、constraints、partial unique indexes、fence monotonicity；
- exact v5 含真实 M1R Material/Site/Reservation/lot 数据升级后 byte-semantic 保留；
- crash before/during/after migration；mixed/unknown schema fail closed；
- 无 `material.db`、`resources.authority`、dual-read/write 或 cross-DB FK。

### 13.2 Claim concurrency

- device-only D1A、business Material、composition ancestor/descendant 和 Site contention；
- complete set all-or-none、稳定 order、并发 clients 无 deadlock；
- Task Reservation coverage、device/Site 不扩大 Reservation；
- duplicate replay、same owner/different set、attempt N/N+1、token monotonic/stale rejection；
- blocked Job 保持 pending且 zero partial rows/events。

### 13.3 Dispatch/cancel/D1A

- D1A 与普通 Workflow争用同一 device Material只能一个 Claim成功；
- workflow Job dispatched 先于 driver call；pre-send proof release 与 ambiguous exception uncertain；
- running、cancel ACK、cancel complete、timeout、transport unknown、late result；
- manual unlock 不能绕过 Inventory Claim；
- current D1A claimed/unknown upgrade recovery fail closed。

### 13.4 ChangeSet

- Material data/disposition/composition、Site occupancy、create/soft-delete invariant；
- same effect/fingerprint replay、different fingerprint conflict、no-op receipt；
- undeclared member、wrong expected version、stale attempt/token、cycle、allowlist、duplicate occupant；
- confirmed reality + invalid scalar output 仍提交 reality并使 Job failed；
- persistence failure不发布 output、不 terminal、不 release Claim。

### 13.5 Crash/recovery/reconciliation

- C1～C7 每个窗口 deterministic fault injection + close/reopen + second restart zero-write；
- active/uncertain Claim不因 restart/TTL/cancel自动释放；
- receipt→Job terminal replay、terminal-without-receipt quarantine；
- device/human resolution same payload replay、stale operator command、unresolved保持 fence；
- Task Reservation 只在全部 Claim settle 后释放，且不误释放其他 Task/attempt。

### 13.6 Full gate

- M1R/M1A～M1D、M2A、D1A、device manual unlock、R1B runtime targeted regression；
- `tests/workflow`、`tests/app`、完整 `pytest -q -rs tests`；
- changed-files Ruff E/F/I + format、changed production compile/import；
- `git diff --check`、exact candidate clean；
- Core #156 真实 OS process、真实 inventory.db、独立 concurrent clients、restart/E2E evidence。

## 14. 历史决策冲突与 supersession

本 spec 明确调整以下历史内容；未列出的 M1R/Backend field/ResourceSlot/Reservation 决策保持：

1. supersede 旧 M1 spec 的 `unilabos.resources.authority.MaterialModule` 与 shared workflow/material
   UoW；最终 seam 保持 M1R `unilabos.app.scheduler.inventory.InventoryService + inventory.db`；
2. supersede 旧表名 `material_execution_claim*`，使用 OS #15 已冻结的 `material_claim*`；
3. refine D-098/Core #155 的 `active/fenced/released` 命名为 latest Backend
   `reserved/running/uncertain/released`；所有 live/uncertain state 仍 fence-holding，安全语义不降低；
4. supersede “ChangeSet + Job terminal + Claim release 同一 SQLite UoW”，改为 inventory atomic receipt
   + workflow terminal projection + claim release durable saga；
5. refine D1A `device_action_task.claim_status` 为 projection；direct device action 与 Workflow action
   共用 Inventory Claim authority；
6. refine M1R terminal release：active/uncertain Claim存在时 release result `blocked`，不能提前释放
   Task Reservation；
7. 保留 Site allowlist association table；Backend latest JSON 列只作为 public projection reference；
8. 不采纳 latest Backend 对 frozen FE Task input/output/route 的无关漂移。

Core #155/#156 和 OS #15 的 current block 必须在 spec publication 时同步这些 supersession；旧正文
保留 provenance，但不能继续被 implementation 当作 current 单库或旧 state/table 合同。

## 15. 完成与后续依赖

本 spec publication 完成 M1EF to-spec gate，不授权 production implementation 或 merge。下一步严格是：

1. 唯一独立 test-author 从 published spec SHA 创建 tests-only RED；
2. implementation owner 在同一 delivery branch完成 atomic implementation；
3. full tests + exact-SHA reviewer + Core E2E；
4. 用户明确授权后才 merge/release。

M2B 可以继续完成 selector→binding 与 Task admission，不必等待 M1EF production；但任何真实
physical Action dispatch/result acceptance 必须同时通过 M1EF Claim/fence/ChangeSet/recovery gate。
R2 只有在 A1/I1/C1/M1R/M1EF/M2B 等依赖满足后才可解除 Material safety hard gate。
