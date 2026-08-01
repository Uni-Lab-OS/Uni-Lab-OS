# M1：Material/Site authority foundation 实现设计

日期：2026-08-01

实现分支：`migration/m1-material-authority-foundation`

基线：`integration/workflow-task-runtime@91b00dd030483058a6d0aafc42f143de829cc1bc`

控制面：[Core #155](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/155)

跨仓验收门：[Core #156](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/156)

迁移治理裁决：[Core #158](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/158)

## 1. 结果、权威与当前 stage gate

M1 把现有 Inventory transaction engine 深化为 OS-local 唯一 Material Module。该
Module 持有 Material/Site durable truth，并提供 02H `ResourceSlotResolver` 的 production
adapter、Task Material Reservation、带 fencing 的 Job Execution Claim、幂等 ChangeSet
repository primitive，以及 restart/reconciliation 所需的持久事实。

本设计冻结以下总原则：

- 接收 `POST /api/v1/workflow-tasks` 的 OS 同时是该 Task 的 Workflow、Task 与 Material
  Authority；请求不得选择第二个 Material authority，也不得回退 Backend 或远程查找；
- Material/Site、WorkflowTask/WorkflowNodeJob、Reservation/Claim 与对应 ledger/outbox
  位于同一个 runtime-authority SQLite database，并由同一 connection、同一 transaction
  coordinator 和同一 Unit of Work 提交；
- 表可以由不同领域 Module 拥有。共享 database/UoW 不表示 `WorkflowStore` 获得解释
  Material 业务规则的权力；所有 Material/Site invariant 只能由 Material Module 执行；
- `ResourceTreeSet`/PLR、Frontend DTO、Scheduler cache 和 device adapter 都是受控投影或
  消费者，不是持久真值；
- sole runtime coordinator 以后可调用 Material Module 的显式 port，但 legacy
  `EdgeScheduler` 永远不能成为 Material、Reservation、Claim、fencing 或 admission 的
  权威。

### 1.1 production RED 暂停条件

本设计文档可以继续审阅和发布，但 production RED 与 production implementation 当前
暂停。原因是 [Core #158](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/158) 正在裁决：
[Core #104](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/104) 要求每个
implementation round 至少两个 test-author、至少三个 reviewer，而本仓 `AGENTS.md` 的
Workflow Migration Round Gate 要求恰好一个 test-author、恰好一个 reviewer，且 agent
不得并发。两条当前都标记为强制规则，不能由实现者自行挑选或折中。

在治理权威明确消解该人数冲突前：

- 不分派 production RED test-author；
- 不修改 production code 或 production migration；
- 不声称 M1 已从 `stage:protocol-definition` 进入 implementation；
- 不用手写本地测试替代独立 RED provenance。

冲突消解后，按被明确确认的唯一 gate 执行，不从本文件反向推断 agent 人数。

### 1.2 Feishu 协议冲突

Feishu OKF《01.1 工作流协议》revision 9（wiki node
`Qa1EwFWB1iqx4OkfNXhcvTh3nPf`）仍把 PLR Site label 写成跨端身份键；这与 Core #155 及
OS `91b00dd030483058a6d0aafc42f143de829cc1bc` 上下文采用 stable Site UUID 的合同冲突。
本 spec 不删除或静默改写该历史段落：M1 Candidate 只按 stable Site UUID 设计，Decision
保持 `stage:protocol-definition`；在 M1 Accepted 前必须把正式 Feishu Protocol 更新为 UUID
identity，并保留对 revision 9 的 supersession 说明。FE、driver 与 migration 在此之前都不得
把 label/name 当持久 identity。

## 2. 范围与停止线

### 2.1 M1 必须交付

1. 把现有 Inventory 持久化能力迁入唯一 runtime-authority SQLite UoW，并停用 production
   中第二个 `InventoryStore` connection/database；
2. 持久化 Backend-shaped Material identity、composition、Disposition、barcode、version
   与 soft delete；
3. 将 Site 作为独立 aggregate 持久化，明确分离 composition 与 placement；
4. 用 durable Material adapter 替换 02H 的
   `UnconfiguredResourceSlotResolver`，保持现有 Task input caller 与 HTTP DTO 不变；
5. 在 Task create UoW 内尝试 all-or-none Task Material Reservation；
6. 提供由 `job_uuid + attempt` 拥有、complete-set、durable monotonic fencing 的 Job
   Execution Claim；
7. 提供 deterministic/idempotent ChangeSet repository primitive，并允许 D1 以后在同一
   UoW 中原子提交 Material/Site facts 与 Job terminal state；
8. 在 authority ready 前完成 migration verification、Reservation/Claim recovery、fence
   reconciliation 与 projection rebuild；
9. 提供 Backend-shaped Material/Site/Reservation/Claim read projection，并通过既有全局
   SSE outbox 发送失效通知，客户端再经 REST rehydrate；
10. 证明 `400 invalid_input`、`404 not_found`、`409 conflict` 的稳定边界，以及 contention
    不被误报为 input error。

### 2.2 明确不做

M1 不实现、也不创建空表、空 DTO、feature flag 或 placeholder 预占以下 M2 能力：

- Core #140～#146 的 MaterialSource node、mode、flow role 与 resolution lifecycle；
- `existing` 自动选择、`create_new`、CandidateSiteSet、lot deduction、warehouse/mount
  selector；
- 传感器 occupancy、freshness、debounce、cold-start 与 Backend pre-allocation handoff；
- per-Material progress、MaterialSource/Site status read model、`apply_deduct_resource` 或转运
  child Action。

本轮同样不实现 R2 DAG readiness/ExecutionPlan/admission loop，不执行 D1 driver dispatch
或 Action result wiring，不生成 O1 WorkflowTask output。A1/R2 后续提供 typed claim-set
输入，D1 后续把真实 Action result 接到本轮 ChangeSet primitive；M1 不用字段名、参数
顺序、Action 名称、barcode、PLR object identity 或 Python runtime value 猜这些信息。

## 3. 统一语言与不变量

| 术语 | M1 中的精确定义 |
|---|---|
| Material | 具有稳定 `uuid`、`resource_template_uuid`、composition、业务状态与乐观 version 的具体实体。设备和业务物料共享 identity 模型，但不共享业务 Disposition。 |
| Site | 某个 Material 拥有的稳定位置。`Site.material_uuid` 是 owner，`Site.occupied_material_uuid` 是当前 occupant。 |
| Warehouse | M1 中不是第二种 authority 或独立选择引擎；它是拥有稳定 Material identity 与一组稳定 Site 的普通 aggregate。自动选择、mount 特例与库位策略属于 M2。 |
| Disposition | 业务 Material 的持久可运行状态：`active/consumed/discarded/quarantined/reconciling`。`reserved` 与 `in_use` 不属于 Disposition。 |
| ResourceSlot | Workflow/Action 边界上的 typed reference；外部值只含 `{uuid}`，不是 Material body、selector 或 allocation request。 |
| Reservation | `workflow_task_uuid` 拥有的 Task-lifetime、all-or-none 业务 Material 承诺。它不等于 device lock，也不因 Material 当前 placement 自动锁 Site。 |
| Claim | `job_uuid + attempt` 拥有的短期物理执行权；覆盖 selected device Material、可变业务 Material 与 occupancy 可变 Site。 |
| fencing token | 每次完整 Claim acquisition 产生的 durable monotonic token。旧 attempt/token 即使延迟回调也不能提交现实状态。 |
| ChangeSet | 从受 Claim 保护的执行前 baseline 与执行后 projection 推导出的、按稳定 UUID 排序的 Material/Site 增量事实。 |
| projection | 从 durable truth 构建的 `ResourceTreeSet`、PLR 或 frontend read model；可丢弃重建，无独立写入权。 |

必须始终成立：

1. `Material.parent_uuid` 只表达 composition；Site occupancy 只由
   `Site.occupied_material_uuid` 表达。移动 occupant 不修改 composition，修改 composition
   不隐式移动 occupant；
2. 一个 Site 最多一个 occupant；一个 Material 最多占据一个 Site；owner 与 occupant
   必须存在、未删除，且不能 self-occupy；
3. business Material 只有 `active` ordinarily runnable。Reservation/Claim 从各自表派生，
   不回写成 Material status；
4. device Material 的 online/health 仍属于 DeviceState；M1 只用同一 Material UUID 建立
   executor Claim，不给 device 写 warehouse/consumed 等业务 Disposition；
5. soft-deleted entity 不参与正常读、resolve、reserve 或 claim。受 live relationship、
   Reservation、Claim 或 uncertainty fence 保护的 entity 不得删除；
6. 非空 barcode 在非删除 Material 中按大小写不敏感唯一。SQLite 内部只存一个
   `barcode` 值，公共 DTO 一对一投影为 Backend `code`，禁止 `code`/`barcode` 双列；
7. 任一并发 primitive 都完整成功或 zero-write；transaction/process mutex 不是
   Reservation/Claim 的替代品；
8. SQLite transaction 不跨物理 Action，不调用 driver、ROS、网络或 PLR refresh。

## 4. 深 Module 与 one-UoW 架构

现有 `unilabos.app.scheduler.inventory` 是迁移来源和要深化的 transaction engine，而不是
需要并存的 legacy authority。实现允许在该 package 内重构或移动代码，但 production
composition 最终只能装配一个概念上的 `MaterialModule`：

```python
class MaterialModule:
    def resolve_resource_slot(...) -> ResolvedResourceSlot: ...
    def reserve_task_materials(uow, *, task_uuid, root_material_uuids) -> ReservationOutcome: ...
    def acquire_job_claim(uow, *, job_uuid, attempt, device_material_uuid,
                          material_root_uuids, site_uuids) -> ExecutionClaim: ...
    def fence_job_claim(uow, *, job_uuid, attempt, fencing_token, reason) -> ExecutionClaim: ...
    def release_job_claim(uow, *, job_uuid, attempt, fencing_token) -> ExecutionClaim: ...
    def commit_change_set(uow, *, change_set, fencing_token) -> ChangeSetReceipt: ...
    def reconcile_startup(uow) -> RecoveryPlan: ...
```

名称可因代码布局微调，语义不可拆散。边界规则：

- transaction coordinator 拥有 SQLite connection、进程内写锁、`BEGIN IMMEDIATE`、commit
  和 rollback；
- Material repository 接受已经打开的 UoW/connection，不创建 connection、不 acquire
  第二个 store lock、不自行 commit；
- Workflow repository 与 Material repository 可在同一 transaction 中写各自拥有的表；
- HTTP、`WorkflowService`、worker、Scheduler、driver 和 projection builder 不能执行
  Material SQL；
- production 不再由 `ULAB_INVENTORY_DB` 装配一个独立 live writer。旧数据库只允许在
  maintenance migration 中 read-only 打开，迁移完成后必须关闭；
- tests 可使用临时 runtime-authority SQLite，但不得以 mock 两个 store 的补偿流程作为
  原子性证据。

建议从现有 `WorkflowStore` 抽取可跨领域使用的 `RuntimeAuthorityUnitOfWork`，或在不泄露
Material SQL 的前提下扩展其 transaction coordinator。禁止让 `MaterialModule` 变成
`WorkflowStore` 的 CRUD helper；共享的是 transaction capability，不是 domain ownership。

## 5. Durable schema 与约束

最终表名使用单数 snake_case。下面是 M1 的最小持久合同；实现可增加不改变语义的索引、
时间戳或内部审计列，但不能增加平行 identity/status。

### 5.1 `material`

| column | contract |
|---|---|
| `uuid` | primary key；唯一 canonical Material identity |
| `resource_template_uuid` | required；authority-owned ResourceTemplate identity |
| `parent_uuid` | nullable FK `material.uuid`；只表达 composition |
| `barcode` | required non-blank；公共 DTO 投影为 `code` |
| `disposition` | business Material 为 closed `active/consumed/discarded/quarantined/reconciling`；device Material 不使用该业务状态机 |
| `material_kind` | closed `business/device`，只隔离业务 invariant，不改变 UUID 类型 |
| `version` | positive integer；只在实际业务事实变化时递增 |
| `deleted_at` | nullable UTC timestamp；只允许 soft delete |
| `create_time/update_time` | required UTC timestamp |

Material 的已确认内容、配置与业务字段继续由 Material Module 在同一 aggregate 内持有；
存储可用 closed JSON 或规范化子表，但不得藏入第二个 `ResourceTreeSet` snapshot 充当真值。
composition 必须无环；一个 child 最多一个 `parent_uuid`。

### 5.2 `site` 与 allowlist

| column | contract |
|---|---|
| `uuid` | primary key；stable Site identity |
| `material_uuid` | required owner FK `material.uuid` |
| `name` | required；同一未删除 owner 下大小写规范化后唯一 |
| `sort_order` | required integer；只用于稳定展示/PLR 顺序，不是 identity |
| `occupied_material_uuid` | nullable FK `material.uuid`；active occupant 全局唯一 |
| `geometry` | required valid Backend-shaped geometry JSON |
| `version` | positive integer；只在实际变化时递增 |
| `deleted_at` | nullable UTC timestamp |
| `create_time/update_time` | required UTC timestamp |

`site_allowed_resource_template` 以 `(site_uuid, resource_template_uuid)` 为主键保存 allowlist；
不要在 runtime 解析逗号字符串。空集合和未约束必须沿用冻结 Backend Site 合同的唯一语义，
不能由 FE 或 driver 自行解释。occupancy write 在同一 transaction 验证 owner、occupant、
template allowlist、soft delete、self-occupancy、occupant 唯一性与 expected Site version。

`lab_zone`/`lab_placement` 继续是独立 2D layout model，不是 Site，不拥有 occupancy，也不
产生 Site Claim。

### 5.3 Task Reservation

`material_reservation`：

| column | contract |
|---|---|
| `uuid` | primary key |
| `workflow_task_uuid` | required FK，active history 中一个 Task 只有一份 canonical Reservation |
| `set_fingerprint` | sorted complete member set 的 deterministic fingerprint |
| `status` | closed `active/released` |
| `create_time/released_at` | durable lifecycle |

`material_reservation_member`：

| column | contract |
|---|---|
| `reservation_uuid` | FK |
| `material_uuid` | business Material FK |
| `root_material_uuid` | 产生该 member 的 explicit ResourceSlot root；只作 provenance |
| `acquired_version` | reservation 时观察到的 Material version |
| `released_at` | nullable；active member 为 null |

主键是 `(reservation_uuid, material_uuid)`；同一 `material_uuid` 最多存在一个 active
member。对显式 concrete ResourceSlot root，Material Module 在 transaction 内展开当前
composition subtree 并去重，从而使 ancestor 与 descendant reservation 发生冲突。不得按
JSON 字段名扫描任意 UUID，也不得自动选择同模板的另一个 Material。

### 5.4 Job Claim 与 durable fence

`material_execution_claim`：

| column | contract |
|---|---|
| `uuid` | primary key |
| `workflow_node_job_uuid` | required FK |
| `attempt` | required positive integer；必须等于 Job attempt |
| `set_fingerprint` | complete sorted member set fingerprint |
| `fencing_token` | required durable monotonic integer，unique |
| `status` | closed `active/fenced/released` |
| `reason` | fenced/release 的 stable machine reason |
| `create_time/fenced_at/released_at` | durable lifecycle |

`material_execution_claim_member` 的主键为
`(claim_uuid, resource_kind, resource_uuid)`；`resource_kind` 只允许：

1. `device_material`；
2. `business_material`；
3. `site`。

该顺序同时是 `(kind, uuid)` 的稳定 lock/validation/write order。member 保存 acquisition
时的 expected version。Material roots 必须在 transaction 内展开最新 composition subtree；
Site set 必须加入当前 occupant 及 typed caller 明确提供的相关 business Material。Claim
request 的 roots/Sites/device 来自 A1/R2 typed contract，不从 runtime 参数 shape 猜测。

`material_fencing_token(sequence INTEGER PRIMARY KEY AUTOINCREMENT, claim_uuid UNIQUE)` 产生
永不复用的 token；`material_resource_fence(resource_kind, resource_uuid, fencing_token,
claim_uuid)` 保存每个 resource 的最新 fence。新 Claim 只有在每个 member 都没有别的
fence-holding `active/fenced` Claim 时才能一次性写入。release 后保留最新 token 记录，因此
旧 callback 在资源尚未重新分配时也必须再通过 Claim lifecycle 检查，在重新分配后则同时
因 token mismatch 被拒绝。

同一 `(job_uuid, attempt)` replay：

- complete set 相同：返回原 Claim 与原 token；
- complete set 不同：`409 conflict`，不得扩大、缩小或换资源；
- attempt 变化：必须取得新 Claim 与更大的 token；旧 token 永久 stale。

### 5.5 ChangeSet receipt、ledger 与 outbox

`material_change_set_receipt` 至少保存：

- `workflow_node_job_uuid`、`attempt`；
- `deterministic_fingerprint`；
- `fencing_token`；
- canonical committed result/versions；
- `create_time`。

`(job_uuid, attempt)` 唯一：同 owner、同 fingerprint replay 原 receipt；同 owner、不同
fingerprint 返回 `409 conflict`。Material/Site audit ledger append-only。Frontend invalidation
复用现有 `frontend_event` 全局 SSE outbox；不得创建 Task-scoped event stream、Runtime
WebSocket、进程内-only material event authority 或 timer polling fallback。

## 6. ResourceSlot resolution 与 HTTP 错误边界

02H 已冻结外部 ResourceSlot 为 closed `{"uuid":"<material-uuid>"}`，并在 Task/Job
零写入前完成 shape validation。本轮 production adapter 只实现该 port：

```text
ResourceSlot {uuid}
  -> local Material lookup
  -> not deleted
  -> material_kind == business
  -> disposition runnable
  -> no durable uncertainty fence
  -> template allowlist check
  -> {uuid, resource_template_uuid}
```

resolver 不创建 Reservation、Claim、Task 或 Job，不刷新 projection，也不读取
`ResourceTreeSet`。它永远只返回 authority-owned template UUID；caller 不能提交 template
来覆盖真值。

错误映射固定如下：

| 条件 | HTTP / machine code |
|---|---|
| closed shape、UUID/type 非法，Material 不是 business kind，template mismatch，adapter 返回非法 identity | `400 invalid_input` |
| Material 不存在或 soft-deleted | `404 not_found` |
| `consumed/discarded/quarantined/reconciling`、live uncertainty fence 或其他稳定 non-runnable fact | `409 conflict` |

其他 Task 的 active Reservation 是 transient contention，resolver 阶段不得返回 409。它在
随后的 all-or-none reservation attempt 中决定 Task 是否等待。内部 SQLite exception、表名、
Material body、输入值、barcode 和 adapter exception text 不进入公共错误 body。

## 7. Task create + Reservation 原子时序

M1 扩展现有 Task creation pipeline，但不改变 `POST /api/v1/workflow-tasks` request DTO：

```text
Authority/process lease
  -> command serialization
  -> BEGIN IMMEDIATE on runtime-authority SQLite
  -> read exact Workflow Graph/Input Contract
  -> 02H input/ResourceSlot/Handle preflight
  -> derive explicit concrete ResourceSlot roots from typed schema only
  -> expand composition and attempt complete Reservation under SAVEPOINT
     -> success: retain one header + every member
     -> reservation contention: rollback SAVEPOINT to zero Reservation row/member
  -> INSERT WorkflowTask/WorkflowNodeJob snapshot facts
  -> append ledger + one existing global SSE invalidation as required
  -> COMMIT
```

实现可以先 INSERT Task 再 INSERT Reservation 以满足 FK，但两者必须仍在同一 outer
transaction；contention 只回滚 reservation savepoint，不能回滚已验证 Task intent。结果：

- 无争用：HTTP `201`，Task 为 `pending`，Task 与 complete active Reservation crash-atomic；
- 有其他 Task Reservation 争用：HTTP 仍为 `201`，Task 为 `pending`，该 Task 的
  Reservation header/member 均为零，sole coordinator 以后用相同 typed roots 重试；
- stable invalid input/not-found/conflict：返回 400/404/409，Task、Job、Reservation、ledger、
  outbox 全部 zero-write；
- 同一 Task/client request identity replay 返回既有 Task 与既有 Reservation outcome，不生成
  第二份 Reservation；
- 无 complete Reservation 的 Task 不得 dispatch 任何 Job。

Reservation contention 的诊断只能是 Task admission projection/journal 中的 stable reason；
它不是第二份 resource ownership row。不要创建 partial Reservation 来表达“已拿到其中
几个”，也不要用 Material `disposition=reserved` 表达等待。

Reservation release 只能在 Task terminal 且所有 dispatched/unknown Job Claim/fence 已
settled 后发生，并与对应 runtime cleanup mutation 同事务提交。wall-clock TTL、进程退出或
前端离线都不能自动释放 Reservation。

## 8. Claim acquisition、fencing 与 release

R2 以后调用 Claim port 的时点固定为：Node ready、concrete executor selected 之后，创建
runtime projection/dispatch 之前。M1 repository 的 acquisition algorithm 为：

1. 验证 Job 存在、attempt 匹配、所属 Task 有 complete active Reservation；
2. 验证 selected device Material 存在且由 DeviceState 判定可选，但不把 DeviceState 写成
   Material Disposition；
3. 从 A1/R2 typed roots 在 transaction 内展开全部 business Material composition descendants；
4. 对 typed Site UUID 验证 owner、occupancy policy、template allowlist 与 version，加入 Site、
   当前 occupant 和明确的目标 occupant；
5. deduplicate，按 `device_material -> business_material -> site`、再按 UUID 排序；
6. 验证没有其他 `active/fenced` Claim、expected version 未变化；
7. 分配一个新 monotonic fencing token，写 complete Claim/members/resource fences、ledger/outbox；
8. commit 后才刷新 projection；projection ready 后才允许 dispatch。

任一 conflict 保持 Job `pending`，Claim/fence/ledger/outbox zero-write。Claim conflict 是 late
admission wait，不把 Job 标为 failed，也不让 legacy Scheduler 选择另一资源。一次 selected
device v1 独占到 Claim 释放。

Claim lifecycle：

- `active`：已取得 complete set，允许匹配 token 的正常 ChangeSet；
- `fenced`：物理结果不确定、dispatch unknown、post-action persistence failure 或 cancellation
  未 settle；仍阻止其他 Claim，禁止自动 dispatch/replay；
- `released`：仅在 pre-dispatch 明确失败，或 physical result + ChangeSet + Job terminal 已
  crash-atomically settle 后进入。

Claim 没有安全的时间到期自动释放。cancellation 只记录 intent；只要 Action 可能已经执行，
Claim 就保持 active/fenced。stale `job_uuid/attempt/token` 在所有 commit/reconcile seam 返回
409 且 zero-write。

## 9. 固定 transaction / lock order

所有 Task+Reservation、Claim acquisition、ChangeSet+Job terminal、release 与 recovery
transaction 严格使用：

1. Authority/process lease；
2. sole coordinator command serialization；
3. runtime-authority SQLite `BEGIN IMMEDIATE`；
4. 按稳定 `(kind, uuid)` 读取、验证和写 aggregate；
5. append-only ledger 与 existing frontend outbox；
6. COMMIT；
7. projection/driver lock 与 targeted refresh。

禁止：

- 嵌套取得独立 `WorkflowStore._lock` 与旧 `InventoryStore._lock`；
- 持有 projection/driver lock 后反向进入 Store；
- SQLite transaction 内调用 ROS、driver、HTTP、SSE publish、sleep 或 PLR refresh；
- 跨物理 Action 保持 transaction；
- 用 retry + compensating write 冒充跨 database 原子性；
- 依赖 SQLite 当前单写者特性而省略 stable resource order、durable unique constraint 或
  fencing verification。

commit 后 outbox publisher 与 projection refresher 才可运行。publish/refresh 失败不能回滚
已提交真值；分别通过 durable outbox replay 与从 authority rebuild 恢复。

## 10. deterministic/idempotent ChangeSet primitive

M1 只建立 repository primitive；A1/D1 以后负责从真实 Action contract/result 与 Mutation
Session 生成并接线。ChangeSet 输入至少包含：

- `job_uuid`、`attempt`、`fencing_token`；
- 每个现有 Material/Site 的 expected version；
- created Materials；
- existing Material 字段/content 的实际变化；
- composition attach/detach/reparent；
- explicit soft delete；
- Site occupancy 的实际变化。

生成方必须按稳定 UUID canonicalize；同 UUID conflicting projections、composition cycle、
undeclared Site mutation、未 Claim entity 或把缺失节点猜成删除都必须拒绝。soft delete 只能
来自 explicit operation，不能由 projection 中“没看见”推断。

fingerprint 为 canonical ChangeSet JSON 的 SHA-256；canonical input 排除 observation time、
trace text 等非业务噪声，包含所有 operation、identity、expected version 与目标 canonical
value。repository 在一个 UoW 中：

1. 查询 `(job_uuid, attempt)` receipt；同 fingerprint replay 原结果，不再写 aggregate；不同
   fingerprint 409；
2. 验证 Claim 仍 fence-holding、owner/attempt/token 完全匹配，且每个 affected entity 都是
   Claim member；
3. 验证 Material/Site exists、未删除、expected version、composition/occupancy/allowlist 与
   domain invariant；
4. 只 UPDATE 实际改变的 row/column，只给实际改变的 aggregate bump version；
5. 写 receipt、每个实际变化的 ledger 与一个事务级 frontend invalidation；
6. 由 D1 caller 在同一 UoW 写 canonical `WorkflowNodeJob.return_info`、Job terminal 与 Claim
   release，然后一起 COMMIT。

物理动作已经发生但 scalar/output validation 失败时，已确认的 Material/Site reality 仍可在
该 primitive 下提交，随后 Job 失败；现实不能被“业务 output invalid”回滚。无法确认 reality
或持久提交失败时，不发布 stale output、不标 Job succeeded、不释放 Claim，而是 fence 并
进入 reconciliation。

语义 no-op 仍写幂等 receipt，但不 bump Material/Site version，不写虚假 aggregate ledger，
不单独产生 Material SSE。相同 Material 在多个 input/output/list 出现时持久化一次；业务
output 自身仍保留字段顺序和重复引用。

## 11. ResourceTreeSet / PLR projection

`ResourceTreeSet` 只在以下受控边界使用：

1. authority startup/recovery 完成后，从 durable Material/Site facts 构建；
2. Job Claim commit 后，为 claimed roots 构建 immutable baseline 与 job-owned Mutation Session；
3. ChangeSet commit 后，在 SQLite transaction 外按 committed versions targeted refresh；
4. projection stale 时，从 durable facts rebuild affected roots 或 full projection。

禁止 background bidirectional sync、polling compare、last-write-wins 或“进程启动时用 graph
覆盖数据库”。graph 只能在空 authority 上通过显式、一次性的 import/migration 建库。

若 durable commit 成功但 projection refresh 失败：

- durable Material/Site 仍是唯一 truth；
- affected projection 标记 stale/unavailable；
- 阻止需要该 projection 的 Claim/dispatch；
- 从数据库重建，绝不把旧 PLR tree 写回作补偿。

driver 可在取得 Claim 后把 stable Site UUID 解析为 owner-relative PLR site name/spot；该
transport value 不进入 Workflow/Material persistence identity。

## 12. Startup recovery 与 reconciliation

production composition 取得 authority/process lease 后、对外 ready 和 worker/dispatch 启动
前，按以下顺序运行：

```text
open one runtime-authority database
  -> schema migration / legacy import verification
  -> R1B in-flight Job recovery
  -> Material Reservation/Claim/fence audit
  -> persist required fenced/reconciling transitions in one UoW
  -> rebuild ResourceTreeSet projection from durable truth
  -> publish ready
```

恢复规则：

- Reservation 与 Claim 不因 restart 自动释放；
- pending Task + complete Reservation 保持候选；pending Task + zero Reservation 进入 sole
  coordinator 的 retry set；M1 不自行实现 admission loop；
- 只有同时证明 Job 从未 dispatch、没有 transport acknowledgement、没有 physical uncertainty
  的 Claim 才可安全 release；
- `dispatched/running/intervention_required/cancel_requested/execution_unknown` 或 post-action
  persistence failure 的 Claim 转/保持 `fenced`，Task/Job 进入既有 R1B reconciliation
  contract，绝不自动重发；
- terminal Job 若存在匹配 ChangeSet receipt、Job result 与 released Claim，视为 settled；三者
  不一致时 fail closed 并产生 operator-visible reconciliation item；
- active/fenced Claim 的 resource fence 必须完整匹配 member set/token。缺行、多行、token
  mismatch 是 authority corruption，不得静默重建为“可用”；
- stale callback、旧 attempt 和旧 token 在 restart 前后行为一致：409、zero-write；
- rebuild projection 失败时 authority 不 ready for material execution，但 REST 可在明确 degraded
  状态下继续展示 durable facts。

reconciliation 是显式决定并提交 reality 的流程，不是 TTL cleanup。人工或 device query 得到
证据后，仍使用匹配 Claim token 和 ChangeSet primitive 提交；只有事实与 Job 状态 settle 后
才 release Claim，再在 Task cleanup 条件满足后 release Reservation。

## 13. Legacy Inventory migration 与 retirement

当前 `unilabos/app/scheduler/inventory` 具有可复用的 SQLite transaction、ledger/outbox、
乐观 version 与 all-or-none 操作经验，但现有 schema/接线不是 M1 合同：`edge_uuid` /
`legacy_cloud_id` 双 identity、`template_id`、overloaded instance status、node-scoped
`inventory_reservation`、`resource_relation` placement、独立 database/lock，以及
EdgeScheduler fail-open/in-memory lock 都必须迁移或退役。

一次性 migration 必须在没有旧 writer 的 maintenance/startup gate 中完成：

1. 取得 process lease，确认旧 Scheduler/Inventory writer 未运行；
2. read-only 打开 legacy Inventory DB，打开唯一 target runtime DB；
3. 在写 target 前完整生成并验证 identity/template mapping；
4. 在一个 target UoW 导入 Material/Site/允许保留的 ledger provenance；
5. 校验 row count、UUID、barcode、composition、Site occupancy、version 与 live reservation；
6. 写 immutable migration receipt（source fingerprint/schema version/mapping fingerprint）；
7. commit、关闭 legacy DB；production 后续只打开 target runtime DB。

映射规则：

- canonical Material identity 是 `uuid`。`edge_uuid` 与非空 `legacy_cloud_id` 不一致时必须使用
  显式、预审计 mapping artifact；禁止以“哪个像 UUID”、最后写入或远程 lookup 猜 identity；
- `template_id` 必须经显式 ResourceTemplate UUID mapping；
- `warehouse/bench` 可映射 business `active`；`consumed/discarded/quarantined` 映射同名
  Disposition；`in_use` 进入 `reconciling`；legacy `reserved` 只有在能证明完整 Task owner 时
  才导入新 Reservation，否则 migration fail closed，不能静默释放；
- `material_instance.parent_uuid` 独立迁为 composition；
- `resource_relation` 的非空 `slot_id` 解析或创建 owner 的具名 Site 并迁为 occupancy；空
  `slot_id` 不再创建 placement truth。迁移后 `resource_relation` read-only/retired；
- old node-scoped reservation 不直接改名成 Task Reservation。无法映射到真实
  `workflow_task_uuid` 和 complete member set 的 live row 必须阻断 migration；
- lot/quantity/warehouse selector tables 留待 M2 决策，不接入 M1 runtime，不新增 facade；
- `lab_zone/lab_placement` 可原样保留为 layout，但不能被解释为 Site。

迁移后 production 必须停用：

- old Inventory API/command 对 legacy tables 的写入口；
- `EdgeScheduler.reserve_workflow`、`consume_reservation`、fail-open warning；
- `_job_resource_locks` 与 `@action(lock_resource=...)` value guessing；
- 旧 material monitor 作为 frontend authority；
- remote Backend/material fallback 与从 graph 动态生成 Material UUID。

若其他 legacy runtime 仍需要旧 Scheduler，它只能作为隔离的迁移兼容路径，不能处理本轮
WorkflowTask、不能写 M1 tables、不能被 production composition 同时装配为 authority。

## 14. Read projection、SSE 与 FE boundary

OS 提供 closed、Backend-shaped read DTO：

- Material：`uuid/resource_template_uuid/parent_uuid/code/disposition/version/deleted_at` 及已冻结
  Backend business fields；
- Site：stable UUID、owner、name/order、template allowlist、occupant、geometry、version；
- Reservation：Task owner、complete members、lifecycle；
- Claim：Job/attempt owner、complete typed members、lifecycle、fencing token 与 stable reason。

Reservation/Claim 对普通 FE 是 read projection，不是资源分配 command。FE 只提交
ResourceSlot `{uuid}`，不提交 `resource_template_uuid`、Reservation、Claim、fencing token、
expected version 或 resource selection decision。

每个前端可见的 committed Material transaction 在既有 `frontend_event` 中追加最小
invalidation；全局 `GET /api/v1/events` SSE 只通知“哪些 projection 已失效”，FE 再读取
REST。事件不是 patch、不是 truth，也不建立 material-scoped WebSocket。具体 event 名称与
closed payload 必须在 FE/OS integration DTO gate 一起冻结；在此之前 repository 只能复用
既有 outbox，不得另造传输。

FE 展示：

- `400` 为字段/codec/template 不匹配；
- `404` 为引用不存在或已删除；
- `409` 为稳定不可运行或 fence conflict；
- Task `pending` 且没有 Reservation 的 contention/waiting 为 admission 状态，不改写为 409；
- Claim `fenced`/reconciling 是 operator-visible unresolved state，不展示成“空闲”或自动
  “已释放”。

## 15. RED 计划与验收矩阵（待治理冲突解除）

冲突解除后的独立 RED 必须通过 public Module/WorkflowService/真实 HTTP seam 与真实 SQLite
编写。直接查 SQLite 只用于 schema constraint、atomicity、fencing、ledger/outbox、crash 和
reopen 证据；不能 mock repository transaction 来证明原子性。

最低覆盖：

1. Material/Site create/read/update/soft-delete、barcode case-insensitive unique、composition
   cycle、occupancy/allowlist/version；
2. ResourceSlot closed `{uuid}`、authority-owned template、business/device distinction 与
   exact 400/404/409；
3. Task create + Reservation success 的同事务 fault injection；
4. 多个并发 client 争用同一 Material：恰好一份 complete Reservation，其余 Task 全部
   `pending` 且各自零 Reservation；
5. 多 root/descendant contention、完整集合中间失败、same Task replay 与无 partial member；
6. Job Claim complete-set、kind/UUID stable order、same attempt replay/different set conflict、
   attempt 递增 token；
7. stale attempt/token、ancestor/descendant、Site/occupant/device conflict 均 zero-write；
8. cancel、dispatch unknown、post-action persistence failure 不提前 release Claim；
9. ChangeSet same fingerprint replay、different fingerprint conflict、version conflict、no-op 不
   bump version/SSE、Material+Site+Job terminal crash atomic；
10. 在每个 transaction phase crash/reopen：Reservation/Claim/fence/receipt/ledger/outbox 只出现
    before 或 after，不出现 partial；
11. restart recovery 不释放 live facts、不盲 dispatch，安全 pre-dispatch release 有完整证据，
    projection 只从 SQLite rebuild；
12. legacy migration identity ambiguity、reservation ambiguity、relation/Site mapping、receipt
    replay 与双 writer prevention；
13. 全局 SSE invalidation + REST rehydration，且无 Material WebSocket、轮询或 frontend
    allocation；
14. 静态守护证明 production WorkflowTask path 不 import/call old EdgeScheduler material
    reservation、`_job_resource_locks`、legacy Inventory writer 或 M2 #140～#146 symbol。

Core integration gate 还必须使用真实 OS process、同一 SQLite authority、多进程/多 client
contention、fault/crash injection 与 restart；只有单元 mock 不能进入 Accepted。

## 16. 完成定义

M1 只有在以下条件全部满足后才可声明实现完成：

- Core #155 仍是 active Decision，OS/FE implementation spec 与 Core contention/restart gate
  已发布并通过各自 stage gate；
- #104 与 OS `AGENTS.md` 的 agent-count 冲突有可引用的明确裁决；
- production 只有一个 runtime-authority database 与 transaction coordinator、一个 Material
  Module；需要跨域原子的写入共享同一 UoW/connection；
- ResourceSlot、Reservation、Claim/fencing、ChangeSet、restart 与 400/404/409 全部通过 RED、
  完整回归及 exact-SHA review；
- EdgeScheduler、Frontend、ResourceTreeSet 与 legacy Inventory tables 均未形成第二真值；
- M2 #140～#146 没有 production/DTO/placeholder 实现；
- tested SHA、commands/results、test-author/reviewer provenance、finding disposition 与迁移结果
  已写入 round ledger；
- 只在得到明确授权后 push/发布。
