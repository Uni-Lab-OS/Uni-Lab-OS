# M1D Task Material Reservation 趋势报告

日期：2026-08-02

本轮从 OS `integration/workflow-task-runtime@9a7be8e6de5cb266e91b56ee92ead0691210d1a6`
新开 `migration/m1d-task-material-reservation`，实现 M1 的第一段 Task-lifetime Material
Reservation：Task create shared-UoW acquisition、完整集合争用、重启后 dispatch guard，以及 A1
typed Action literal 到 Material Authority 的接缝。本轮不实现 release/retry admission、Job
Claim/fencing、ChangeSet、MaterialSource/M2 或 FE allocation。

## 1. public Module 与 durable schema

`unilabos.resources.authority.MaterialModule` 新增：

```python
reserve_task_materials(
    uow,
    *,
    task_uuid,
    root_material_uuids,
) -> MaterialReservationOutcome

has_complete_task_reservation(
    uow,
    *,
    task_uuid,
    root_material_uuids,
) -> bool
```

调用者必须提供 runtime-authority UoW；Workflow 层只持有 public Material capability，不解释
Material SQL。SQLite adapter 新增冻结设计中的两张 singular 表：

- `material_reservation(uuid, workflow_task_uuid, set_fingerprint, status,
  create_time, released_at)`；
- `material_reservation_member(reservation_uuid, material_uuid,
  root_material_uuid, acquired_version, released_at)`。

active Task header 与 active Material member 都由 partial unique index 保护。adapter 在同一个
`BEGIN IMMEDIATE` 内按稳定 UUID 顺序递归展开 composition subtree、去重并计算完整 member set
fingerprint；header 与全部 members 在 savepoint 内全有或全无。same Task/same complete set 返回既有
outcome；different set 为 stable conflict；其他 Task 争用返回未获取 outcome，并把 savepoint
回滚到零 header/零 member。

## 2. Task create 与 typed-root 闭环

production composition 只构造一个 `MaterialModule`/`MaterialResourceSlotResolver`，同时提供
ResourceSlot lookup、Task Reservation 和 runtime guard。Task create 的 transaction 顺序为：

```text
BEGIN IMMEDIATE
  -> Applied Graph / Input / active ExecutionPlan preflight
  -> Material Authority canonicalize concrete ResourceSlots
  -> INSERT WorkflowTask（满足 Reservation FK）
  -> complete Reservation SAVEPOINT
  -> INSERT WorkflowNodeJobs
COMMIT
```

Job INSERT fault 会回滚外层 transaction，因此 Task、Reservation header/member 与 Jobs 只出现
完整 before/after。Reservation contention 只回滚 savepoint；Task 与 Jobs 仍以 `pending` 创建，且
该 Task 不持有任何 ownership row。

root extraction 只读取两类 frozen typed facts：

1. Workflow Input Contract 已解析的 concrete ResourceSlot；
2. active ExecutionPlan Node 对应 frozen target Handle 的
   `meta_data.unilab.value_schema`/allowlist 与 param。

它不扫描字段名、任意 UUID、未声明 param、disabled/out-of-scope Node 或端口 ordinal。A1 target
Handle 上的 concrete literal 先经 Material Authority 规范化，再冻结到 Task workflow snapshot、
ExecutionPlan 和 Job param；caller 不能提交或覆盖 `resource_template_uuid`。WorkflowRuntimeCoordinator
在 `pending -> dispatched` 前从 Task frozen facts 重建相同 roots，并通过 public Material guard
确认 complete active Reservation；缺失、集合漂移、version/disposition 漂移或 authority error 都
fail closed，Job 保持 `pending`。

## 3. 独立测试 provenance

本轮唯一 test-author：`/root/m1_audit`。三次 tests-only tracer 均保留独立分支与提交：

| tracer | 原始 tests-only commit | 候选 commit | 结果 |
|---|---|---|---|
| Task shared-UoW / contention / fault | `e83ce09e928f6cfe813c3cca9a96c76e67422c78` | `80b998f` | base 上 `3 failed`，实现后 GREEN |
| A1 typed Action literal | `fcd994c0bd40336335e33e01e701ea5f93cf573c` | `d55f8ae` | `0fbe531` 上 `1 failed + 3 passed`，修复后 GREEN |
| concurrent clients + composition conflicts | `39d577a802657b51f245580b4e9efa0f3222d153` | `27e1349` | behavior `70ff4bd` 上已 `3 passed`，作为缺失的独立 security evidence 纳入 |

三组 cherry-pick 与原始 tests-only commit 的 stable patch-id 分别一致。最后一组没有为制造 RED
而修改 production：实现已经通过 6 个 Barrier 同步的独立 HTTP clients，以及
ancestor→descendant / descendant→ancestor 双向冲突；本轮只补齐冻结设计 §15.4/§15.5 的
提交级证据。

测试覆盖：

- 单 root、多 root、stable ordering/fingerprint 与 same-Task replay；
- 竞争 Task `201 pending`、零 ownership row、无 Reservation dispatch fail-closed；
- Reservation 在 Job INSERT 前已完整可见，Job fault 时 outer transaction 全回滚；
- A1 Handle schema/allowlist、literal authority canonicalization、caller template override zero-write；
- 6 clients/threads 经 Barrier 同时请求同一 Material，恰好一份 complete Reservation，其余五份
  Task 全部 pending/零 Reservation；
- ancestor-first 完整 subtree/provenance 与两个冲突方向的 zero-partial；
- reopen 后 winner 可 dispatch、所有 loser 保持 pending。

## 4. review finding disposition

本轮唯一 reviewer：`/root/m1_reviewer`，采用 regression/security 视角，同时分别检查 Standards
与 Spec。

第一次审查 `0fbe531` 发现 1B：typed-root extraction 只读取 Workflow Input Contract，A1 target
Handle 的 concrete Node literal 可在零 Reservation 下 dispatch。独立 RED `fcd994c` 精确复现，
`70ff4bd` 改为按 active plan + frozen Handle schema 规范化、冻结和守卫，finding resolved。

第二次审查确认行为修复，但发现 gate 1B：初始测试只有串行 HTTP contention，且没有
ancestor/descendant 双向证据。独立 tests-only `39d577a` 在行为 SHA 上直接 GREEN，并以
`27e1349` 纳入候选；6-client concurrency、完整/零集合、双向 subtree conflict 与 dispatch guard
全部闭合。

reviewer 对 exact candidate `27e1349aab5e9dccd2052dc2b16e1143781f335a` 最终确认：

- Standards：`0B/0NB`；
- Spec：`0B/0NB`；
- 两个 blocking finding 均 resolved；
- shared UoW、lock order、savepoint、replay、FK/partial unique index、typed-root、restart guard 与
  closed errors 未见剩余问题；
- 未越界进入 Claim/release/M2/Registry/ResourceTreeSet；
- 最终结论：`ACCEPT`。

## 5. 最终门禁

固定 behavior SHA：`70ff4bd9ba5808875c8348fcdb00e2686edf4dee`。

固定受测/受审 candidate SHA：`27e1349aab5e9dccd2052dc2b16e1143781f335a`。

| 门禁 | 结果 |
|---|---:|
| M1D 专项 | `7 passed` |
| M1/A1/I1/R1B/composition 受影响集合 | `364 passed, 5 warnings` |
| `pytest -q -rs tests` | `2173 passed, 4 skipped, 44 warnings` |
| reviewer 独立 full suite | `2173 passed, 4 skipped, 43 warnings` |
| changed-files Ruff `E/F/I` | passed |
| changed-files Ruff format | passed，10 files |
| changed production `compileall` | passed |
| `git diff 9a7be8e..27e1349 --check` | passed |
| exact candidate worktree | clean |

四个 skip 是三个需显式环境变量开启的 networking slow tests，以及一个需显式 Phoenix executable
的 integration test。43/44 warning 差异来自既有 optional dependency/TestClient 路径在并发
suite 中是否触发；没有新增 waiver、skip 或 xfail。

## 6. 影响范围与下一 frontier

本轮修改只在 OS：

- `unilabos.resources.authority` 的 Reservation public contract 与 SQLite adapter；
- WorkflowTask preflight/store/composition/runtime 的 typed-root 与 same-UoW 接线；
- M1D 独立合同测试。

没有改变 WorkflowTask HTTP request DTO，没有新增 MaterialSource selector，没有让
Registry/PackageCatalog 拥有 Material 状态，也没有修改 FE、Core submodule pin 或旧
ResourceTreeSet projection。

M1 仍是 active implementation，不能关闭 OS `#6` 或 Core `#155`，也不能添加
`stage:accepted`。下一 M1 round 必须从本轮发布后的最新 integration branch 新开，先用独立 RED
冻结 sole-coordinator Reservation retry/admission reason 与 terminal-safe release，再进入 Job
Claim/fencing；不得在 M1D 分支继续堆叠，也不得提前实现 MaterialSource/M2。
