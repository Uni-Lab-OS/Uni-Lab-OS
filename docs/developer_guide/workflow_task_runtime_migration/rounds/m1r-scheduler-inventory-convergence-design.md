# M1R：现有 Scheduler Inventory 原位深化设计

日期：2026-08-02

状态：implementation spec candidate

权威票据：

- Core Decision：[`Uni-Lab-OS/Uni-Lab-Core#161`](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/161)
- OS owning delivery：[`Uni-Lab-OS/Uni-Lab-OS#14`](https://github.com/Uni-Lab-OS/Uni-Lab-OS/issues/14)
- M1 umbrella：[`Uni-Lab-OS/Uni-Lab-OS#6`](https://github.com/Uni-Lab-OS/Uni-Lab-OS/issues/6)
- M2A/M2B：[`Uni-Lab-OS/Uni-Lab-OS#8`](https://github.com/Uni-Lab-OS/Uni-Lab-OS/issues/8)、
  [`#9`](https://github.com/Uni-Lab-OS/Uni-Lab-OS/issues/9)

实现基点：`integration/workflow-task-runtime@8fad069c16faeb991fade5232eaf84ef32b17146`

本设计 supersede
`rounds/m1-material-authority-foundation-design.md` 中以下落位：

- `unilabos.resources.authority.MaterialModule`；
- Material 与 Workflow 共用 SQLite connection/Unit of Work；
- 把现有 `unilabos.app.scheduler.inventory` 迁走或降为临时 donor；
- `resources.inventory.InventoryModule` 或 `material.db`。

旧 M1A～M1D 文档仍保存字段、不变量、RED provenance、评审和测试证据；只有与本设计冲突的
package、数据库与事务落位被 supersede。

## 1. Outcome

M1R 是一个不可部分合入的 atomic delivery：在现有
`unilabos.app.scheduler.inventory` 中深化现有 `InventoryService`、`InventoryStore` 和
配套文件，把 M1A～M1D 已完成的 Material/Site/ResourceSlot/Task Reservation 行为并入同一
`inventory.db`，同时建立 `EdgeScheduler` 驱动的 `workflow.db ↔ inventory.db` durable
coordination。

最终只保留：

```text
unilabos.app.scheduler.service.EdgeScheduler
        │ readiness / admission / dispatch / completion authority
        ├── unilabos.app.scheduler.dag_state   # 唯一 DAG 引擎
        └── durable commands/results
                    │
                    ▼
unilabos.app.scheduler.inventory.InventoryService
                    │
                    ▼
                inventory.db
```

`unilabos.workflow` 继续拥有 `workflow.db` 中的 Workflow、WorkflowTask、WorkflowNodeJob、
Task commands、feedback、snapshot 和执行投影；它不拥有 Material 表，不借用
`InventoryStore` connection，也不成为第二个 Scheduler。

## 2. 为什么合并为一个 delivery

M1R-0～M1R-3 只作为 #14 内部 checkpoint，不是四个可独立合入的 slice：

1. 只切 package、不切 schema，会同时存在 `resources.authority` 与 legacy Inventory 两个真值；
2. 只切 schema、不切 Scheduler 调用，会让旧 `workflow_id/node_id/instance_uuid` DTO 写入新表；
3. 只切 Inventory、不完成跨库 replay，会在 inventory commit 后的崩溃窗口留下不可恢复状态；
4. 保留中间 shim、dual-read 或 dual-write 会违反唯一 Material authority。

因此使用一个 branch、一个 owning ticket、一个最终 exact candidate SHA；branch 内保留下列可审查
commit/checkpoint，但在全部 gate 通过前不合入 integration：

1. implementation spec + independent RED；
2. Backend-aligned schema 与 M1A～M1D 并入；
3. legacy Inventory 算法和 caller 原位深化；
4. EdgeScheduler 跨库 command/result/replay；
5. review fixes、全量 gate 与 ledger。

## 3. Authority 与调用约束

### 3.1 EdgeScheduler

`unilabos.app.scheduler.service.EdgeScheduler` 是唯一的：

- DAG readiness owner；
- Material admission coordinator；
- Job dispatch owner；
- Node terminal 与 Task completion owner；
- `workflow.db ↔ inventory.db` durable command/result replay coordinator。

`dag_state.py` 保持唯一 DAG state engine。Inventory 不解释 branch、join、loop、breakpoint、step、
debug scope 或 Job readiness。

### 3.2 InventoryService

保留并深化现有 `unilabos.app.scheduler.inventory.InventoryService`，不引入
`InventoryModule`、第二个 Material facade 或新顶层 package。Scheduler、HTTP adapter 和测试只通过
该 public Interface 调用 Material/Inventory 行为。

InventoryService 隐藏：

- SQLite connection 与 transaction；
- lot/FIFO candidate rows；
- Material/Site row mapping；
- processed command、ledger、outbox 与 cursor rows；
- legacy allocation helper 和内部 DTO。

driver、Frontend、WorkflowStore 与 Registry 不得直接访问 `InventoryStore` 或表。

### 3.3 Registry 与 ResourceTreeSet

- Registry/Package Catalog 继续拥有 ResourceTemplate identity 与定义；
- Inventory 通过注入的只读 lookup 获取 `resource_template_uuid` 和必要 immutable fingerprint；
- `inventory.db` 不复制一套可独立修改的 ResourceTemplate authority；
- `ResourceDict`/`ResourceTreeSet` 是 execution projection，不是 durable Material truth；
- M1R 不轮询、双写或用内存 projection 覆盖 SQLite。

外部 `uni-lab-scheduler` Go 仓库不在本轮范围内。

## 4. 最终文件树与逐文件处置

不新建 package；现有文件名保持：

```text
unilabos/app/scheduler/
  service.py
  dag_state.py
  integration.py
  main.py
  inventory/
    __init__.py
    api.py
    commands.py
    domain.py
    domains.py
    layout.py
    service.py
    store.py
    sync.py
    warehouse.py
```

| 文件 | preserve | rewrite / deepen | retire |
|---|---|---|---|
| `inventory/domain.py` | 已测 errors、lot/requirement value、状态转换 | 并入 Backend-aligned Material/Site/Reservation records；所有 identity 使用 UUID | `edge_uuid`、`legacy_cloud_id`、`instance_uuid` public identity |
| `inventory/store.py` | WAL、RLock、`BEGIN IMMEDIATE`、rollback、query/tx helpers | 统一创建 `inventory.db` schema；并入 M1A～M1D row mapping；processed-command payload hash | `material_instance`、`resource_relation`、旧 node-scoped reservation schema |
| `inventory/service.py` | FIFO、all-or-none、reserve/release/quarantine、ledger/outbox 算法 | 保留 `InventoryService` 并加入 Material/Site CRUD、ResourceSlot、Task admission public methods | 第二 Material facade、旧 public DTO |
| `inventory/commands.py` | command dispatch、result persistence、idempotent replay | 使用 task/job/attempt/fence canonical identifiers 和 closed command/result | `workflow_id/node_id` command identity |
| `inventory/sync.py` | outbox/cursor/reopen/replay | 支持 Scheduler result read/ack 与 terminal cleanup replay | 把 sync cache 当第二 Material truth |
| `inventory/api.py` | FastAPI adapter 与稳定错误映射中仍适用部分 | 只调用 InventoryService；DTO 改为 canonical UUID/Backend fields | 直接 Store/SQL 调用 |
| `inventory/layout.py` | lab layout CRUD/read model | Material/Site 引用改为 canonical UUID | 把 lab placement 当 Site occupancy |
| `inventory/warehouse.py` | 经过测试的 warehouse view 聚合 | 改读统一 Material/Site/stock projection | `material_instance` 第二 identity |
| `inventory/domains.py` | domain pack/catalog 行为 | 引用 Registry identity，不拥有模板真值 | 可独立修改的 `resource_template` authority |
| `inventory/__init__.py` | package 入口 | 只导出稳定 Interface/DTO/error | Store connection、row/internal helper exports |
| `app/scheduler/service.py` | `EdgeScheduler`、`dag_state` 调用、ready/dispatch loop | durable Task admission/release result replay；不再 fail-open | node-name/param-shape 物料猜测、in-memory business Material lock |
| `resources/authority/*` | M1A～M1D 领域行为、字段和测试 provenance | 实现按职责并入上述现有文件 | package 与长期 compatibility shim |

旧文件的内容按函数/不变量逐项迁移，不按目录机械复制。每一项必须在最终 ledger 记录
`preserve | rewrite | retire`、来源 blob、目标位置和替代测试。

## 5. inventory.db lifecycle

### 5.1 路径与进程

- 数据库固定为 `<BasicConfig.working_dir>/inventory.db`；
- 不使用 `~/.unilabos` legacy default；
- 同一 workspace 的 OS process lease 在打开 `workflow.db`/`inventory.db` 前取得；
- InventoryStore 使用 WAL、`foreign_keys=ON`、`synchronous=NORMAL`、busy timeout 和
  `BEGIN IMMEDIATE`；
- close/reopen 后 command result、reservation、ledger、outbox 与 cursor 必须完整。

### 5.2 放弃旧运行数据

本轮从空 `inventory.db` 启动，不实现旧数据库数据迁移。检测到 legacy schema/旧 user version 时
fail closed，并要求操作员归档或删除旧数据库后重启；禁止：

- 自动改写旧数据；
- fallback 到 `material.db`；
- dual-read、dual-write；
- 从 `workflow.db` 或 ResourceTreeSet 回填 Material truth；
- 静默创建第二个数据库。

测试 fixture、Workflow 定义、source、代码与不可变测试证据不在弃数范围。

## 6. Backend-aligned Material schema

### 6.1 `material`

必须保留 M1A 已评审字段与约束：

```text
uuid                         TEXT PRIMARY KEY
create_time                  TEXT NOT NULL
update_time                  TEXT NOT NULL
deleted_at                   TEXT NULL
description                  TEXT NULL
meta_data                    JSON object NOT NULL
resource_template_uuid       TEXT NOT NULL
parent_uuid                  TEXT NULL -> material.uuid
class                        TEXT NOT NULL
barcode                      TEXT NOT NULL DEFAULT ''
name                         TEXT NOT NULL
config                       JSON object NOT NULL
data                         JSON object NOT NULL
disposition                  active|consumed|discarded|quarantined|reconciling|null
material_kind                business|device
version                      INTEGER > 0
```

- business Material 必须有 disposition；device Material 的 disposition 必须为 null；
- 非空 barcode 在未软删除 Material 中 Unicode-casefold unique；空串可重复；
- Material 与 OS Resource Instance 共用 `material.uuid`；
- `material_instance`、`edge_uuid`、`legacy_cloud_id`、`instance_uuid` 不再形成第二 identity。

### 6.2 `site`

```text
uuid                         TEXT PRIMARY KEY
create_time                  TEXT NOT NULL
update_time                  TEXT NOT NULL
deleted_at                   TEXT NULL
description                  TEXT NULL
meta_data                    JSON object NOT NULL
material_uuid                TEXT NOT NULL -> material.uuid
name                         TEXT NOT NULL
sort_order                   INTEGER >= 0
occupied_material_uuid       TEXT NULL -> material.uuid
position_x/y/z               REAL NOT NULL
depth/length/width           REAL >= 0
version                      INTEGER > 0
```

- active Site 的 `(material_uuid, casefold(name))` unique；
- 一个 active Material 同时最多占一个 active Site；
- owner、occupant、composition parent 是三种不同关系；
- `lab_zone/lab_placement` 继续是 2D UI layout，不替代 Site。

### 6.3 `site_allowed_resource_template`

保留规范化关联表：

```text
site_uuid                    TEXT -> site.uuid
resource_template_uuid       TEXT
PRIMARY KEY(site_uuid, resource_template_uuid)
```

不把 allowlist 存成逗号字符串、JSON shadow column 或 Site name 推断。

### 6.4 Task Material Reservation

```text
material_reservation
  uuid                       TEXT PRIMARY KEY
  workflow_task_uuid         TEXT NOT NULL
  set_fingerprint            TEXT NOT NULL
  status                     active|released
  create_time                TEXT NOT NULL
  released_at                TEXT NULL

material_reservation_member
  reservation_uuid           TEXT -> material_reservation.uuid
  material_uuid              TEXT -> material.uuid
  root_material_uuid         TEXT -> material.uuid
  acquired_version           INTEGER > 0
  released_at                TEXT NULL
  PRIMARY KEY(reservation_uuid, material_uuid)
```

`workflow_task_uuid` 是跨数据库 logical reference，不能声明到不存在于 `inventory.db` 的
`workflow_task` foreign key。一个 Task 只能有一个 active Reservation；一个 Material 同时只能属于
一个 active Reservation。完整 subtree 按稳定 UUID 顺序 all-or-none 获取。

Claim/ChangeSet 表与状态机由后续合并的 M1EF delivery 冻结并实现；M1R 不凭空预实现其字段。

## 7. Legacy stock 与辅助表

以下经评审能力原位保留，但不得成为第二 Material authority：

- `inventory_lot`：保留 FIFO、数量不变量、expiry/quarantine/version；引用字段统一为
  `resource_template_uuid`；
- `inventory_ledger`：与业务变更同 transaction；aggregate identity 使用 canonical UUID；
- `sync_outbox`：与 result/Material 变更同 transaction；
- `processed_command`：加入 stable payload hash；同 command + 同 payload 返回原 result，同 command +
  不同 payload 返回 conflict；
- `sync_cursor`：保存 Scheduler acknowledgement；
- `lab_meta/lab_zone/lab_placement`：保留独立 UI layout 语义。

旧 `resource_template` 表不能继续作为 Registry 的可修改副本。旧 `resource_relation` 不再拥有
placement truth；composition 使用 `material.parent_uuid`，placement 使用 Site occupancy。旧
`substance_content` 行为在 #146 决策前不扩展为新 public contract；如保留内部 stock capability，键必须
改为 `material_uuid` 且不能覆盖 `material.data`。旧 `inventory_reservation` 的 workflow/node identity
被 Task Material Reservation 和内部 stock allocation result 取代。

## 8. InventoryService public Interface

M1R 保留已有 Material/Site CRUD 与 ResourceSlot 行为，并冻结 M2B 所需的最小深 Interface：

```python
class InventoryService:
    # M1A～M1C
    def create_material(...) -> MaterialRecord: ...
    def get_material(material_uuid: str) -> MaterialRecord: ...
    def create_site(...) -> SiteRecord: ...
    def get_site(site_uuid: str) -> SiteRecord: ...
    def list_sites(material_uuid: str) -> tuple[SiteRecord, ...]: ...
    def resolve_resource_slot(...) -> ResourceSlotResolution: ...

    # M1D / M2B deep seam
    def admit_task(
        command: TaskMaterialAdmissionCommand,
    ) -> TaskMaterialAdmissionResult: ...
    def release_task(
        command: TaskMaterialReleaseCommand,
    ) -> TaskMaterialReleaseResult: ...

    # cross-DB replay
    def get_command_result(command_uuid: str) -> InventoryCommandResult: ...
    def read_outbox(after_sequence: int, limit: int) -> tuple[InventoryEvent, ...]: ...
    def acknowledge(sequence: int) -> None: ...
```

具体 HTTP routes 不在 M1R 新增 shared frontend contract；现有 private Inventory adapter 只做 Interface
投影与稳定错误映射。

### 8.1 Admission command

`TaskMaterialAdmissionCommand` 是 closed、versioned、JSON-canonical DTO：

```text
schema_version
command_uuid
idempotency_key
workflow_task_uuid
workflow_snapshot_fingerprint
sources[]
  material_source_node_uuid
  mode                       existing|create_new
  resource_template_uuid
  mount                      ResourceSlot reference
  material_uuid              nullable
  site_uuid                  nullable
  candidate_site_uuids[]     canonical ordered unique
  flow_role
```

command 携带 Task snapshot 中全部 required MaterialSources，不能逐 Source 调用。InventoryService 在一个
`inventory.db` transaction 中完成 selector/template/Site 检查、Material/stock 选择、全部 Task
Reservation、ledger、outbox 与 processed result，整组 all-or-none。

结果是 closed DTO：

```text
schema_version
command_uuid
workflow_task_uuid
status                       admitted|blocked|rejected
reservation_uuid             nullable
bindings[]
  material_source_node_uuid
  resource_slot              canonical typed value
  site_uuid
diagnostics[]
outbox_sequence
```

- transient stock/Site/Reservation contention 返回 `blocked`，零 partial rows；
- selector/schema/type/not-found 等确定性错误返回 `rejected`；
- 同 key 同 payload replay 返回完全相同的 Material/Site/binding/result；
- read-only preflight 不创建 Reservation，也不授权 dispatch。

M2B 冻结 selector 的完整业务语义和 workflow projection；M1R 只提供上述事务与 replay seam。

## 9. 跨数据库 durable coordination

SQLite 不提供 `workflow.db + inventory.db` 单 connection transaction。禁止 attach 两库后声称共享本地
事务；使用 EdgeScheduler durable saga：

### 9.1 Admission

1. `workflow.db` 已持久化 pending Task、immutable snapshot 与 MaterialSource resolution Jobs；
2. EdgeScheduler 从 snapshot 规范化完整 command，生成由 Task UUID + snapshot fingerprint 派生的稳定
   idempotency key；
3. InventoryService 在 inventory transaction 写入 Material/Site/Reservation/ledger/outbox/result；
4. EdgeScheduler 在独立 workflow transaction 投影全部 per-source typed `return_info` 和 admission state；
5. workflow commit 后，Scheduler acknowledgement 进入独立 inventory transaction；
6. acknowledgement 丢失时，outbox/result 继续 replay，workflow projection 必须幂等。

### 9.2 两个强制 crash window

必须有可重复、可重启测试：

- **W1**：inventory commit 后、workflow projection 前崩溃。重启后重新提交同 command，只完成 workflow
  projection；不得重复选择、创建、预留、扣减或占 Site。
- **W2**：workflow projection commit 后、inventory acknowledgement 前崩溃。重启后 replay 不得创建第二
  Job result、第二 Reservation、第二 feedback/event 或改变已冻结 binding。

### 9.3 Terminal release

Task terminal 时 EdgeScheduler 生成 stable release command。InventoryService 幂等释放该 Task 的 active
Reservation并写 ledger/outbox/result；workflow cleanup projection 和 inventory acknowledgement 仍按上述
顺序执行。失败不得 best-effort 吞掉，必须保持可重试和可观测。

### 9.4 Dispatch guard

普通 Action Job 只有在以下条件同时成立时才可进入 `dag_state` admission/dispatch：

1. Task-wide admission result 已完整投影到 workflow.db；
2. 每个 required MaterialSource resolution Job 有 typed binding；
3. InventoryService 证明对应 Task Reservation 仍 active/current；
4. 后续 M1EF 完成后，还必须具有当前 Job Claim/fence。

M1R 不恢复前端轮询或第二 DAG walker。Scheduler startup reconciliation 可以扫描 durable pending Task/outbox，
但 UI 仍只通过 REST + 全局 SSE 读取投影。

## 10. Transaction 与 lock order

任何代码路径不得同时持有 WorkflowStore 和 InventoryStore transaction。顺序固定为：

1. workspace process lease；
2. EdgeScheduler command serialization（只决定下一 durable operation，不持有它调用 SQLite/driver）；
3. InventoryStore RLock + `BEGIN IMMEDIATE`，完成 inventory mutation/result/outbox 后释放；
4. WorkflowStore transaction，完成 Task/Job projection/outbox 后释放；
5. InventoryStore acknowledgement transaction；
6. transaction 外进行 SSE publish、HTTP、ROS、driver 或文件 I/O。

Inventory transaction 内不得回调 WorkflowStore、Registry、driver 或 Scheduler。Registry template snapshot/
fingerprint 在 transaction 前取得；transaction 内只用 command 中冻结的 UUID/fingerprint 与当前
Material/Site/stock truth。

## 11. M2A 适配

M2A 的静态 schema、closed selector、Python/DAG round-trip、stable node/handle identity、template
compatibility 和 material fan-out `<=1` 保持接受。

因独立 DB 决策，M1R 必须移除：

- `unilabos.resources.authority` imports；
- `MaterialModule`/`SQLiteMaterialAdapter.from_runtime_authority()`；
- `RuntimeAuthorityUnitOfWork`；
- M2A 对 WorkflowStore connection 的 borrowed-UoW。

Authoring composition 通过注入的窄 read Interface 调用 InventoryService。Preview/Save/Apply 不读取动态
occupancy、stock 或 Reservation；固定 Material/Site identity 的静态诊断是当时读模型，不宣称与
workflow.db 原子。Task admission 必须重验所有 Material/Site/Reservation 事实。因此旧 M2A shared-UoW
TOCTOU tests 由明确的 cross-DB static-read + Task revalidation tests 替代；不得简单删除而没有替代证据。

## 12. Error taxonomy

保持稳定 public 分类：

- `400 invalid_input`：closed DTO、UUID、selector、template/type、字段组合错误；
- `404 not_found`：指定 Material/Site 不存在或已软删除；
- `409 conflict`：同 idempotency key 不同 payload、稳定状态冲突、删除受保护实体；
- `blocked` result：当前 stock/Site/Reservation contention，不映射为输入错误；
- `material_authority_unavailable`：数据库或基础设施不能完成请求，不能 fail-open dispatch。

错误不得泄露 SQLite、driver exception、内部候选 UUID 集合或敏感路径。

## 13. RED 与验收矩阵

本 atomic delivery 在任何 production change 前使用一个独立 test-author，提交 tests-only RED。AGENTS.md
的“恰好一个 test-author、恰好一个 reviewer”规则高于旧 round-gate template 中的多角色占位。

### 13.1 Static/package gate

- `unilabos.app.scheduler.inventory.InventoryService` 是唯一 Material/Inventory public seam；
- production/tests 无 `unilabos.resources.authority`、`MaterialModule`、`InventoryModule`、`material.db`；
- 无 `SQLiteMaterialAdapter.from_runtime_authority` 或 borrowed WorkflowStore UoW；
- `app/scheduler/dag_state.py` 仍是唯一 DAG engine；
- 外部 Go Scheduler repo 无改动。

### 13.2 Database gate

- 空 `inventory.db` 创建 exact material/site/allowlist/reservation schema；
- Backend fields、JSON checks、barcode casefold、soft delete、Site uniqueness 与 disposition constraints；
- `material_reservation.workflow_task_uuid` 无跨 DB foreign key；
- WAL/reopen/user-version/legacy DB fail-closed；
- 无 fallback/dual-read/dual-write；
- `inventory_lot` 数量不变量和 FIFO 回归保持。

### 13.3 Behavior gate

- ResourceSlot 稳定 400/404/409；
- Task subtree Reservation、并发 contention、duplicate replay、all-or-none；
- 全部 MaterialSources 单 transaction；
- transient blocked 零 partial rows；
- terminal release 不误释放其他 Task；
- layout/warehouse/HTTP/commands/sync 现有测试保持或有逐项替代。

### 13.4 Crash/recovery gate

- W1、W2 两处 deterministic crash injection；
- restart/reopen/duplicate command/outbox replay/ack；
- release command crash/replay；
- admission projection 不完整时 dispatch 为 0；
- 当前 Reservation 失效时 dispatch 为 0。

### 13.5 Full gate

- M1A～M1D + M2A + legacy Inventory/Scheduler targeted suites；
- 完整 `tests/workflow`；
- 完整 `pytest -q -rs tests`；
- changed-files Ruff check/format；
- changed production `py_compile`；
- `git diff --check`；
- exact candidate worktree clean。

## 14. Review 与合并

- 一个独立 reviewer 同时做 Standards 与 Spec exact-SHA 双审；
- blocking finding 修复后必须重跑 affected + full gate，并对新 exact SHA 复审；
- ledger 记录 test-author、tests-only commit、RED 输出、candidate SHA、命令结果、findings disposition、
  preserve/rewrite/retire matrix；
- 不 squash provenance；
- 全部 checkpoint 完成前不把任何中间状态合入 integration；
- final candidate 只在用户明确授权后 push/merge。

## 15. 明确排除

- M1E/F 的 Job Claim/fencing、ChangeSet、uncertain reconciliation 具体表与状态机；
- M2B 的完整 selector business policy 与 Workflow projection 实现；
- M2C sensor observation、M2D FE read model、M2E live acceptance；
- quantity/cardinality、MaterialSource group、多 Warehouse route；
- #146 `apply_deduct_resource` 最终策略；
- Template Catalog/Registry 重构；
- PackageCatalog 改造；
- R2 ExecutionPlan、D1 device execution、O1 Task output；
- 外部 Go Scheduler。

## 16. 完成条件与后续依赖

OS #14 只有在本 spec 全部 gate 通过并以一个 exact candidate 合入 integration 后才能完成。之后：

- 合并后的 M1EF ticket 可实现 Claim/fencing + ChangeSet/recovery；
- M2B #9 在 M2A #8 + OS #14 完成后实现 Task-wide MaterialSource business admission；
- M1EF 与 M2B 可按文件冲突和测试资源安排并行；
- 完整 M1/M2 gate 通过后才解除 R2 的 Material dependency。
