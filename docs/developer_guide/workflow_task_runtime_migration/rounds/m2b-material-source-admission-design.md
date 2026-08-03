# M2B：Task-wide MaterialSource admission 与 resolution projection 设计

日期：2026-08-02

状态：**IMPLEMENTATION SPEC CANDIDATE / USER DECISIONS FROZEN**。本设计已冻结本轮
范围、Module Interface、Task 状态、分配顺序、错误分类、跨库时序和 RED 门禁；下一步是从
精确 integration 基线建立独立 tests-only RED。在 RED 进入 round branch 且本设计通过
exact-SHA Spec review 前，不修改 production。

实现分支：`migration/m2b-material-source-admission`

精确实现基线：
`Uni-Lab-OS/Uni-Lab-OS:integration/workflow-task-runtime@cf6f81da8bf41950c8779555c60a7b7349184fbe`

当前已发布工作区：
`Uni-Lab-OS/Uni-Lab-OS:dev@f660fd83dd8008c3947d75241a1e222f30ad7852`；该提交通过
`f4d9c1e4fb007ff26c8a867d0d2e1f43eafd404b` 包含上述 integration 基线。按仓库 round
规则，production branch 从最新 `integration/workflow-task-runtime` 开，不从发布 merge 或旧
M1R release merge 开。

交付票：[Uni-Lab-OS #9](https://github.com/Uni-Lab-OS/Uni-Lab-OS/issues/9)

父票：[Uni-Lab-OS #7](https://github.com/Uni-Lab-OS/Uni-Lab-OS/issues/7)

跨仓合同：

- [Uni-Lab-Core #140](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/140)
  MaterialSource 合同；
- [Uni-Lab-Core #141](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/141)
  Task-wide allocation/Reservation 原子边界；
- [Uni-Lab-Core #161](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/161)
  M1R 最终 Module 落位。

继承锚点：

- M2A integration：`8fad069c16faeb991fade5232eaf84ef32b17146`；
- M1R exact candidate：`a807582124247fc25e789005f51cf3d088cc537d`；
- M1R integration：`de95b9965ed551ed2ed1581fe2bd3100e1b93672`；
- D1A-aligned integration：`cf6f81da8bf41950c8779555c60a7b7349184fbe`。

## 1. 本轮冻结结果

M2B 把 M2A 已冻结的静态 `MaterialSource` selector 解析为具体 Material/Site binding，取得
Task Material Reservation，并把结果投影到 M2A 已预建的 coordinator-owned resolution Jobs。

本轮只保留一个外部 **Seam**：

```text
WorkflowTask immutable snapshot + resolution Jobs
                       │
                       ▼
unilabos.app.scheduler.service.EdgeScheduler
       │ one durable Task-wide command
       ▼
unilabos.app.scheduler.inventory.InventoryService.admit_task()
       │ one inventory.db transaction
       ├── select existing Material, or
       ├── create one Material on one Site
       ├── reserve the complete Task Material set
       └── persist result + ledger + outbox
                       │
                       ▼
EdgeScheduler projects result into workflow.db
       ├── Task status
       └── per-source resolution Job return_info/status
```

`InventoryService` 是深 **Module**：调用者只学习一个 `admit_task()` **Interface**，Site
排序、候选匹配、Material 创建、Reservation、processed-command、ledger/outbox 与 SQLite
Implementation 都留在该 Module 内。不得把候选查询、排序 helper、Store connection 或 per-source
allocator 暴露给 Scheduler、Workflow runtime 或测试。

## 2. 用户已确认的四项决策

以下决策是本设计的最终输入，旧 Issue/评论与之冲突时以本节为准：

1. **Inventory deduction 是 non-goal。** M2B 不读取、预留、扣减或消费
   `inventory_lot` 数量，不写 stock effect，也不把 FIFO lot policy 接到 MaterialSource。
2. **Material/Site 候选按 Site order。** 规范排序为
   `Site.sort_order ASC, Site.uuid ASC`；输入 CandidateSiteSet 的 UUID 顺序不是业务优先级。
3. **固定 Material 位置不匹配直接 reject。** 不进入等待，不由 coordinator 等待其被搬入，
   不借 MaterialSource 隐式移动物料。
4. **`admission_blocked` 是 WorkflowTask 状态。** 它不是只存在于 admission projection 的
   waiting reason；Task 列表、详情、SSE rehydrate、启动恢复和取消路径都必须识别该状态。

## 3. 继承且不重开的 M2A/M1R 合同

### 3.1 M2A 静态合同

保持 M2A 全部已接受行为：

- `MaterialSource` 是非 Action 持久节点；
- selector 是 closed object：`mode`、`resource_template_uuid`、`mount`、可选
  `material_uuid`、互斥 `site`/`slot_range`、必填 `flow_role`；
- 一个 typed `ResourceSlot` source Handle；
- Python/DAG round-trip、稳定 Node/Handle identity 与 material fan-out `<=1`；
- Preview/Save/Apply 只做静态 identity/template/mount/allowlist 证明，不把当时 occupancy、
  disposition 或 Reservation 当作 Task-time 事实；
- Task snapshot 原样冻结 canonical selector、Node/Handle identity、role 与 material edges。

M2B 不改 M2A schema、codec、authoring marker、Graph validator 或公开 selector 字段。

### 3.2 M1R Module 落位

保持以下唯一落位：

- `unilabos.app.scheduler.inventory.InventoryService + inventory.db` 是唯一持久 Material/Site/
  Reservation authority；
- `EdgeScheduler` 是唯一跨库 coordinator；
- `unilabos.workflow + workflow.db` 只拥有 Workflow/Task/Job/snapshot/projection；
- `ResourceTreeSet` 只是 execution projection；
- 无 `resources.authority`、`resources.inventory`、`MaterialModule`、`material.db`、shared
  WorkflowStore UoW、dual-read/dual-write 或长期 compatibility shim。

### 3.3 M1R 已有可复用接缝

直接深化而不另起平行 Module：

- `TaskMaterialAdmissionSource`；
- `TaskMaterialAdmissionCommand`；
- `TaskMaterialAdmissionResult`；
- `TaskMaterialBinding`；
- `InventoryService.admit_task()` / `release_task()`；
- processed command payload hash、result、ledger、outbox、ack/replay；
- `EdgeScheduler.reconcile_task_admission()` 与 per-Task saga serialization；
- `WorkflowService.project_material_admission()`；
- `workflow_task_material_admission_projection`；
- W1/W2 crash-window recovery 与 terminal release；
- `can_dispatch_task_materials()` fail-closed guard。

M2B 替换 M1R 的“只支持显式 existing Material”临时实现，不保留它作为 fallback。

## 4. Required MaterialSource 集合

一个 Task-wide command 必须包含该 Task **全部 required MaterialSources**。这里的 required
定义为：

- Node 来自 immutable `WorkflowTask.workflow_snapshot`；
- `type == "material_source"`；
- `workflow.db` 中存在该 Task、该 Node 对应的 coordinator-owned pending resolution Job。

因此 disabled 或 debug-scope 外、没有 resolution Job 的 MaterialSource 不进入 command。不得仅扫描
snapshot 中所有 `material_source` 而忽略 Job 集合，也不得从任意 Node 参数/value shape 猜测 Source。

Sources 按 `material_source_node_uuid ASC` 规范排序。Node UUID 顺序只保证 command 和完整匹配结果
确定性；Site 业务优先级仍只来自 `Site.sort_order`。

空集合不调用 `InventoryService.admit_task()`；Task 沿普通 no-Material 路径执行。

## 5. 保持 closed 的 command/result Interface

### 5.1 Command

继续使用 M1R schema version 1：

```text
TaskMaterialAdmissionCommand
  schema_version                 = 1
  command_uuid                   canonical UUID
  idempotency_key                non-empty string
  workflow_task_uuid             canonical UUID
  workflow_snapshot_fingerprint  sha256:<hex>
  sources[]                      complete ordered required set
    material_source_node_uuid
    mode                         existing | create_new
    resource_template_uuid
    mount                        {"uuid": <mount Material UUID>}
    material_uuid                UUID | null
    site_uuid                    UUID | null
    candidate_site_uuids[]       canonical UUID set
    flow_role
```

`command_uuid` 与 `idempotency_key` 继续由
`workflow_task_uuid + workflow_snapshot_fingerprint` 稳定派生。blocked 重试必须复用同一 command，
不能按重试次数创建新 identity。

### 5.2 Inventory result

Inventory 内部持久 result 状态保持：

- `admitted`：整组 binding/Reservation 已提交；
- `blocked`：当前不能形成完整分配，零 partial mutation，可用同一 command 重试；
- `rejected`：确定性合同或当前事实不允许执行，不再自动重试。

`blocked` 可单调升级为 `admitted` 或 `rejected`；`admitted`、`rejected` 是终态。processed command
不得把第一次 `blocked` 永久缓存为不可升级结果。

成功 binding 保持：

```text
TaskMaterialBinding
  material_source_node_uuid
  resource_slot
    uuid
    resource_template_uuid
  site_uuid
```

外部 Workflow wire 仍只使用 `ResourceSlot`；不新增 `MaterialBinding`、`MaterialRef` 或第三种
并发事实。

## 6. Site 候选集合与规范排序

### 6.1 范围

每个 Source 的 Site 范围只能来自 `mount` 直接拥有的 active Sites：

- 有 `site_uuid`：范围恰好是该 Site；
- 有 `candidate_site_uuids`：范围是该集合与 mount 直接 Sites 的精确交集，任何不存在、已删除、
  非直接 owner 或模板不兼容成员都 reject 整个 command；
- 两者都无：范围是 mount 的全部 compatible direct Sites；
- 两者同时存在或 candidate set 为空：`invalid_material_source`，reject。

不递归进入 mount 的子 Material，不解释 PLR label/range，不跨 Warehouse fallback。

### 6.2 顺序

除 exact Site 外，所有模式都按以下稳定顺序构造候选：

```sql
ORDER BY site.sort_order ASC, site.uuid ASC
```

CandidateSiteSet 的输入顺序只用于 canonical payload，不覆盖 Site order。`sort_order` 相同时以 UUID
作为稳定 tie-breaker。不得使用 Material create time、barcode、name、lot FIFO、查询返回顺序或
Python collection 顺序改变结果。

### 6.3 Task-wide 完整匹配

`InventoryService.admit_task()` 必须在一个 transaction 内寻找按上述候选顺序的、字典序最小的
**完整 Task-wide assignment**，而不是对每个 Source 做会产生假冲突的独立 greedy commit：

- 每个 Source 恰好取得一个 Site 和一个具体 Material；
- 一个 Site 最多属于一个 Source binding；
- 一个 Material UUID 最多属于一个 Source；
- fixed existing choice 先作为硬约束；
- 其余候选按 Source UUID 顺序和 Site order 搜索；
- 存在完整 assignment 时必须选出第一个完整解；不能因为前一个 Source 贪心占用后一个 Source 的
  唯一 Site 而错误返回 blocked；
- 无完整解时整组 blocked，零 partial mutation。

匹配算法属于 `InventoryService` 的内部 Implementation，不新增外部 Interface 或可替换 orderer
Adapter。

## 7. `mode=existing`

### 7.1 固定 Material

`material_uuid` 非空时：

1. Material 必须存在、未删除、`material_kind=business`；
2. `resource_template_uuid` 必须精确相等；
3. disposition 必须可运行；
4. Material 必须当前占用本 Source 范围内恰好一个 compatible direct Site；
5. 其完整 business subtree 可形成 Task Reservation。

第 4 条不满足时返回 `rejected/material_location_mismatch`：不等待、不搬运、不修改 occupancy。

固定 Material 被其他 Task Reservation 暂时持有时返回 `blocked/material_reserved`。`reconciling`
等可恢复保护状态返回 `blocked/material_unavailable`；`consumed`、`discarded`、`quarantined` 等
稳定不可运行状态返回 `rejected/material_not_runnable`。

### 7.2 自动 existing

`material_uuid` 为空时，只考虑范围内 Site 当前 occupant：

- occupant 存在、未删除；
- business Material；
- exact template；
- runnable；
- 不被另一 Task Reservation 保护；
- 未被本 command 的另一 Source 选择。

候选按 Site order 参与完整匹配。没有完整 assignment 时返回
`blocked/material_unavailable`；不得隐式切换 `create_new`，不得跨 mount 搜索或移动物料。

existing 成功不修改 Site occupancy、Material identity、barcode、name、config/data 或 disposition。

## 8. `mode=create_new`

`create_new` 禁止 `material_uuid`。它只在本 Source 范围内选择一个 compatible、active、当前为空且
可使用的 direct Site，并按 Site order 参与完整 Task-wide assignment。

若没有完整 assignment，返回 `blocked/site_unavailable`，不得创建孤立 Material。

成功时在同一个 `inventory.db` transaction 内：

1. 生成一个 canonical UUIDv4 Material UUID；
2. 插入 Backend-aligned business Material：
   - `resource_template_uuid` 来自 selector；
   - `class` 与 `name` 使用 immutable `ResourceTemplateIdentity.material_class`；
   - `barcode=""`；
   - `description=null`、`parent_uuid=null`；
   - `meta_data={}`、`config={}`、`data={}`；
   - `disposition=active`、`material_kind=business`、`version=1`；
3. 把选中 Site 的 `occupied_material_uuid` 更新为新 Material UUID 并推进 Site version；
4. 把新 Material 加入本 Task 的完整 Reservation set；
5. 写 binding、ledger、outbox 与 processed command result。

UUID 在 commit 前崩溃时可以重新生成，因为没有任何事实已发布；commit 后必须从 processed result
重放同一个 UUID。不得额外暴露“Material factory”公共 Interface；创建 helper 是
`InventoryService` 的内部 Seam。

### 8.1 Inventory deduction 明确排除

`create_new` 不执行以下行为：

- 查询或锁定 `inventory_lot`；
- FIFO lot selection；
- quantity available/reserved/total 变化；
- stock reservation、deduction、consumption 或 effect；
- 从 substance/content lot 推导新 Material payload；
- 在 Workflow/MaterialSource 中增加 Inventory bypass/policy 参数。

legacy lot/FIFO 测试继续作为 Inventory 回归，但不是 M2B acceptance 行为。未来真实业务要求扣减时，
另开 Authority/deployment 级 Decision，不扩张本 command version 1。

## 9. 一个 inventory.db 原子边界

一个 command 的成功 transaction 必须同时提交：

- 全部 selector/template/mount/Site/current Material 重验；
- 完整 Task-wide assignment；
- 所有 `create_new` Material rows；
- 相应 Site occupancy/version；
- 一个 active Task Material Reservation 及全部 subtree members；
- 所有 bindings；
- Material/Site/Reservation ledger；
- outbox；
- processed command payload hash 与完整 result。

任一 Source blocked/rejected 或 SQLite transaction 失败时，不得留下：

- 部分 Material；
- 部分 Site occupancy/version；
- 部分 binding；
- 部分 Reservation/member；
- stock/lot 变化；
- 可被 Workflow 投影消费的 partial result。

Inventory transaction 内不得调用 WorkflowStore、Registry、driver、SSE、HTTP 或 ROS。ResourceTemplate
identity 在进入 transaction 前已冻结为 `InventoryService` 的只读 snapshot。

## 10. WorkflowTask 状态机

### 10.1 新状态

在 durable `WorkflowTask.status` 增加：

```text
admission_blocked
```

它表示 immutable Task 已创建，但 Task-wide Material admission 当前没有完整 assignment 或完整
Reservation。它是 Task 状态，不是 Job failure、Task terminal、projection-only hint 或前端本地状态。

本轮不增加 durable `admitting` 状态。一次正在进行的协调由 per-Task saga serialization、durable
command/result 和 transaction 表达；把短暂调用期暴露为 Task 状态只会扩大 Interface 而不增加可恢复
事实。

### 10.2 状态转换

```text
pending
  ├── admitted ------------------------------> pending -> running
  ├── blocked -------------------------------> admission_blocked
  ├── rejected ------------------------------> failed
  └── cancel --------------------------------> canceled

admission_blocked
  ├── retry admitted ------------------------> pending
  ├── retry rejected ------------------------> failed
  ├── same blocked result -------------------> admission_blocked
  └── cancel --------------------------------> canceled
```

`pending -> pending` 的 admitted 标记不是状态 transition；同一个 workflow transaction 先投影完整
binding/Reservation proof，runtime 随后才可按普通规则进入 `running`。

Task `admission_blocked` 时：

- 所有 MaterialSource resolution Jobs 保持 `pending`；
- 普通 Action Job 不得 dispatch；
- `pause/resume/step` 不生效；
- `cancel` 必须可用并进入既有 terminal release/cleanup saga；
- Task list/detail/filter、startup scan、SSE invalidation rehydrate 必须可见。

### 10.3 Workflow projection 的原子变化

`WorkflowService.project_material_admission()` 对一次 Inventory result 在一个 `workflow.db` transaction
中完成：

- `admitted`：blocked projection 单调升级；写全部 resolution Job
  `status=succeeded + return_info={"material": ResourceSlot}`；把 Task
  `admission_blocked -> pending`；
- `blocked`：写/保持 projection；把 Task `pending -> admission_blocked`；不改 Job result；
- `rejected`：blocked projection 可单调升级；把全部仍 pending 的 required resolution Jobs 标为
  `failed` 并保存稳定 Source diagnostic；把 Task 置 `failed`；
- 每次真正变化只追加一个 `workflow.runtime.changed` invalidation；同 result replay 不重复写 Job、
  Task event 或 terminal error。

`workflow_task_material_admission_projection.status=blocked` 继续保存跨库 result 证据，但公开等待真值是
`WorkflowTask.status=admission_blocked`。两者不得出现 Task 已 running 而 projection 仍 blocked 的组合。

## 11. Outcome 分类

### 11.1 Rejected

以下是确定性拒绝，Task 最终 `failed`：

| code | 条件 |
|---|---|
| `invalid_material_source` | closed DTO、mode/字段组合、UUID、duplicate Source/Material、空 CandidateSiteSet |
| `resource_template_not_found` | selector 模板不在冻结的 Inventory identity snapshot |
| `mount_not_found` | mount Material 不存在或已删除 |
| `site_not_found` | exact/candidate Site 不存在或已删除 |
| `site_scope_mismatch` | Site 非 mount 直接拥有 |
| `site_template_mismatch` | Site allowlist 不允许 selector template |
| `material_not_found` | 固定 Material 不存在或已删除 |
| `material_template_mismatch` | 固定 Material template 不匹配 |
| `material_location_mismatch` | 固定 Material 不在规范 Site 范围内 |
| `material_not_runnable` | 固定 Material 稳定 disposition 不允许执行 |
| `task_material_set_conflict` | Task 已持有不同 fingerprint 的 active Reservation |

Source-specific diagnostic 必须带 `material_source_node_uuid`；command-level diagnostic 的该字段为
`null`。错误不得泄露 SQL、任意候选 UUID 列表、绝对路径或 driver exception。

### 11.2 Blocked

以下是可恢复等待，Task 为 `admission_blocked` 且零 partial mutation：

| code | 条件 |
|---|---|
| `material_unavailable` | 自动 existing 当前没有完整可运行候选，或候选处于可恢复保护状态 |
| `site_unavailable` | create_new 当前没有完整空 Site assignment |
| `material_reserved` | 候选 Material/subtree 被另一 Task Reservation 保护 |
| `observation_unknown` | 后续 M2C 提供的有效 occupancy observation 明确要求 fail-closed |

基础 M2B 不自己实现 sensor projector；没有 M2C observation 时只使用 durable Site occupancy，不伪造
`observation_unknown`。

### 11.3 基础设施失败

SQLite/Store/Interface 不可用返回/抛出 `material_authority_unavailable`，不得伪装为 blocked、rejected
或 admitted。Task 保持原状态并由 coordinator/operator recovery 重试；不得 fail-open dispatch。

## 12. Retry、唤醒与恢复

- startup reconciliation 扫描 `pending` 与 `admission_blocked` Task；
- terminal reconciliation 继续先处理 release saga；
- Material 创建/更新、Site occupancy 变化、Reservation release 与后续 observation recovery 可以唤醒
  `admission_blocked` Task；
- v1 可采用 durable event 后的全量稳定顺序 sweep，不要求未来计划层或新队列；
- sweep 顺序为 Task `create_time ASC, uuid ASC`；
- 同一 Task 仍由 M1R per-Task saga serialization 串行；
- 相同 blocked result 不重复 outbox、Task event 或更新时间；
- release 后唤醒不能在持有 Inventory transaction 或 Workflow transaction 时递归进入 admission。

公平性仅保证稳定 Task/Site 顺序；本轮不实现优先级、抢占、未来预约或 starvation lease。长期公平性由
后续 Scheduler Decision 承担。

## 13. 跨数据库时序与 crash windows

继续使用 M1R durable saga；不 ATTACH 两库，不同时持有两个 transaction：

1. `workflow.db` 已有 immutable Task/snapshot/Plan 与全部 required resolution Jobs；
2. EdgeScheduler 构造一个稳定 Task-wide command；
3. `InventoryService.admit_task()` 提交完整 inventory result；
4. EdgeScheduler 在一个 workflow transaction 投影 result、Jobs 与 Task 状态；
5. workflow commit 后 acknowledge Inventory outbox；
6. transaction 外发布 SSE invalidation。

强制故障窗口：

- **W1**：inventory commit 后、workflow projection 前。重启后重放相同 result，不重复 Material、
  occupancy、Reservation、binding 或 ledger；
- **W2**：workflow projection 后、inventory ack 前。重启后不重复 Job/Task transition、error_info、
  runtime event 或新 Material；
- **W0**：create_new UUID 生成后、inventory commit 前。回滚后不存在可观察 identity；重试允许新 UUID；
- **W3**：blocked Task 被唤醒后、升级 admitted/rejected 前。原 blocked 状态可重放，不能先把 Task 改
  pending/running。

## 14. Dispatch 与 M1EF 接缝

M2B 只证明 Task-wide binding 与 Reservation：

- 每个 required resolution Job 已 `succeeded`；
- admission projection `admitted`；
- Task 不再 `admission_blocked`；
- `reservation_uuid` 对该 Task 仍 active/current。

任何一项不成立，普通 Action dispatch 为 0。

M1EF [OS #15](https://github.com/Uni-Lab-OS/Uni-Lab-OS/issues/15) 不阻塞 M2B binding
Implementation，但真实 physical Action dispatch/result safety 还必须取得完整 Job Execution Claim/fence、
提交 fenced ChangeSet 并完成 unknown/reconciliation。M2B 不以旧 `_job_resource_locks` 或
`@action(lock_resource=...)` 冒充该能力。

## 15. 文件处置与 Module 深度

| 文件 | preserve | deepen / rewrite | 不得新增/暴露 |
|---|---|---|---|
| `inventory/domain.py` | command/result/binding DTO | closed status/diagnostic typing（如需要） | per-source public allocator DTO |
| `inventory/service.py` | `InventoryService.admit_task()` Interface、transaction、processed result、Reservation | 两种 mode、Site-order full matching、create_new、blocked→terminal upgrade | public candidate query/sort/factory methods |
| `inventory/store.py` | WAL/transaction/schema/row helpers | 仅 M2B 原子写所需 private helpers/index | Store connection 或 SQL Interface |
| `app/scheduler/service.py` | single command、saga、W1/W2、release、dispatch proof | required-job set、blocked Task scan/wakeup、result projection order | 第二 coordinator、per-source Inventory call |
| `workflow/store.py` | Task/Job/projection/event transaction | `admission_blocked` 状态与 admitted/blocked/rejected 原子投影 | Material/Site/Reservation tables |
| `workflow/service.py` | closed result Adapter | 新 Task status/error 映射 | allocation policy |
| `workflow/runtime.py` |唯一 Task/Job transition kernel、worker Adapter | 新 Task transition、scan、cancel/dispatch guard | 第二 admission loop/DAG walker |
| `workflow/composition.py` / `scheduler/integration.py` | M1R Inventory/Workflow wiring | 只在现有 Adapter 接线缺失时最小修改 | 新 Module 或第二 Inventory instance |

本轮测试通过相同公共 **Interface** 验证深 Module；不得测试 private candidate helper/SQL 行数来锁死
Implementation。SQLite 是 local-substitutable dependency，使用真实临时 `inventory.db`/`workflow.db`，
不为测试新增外部 Store port。

## 16. Independent RED

按 `AGENTS.md`，恰好一个独立 test-author 在独立 worktree 和 `test/m2b-*` branch 上，从精确
`cf6f81da8bf41950c8779555c60a7b7349184fbe` 先提交 tests-only RED。禁止与 Implementation owner
并发工作。

第一条 vertical RED：

```text
Task snapshot:
  Source A: existing, no fixed Material, broad compatible occupied Site set
  Source B: existing, no fixed Material, one compatible occupied Site
  Source C: create_new, one compatible empty Site

Inventory:
  several Sites whose UUID order differs from sort_order
  one complete assignment exists only if A does not greedily take B's sole occupied Site

Note:
  existing 只能绑定 occupied Site，create_new 只能绑定 empty Site；二者本身不会争用同一 Site。
  因此用 A/B 证明 Task-wide 回溯，用 C 在同一事务证明跨 mode 原子性。

Expected:
  one Task-wide admit_task call
  lexicographically first complete Site-order assignment
  one new Material and Site occupancy
  one complete Task Reservation
  all three resolution Jobs succeeded with typed ResourceSlot return_info
  Task remains pending and becomes dispatch-eligible
  inventory_lot rows unchanged
```

随后覆盖以下 RED：

### 16.1 Site order / matching

- exact Site；
- CandidateSiteSet 输入 UUID 顺序与 `sort_order` 相反；
- equal sort order UUID tie-break；
- all-compatible direct Sites；
- 非 direct Site reject；
- 完整匹配存在时不被 greedy 假冲突；
- 同一 Material/Site 不可绑定两个 Sources。

### 16.2 Existing

- fixed happy path；
- fixed location mismatch → rejected/Task failed；
- fixed reserved → admission_blocked；
- automatic existing Site-order selection；
- no candidate → admission_blocked；
- template/disposition/not-found 分类；
- existing 不写 occupancy/Material/stock。

### 16.3 Create new

- first compatible empty Site by Site order；
- atomic Material + occupancy + Reservation + binding；
- no empty Site → admission_blocked，零 Material；
- duplicate/replay 返回同一 Material UUID；
- W0 rollback 无孤立 identity；
- `inventory_lot`、quantity、stock effect 全部不变。

### 16.4 Task 状态与 projection

- pending → admission_blocked；
- admission_blocked replay 无重复 event；
- admission_blocked → pending on admitted；
- admission_blocked → failed on later deterministic reject；
- blocked resolution Jobs 保持 pending；
- rejected resolution Jobs failed；
- cancel admission_blocked Task；
- list/detail/filter/startup scan 支持新状态；
- admitted 前 dispatch 为 0，Reservation 失效后 dispatch 为 0。

### 16.5 Task-wide/cross-DB

- multi-Source success；
- 任一 Source blocked/rejected 全组零 partial write；
- duplicate command same/different payload；
- concurrent Tasks Site/Material contention；
- W1/W2/W3；
- SQLite WAL close/reopen；
- terminal release 与 release 后 wakeup；
- stale snapshot/command/projection conflict fail closed。

## 17. 完整 Gate

### 17.1 Targeted

- 新 `test_m2b_*` suites；
- 全部 `test_m2a_*` 与 M2A review regressions；
- M1R Inventory admission/contention/release/replay/crash-window suites；
- D1A formal Task runtime bridge 与 scheduler regressions；
- Workflow Task/Job state、commands、REST/SSE contract tests。

### 17.2 Static/retirement

- production 只有一个 `InventoryService.admit_task()` external Seam；
- 无 `inventory_lot`/stock deduction call from M2B；
- 无 per-source Inventory commit；
- 无 `resources.authority`、`MaterialModule`、`material.db` 或 shared UoW；
- 无 `admitting` durable Task status；
- 无固定 Material location mismatch → blocked 路径；
- 无 UUID-order、lot-FIFO 或 query-order candidate selection；
- 无普通 Action 在 admission_blocked/admission incomplete 时 dispatch。

### 17.3 Repository

- round-target tests；
- 完整 `pytest -q -rs tests`；
- changed-files Ruff check/format；
- changed production `py_compile`；
- `git diff --check`；
- exact candidate worktree clean；
- exact-SHA Standards/Spec review `0B/0NB`；
- ledger 记录 test-author、RED commit、Implementation commits、commands/results、reviewer 与 finding
  disposition；
- 不 squash test-author/review provenance，不在未获授权时 push。

## 18. Non-goals

- Inventory lot/quantity deduction、reservation、consumption、FIFO 或 `InventoryPolicy`；
- Job Execution Claim/fencing、Material/Site ChangeSet、dispatch unknown reconciliation（M1EF）；
- sensor acquisition/debounce/TTL/projector（M2C）；
- Task Material/Site REST-SSE 专用读模型（M2D）；
- FE MaterialSource 表单、typed port 与浏览器 E2E；
- Backend→OS Authority/Reservation handoff；
- quantity/cardinality/collection/`list[ResourceSlot]`；
- 多 Warehouse fallback、route variant、自动搬运、Pick/Place/AGV/人工 Job；
- `apply_deduct_resource`；
- future Scheduler、抢占、优先级、未来预约或 Go `uni-lab-scheduler`；
- 修改 M2A static schema、Catalog identity 或 material fan-out contract。

## 19. 出门条件

M2B 只有在以下全部完成后才能从 implementation 进入 testing：

1. 本设计以 immutable commit 固定并通过 Spec review；
2. 独立 tests-only RED 在精确 integration 基线上证明缺失行为；
3. 两种 mode、Site-order full matching、Task-wide all-or-none 与 Task
   `admission_blocked` 状态全部实现；
4. W0/W1/W2/W3、duplicate、concurrency、restart/reopen、cancel/release 通过；
5. M2A/M1R/D1A 回归与完整 suite 通过；
6. Feishu 协议、Implementation map 与测试证据记录相同 contract version 和 exact SHAs；
7. owning Issue、Wayfinder Map 与当前 publication/integration anchors 一致。

M2B 完成只证明 MaterialSource binding 与 Task Reservation；不得据此宣称真实 physical Action 的
Claim/fence/ChangeSet 已接受。
