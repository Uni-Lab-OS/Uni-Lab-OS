# M1C：ResourceSlot durable resolver 趋势报告

日期：2026-08-02

## 1. Round 结论

M1C 已完成 concrete ResourceSlot 的持久解析纵向切片。固定行为候选
`74c8f3c4f812a2952e619fd7aa28e6d176c4f29d` 通过独立 RED、目标/累积/完整测试、
changed-files Ruff/format、compileall、diff check，以及同一独立 reviewer 的 Standards/Spec
精确 SHA 复审。最终结论为 **Standards 0B/0NB、Spec 0B/0NB，ACCEPT**。

本轮接受以下能力：

- `MaterialModule.resolve_resource_slot` 的 public concrete lookup seam；
- authority-owned、不可变的 Material/ResourceTemplate identity，以及稳定的
  `400 invalid_input`、`404 not_found`、`409 conflict` 分类；
- Workflow 侧 `MaterialResourceSlotResolver` anti-corruption adapter；
- production composition 默认把 resolver 装配到唯一 `workflow.db`，并借用 Task create 的同一
  outer UoW；
- canonical ResourceSlot 写入 immutable `WorkflowTask.input`/Job param，失败保持 Task/Job
  zero-write；
- Service 构造前启动失败时，Store 未确认关闭便保留 workspace lease，并允许公开 reset 重试清理。

本轮没有实现 Reservation、Claim/fencing、ChangeSet、MaterialSource/M2 selector、
ResourceTreeSet projection、Material REST/SSE 或 Registry/PackageCatalog owner。M1 与 M2 控制面
均保持开放，不得把本 round 误标为完整 M1 Accepted。

## 2. 基线、角色与 provenance

| 项目 | 值 |
|---|---|
| 初始 M1C 基线 | `integration/workflow-task-runtime@5b33d891e12857d6d5412950ded9eab380d1f254` |
| 并行 A1 集成父 | `12476b098dbedaa824a158ed37e39c2ebd8b5a87` |
| 实现分支 | `migration/m1c-resource-slot-resolver` |
| 控制面 | Core `#155`、OS delivery `#6` |
| 唯一独立 test-author | `/root/m1_audit` |
| 唯一独立 reviewer | `/root/m1_reviewer` |
| 固定行为候选 | `74c8f3c4f812a2952e619fd7aa28e6d176c4f29d` |
| 最终 review | Standards `0B/0NB`；Spec `0B/0NB`；ACCEPT |

M1C 从已接受的 M1B integration 开始。并行 A1 随后先合入本地 integration，因此在最终双审前，
M1C 以非 squash merge `c4c8f7c` 纳入 A1 `12476b0`。唯一冲突位于 production composition；
解法同时保留 A1 的 Registry snapshot/Catalog 原子发布与 cleanup，以及 M1C 的 Material resolver
同库装配。最终 reviewer 的 merge-base 精确确认为 `12476b0`。

tests-first 原提交与 migration 保留提交如下；五组 patch-id 均由最终 reviewer 验证一致：

| 合同批次 | tests-only 原提交 | migration 保留提交 | RED / 说明 |
|---|---|---|---|
| public resolver + Workflow/HTTP tracer | `2ab3575` | `aae49ca` | `14 failed, 2 passed` |
| production adapter/default composition/UoW | `cd1dcb6` | `95a960c` | `8 failed, 16 passed` |
| HTTP success envelope 校正 | `a230161` | `c752f15` | tests-only 对齐既有 `data` envelope |
| empty/canonical-duplicate allowlist | `5f45afc` | `aeaebbb` | `2 failed, 24 passed` |
| pre-Service Store.close/lease retention | `56bcf3c` | `6e42ce5` | `1 failed, 1 passed` |

独立 test-author 始终只修改测试；production 由 implementation owner 完成，reviewer 未参与实现或
测试编写。没有 squash、删除独立断言、skip、xfail 或放宽错误分类。

## 3. 行为与错误边界

```text
POST WorkflowTask
  -> WorkflowStore BEGIN IMMEDIATE
  -> 02H input shape/default/null preflight
  -> MaterialResourceSlotResolver (Workflow-owned port adapter)
  -> MaterialModule.resolve_resource_slot (Material Authority)
  -> SQLiteMaterialAdapter borrows current WorkflowStore UoW
  -> canonical {uuid, resource_template_uuid}
  -> Task snapshot + Job param in the same commit
```

解析只接受 concrete Material UUID。caller 不能提交 `resource_template_uuid` 覆盖真值；返回的
template identity 始终来自 durable Material。allowlist 省略表示 unconstrained；存在时必须是
非空、canonical 后唯一的有效 UUID tuple，并在任何 Material lookup 前完成校验。

| 条件 | 稳定结果 |
|---|---|
| UUID/shape/allowlist 非法、device Material、template mismatch、adapter identity 非法 | `400 invalid_input` |
| Material 不存在或 soft-deleted | `404 not_found` |
| business Material 为 consumed/discarded/quarantined/reconciling | `409 conflict` |
| active business Material 且 template 允许 | authority-owned immutable identity；Task create `201` |

resolver 不创建 Task、Job、Reservation 或 Claim。所有稳定失败均发生在 Task/Job 首次 INSERT 前；
测试同时覆盖真实 SQLite reopen、真实 WorkflowService/HTTP、Task/Job zero-write 与 authority body
不被 caller 覆盖。

## 4. same-UoW、composition 与启动生命周期

`WorkflowStore.current_unit_of_work()` 只提供 runtime-authority transaction capability；
`SQLiteMaterialAdapter` 在同线程 outer transaction 内借用该 UoW，不创建第二 connection、第二
database、第二 lock 或 nested transaction。WorkflowStore 仍不包含 Material SQL，MaterialModule
也不获得 Workflow CRUD ownership。

production `compose_workflow_runtime` 使用同一个局部 Store 依次装配：

1. R1B runtime recovery；
2. A1 Registry snapshot 到 TemplateCatalog 的原子发布（配置时）；
3. `SQLiteMaterialAdapter.from_runtime_authority(store)`；
4. `MaterialResourceSlotResolver(MaterialModule(...))`；
5. WorkflowService、source monitor 与 runtime worker。

M1C resolution 只读取 durable Material identity，因此 composition 不注入或复制
Registry/PackageCatalog template snapshot；template discovery/selection 仍不属于 Material owner。

审计进一步发现：若 adapter 初始化在 Service 构造前失败，且首次 Store.close 也失败，旧代码会
释放 lease 并丢失 close failure cause。`74c8f3c` 增加独立 retained startup Store：同进程后续
compose fail closed，外进程仍被 flock 拒绝；公开 reset 只有确认 Store 关闭后才释放 lease，
同时保留 startup error 以 Store.close error 为 cause。既有 Service/monitor/worker close-retry 路径
保持不变。

## 5. 最终门禁与 review

固定行为 SHA：`74c8f3c4f812a2952e619fd7aa28e6d176c4f29d`。

| 门禁 | 结果 |
|---|---:|
| M1C 专项（resolver + startup lifecycle） | `28 passed` |
| M1A/M1B/M1C + 02H/A1 + Backend/R1B 受影响累计 | `378 passed` |
| production composition 生命周期集合 | `356 passed, 35 warnings` |
| `pytest -q -rs tests` | `2166 passed, 4 skipped, 43 warnings` |
| changed-files Ruff `E/F/I` | passed |
| changed-files Ruff format | passed，8 files |
| changed production `compileall` | passed |
| `git diff 12476b0..74c8f3c --check` | passed |
| exact worktree | clean |

四个 skip 是三个需显式环境变量开启的 networking slow tests，以及一个需显式 Phoenix
executable 的 integration test。43 个 warning 来自既有 TestClient、pytest collection、
optional dependency 与 FastAPI lifespan deprecation；本轮没有新增 waiver。

同一独立 reviewer 对 exact SHA 确认：

- Standards：`0B/0NB`；
- Spec：`0B/0NB`；
- public error boundary、authority identity、same-store/UoW、zero-write/reopen、A1 composition
  共存和 pre-Service lease retention 均闭合；
- 五组原始 RED 与 migration 测试提交 patch-id 一致；
- 未越界进入 Reservation、Claim、M2 或 Registry owner。

## 6. 影响范围与下一 round

M1C 将 02H 已冻结但 fail-closed 的 ResourceSlot port 接到了真实 Material Authority，因此后续
Task Reservation 可以从 typed、canonical Task input 推导 concrete Material roots。它不改变
WorkflowTask request DTO，不改变 FE selector 协议，也不要求 PackageCatalog/Registry 承担
Material 状态或分配决策。

下一 M1 round 应从发布后的最新 `integration/workflow-task-runtime` 新开，进入 Task-owned、
all-or-none Reservation 与 Task create shared-UoW contention。必须先用独立 RED 冻结：complete
member set、contention 时 Task pending/零 Reservation、same-request replay、cancel/terminal release
前置条件和 crash/reopen；不得在 M1C 分支继续堆叠，也不得提前实现 Claim 或 M2 selector。

发布后继续保持：

- OS `#6` 与 Core `#155` open、`stage:implementation`；
- Core Wayfinder 的 M1 frontier 前移到 Reservation round；
- Core submodule pin 不变，直到完整跨仓 integration gate；
- 不创建 `stage:accepted` 或关闭 M1/M2 控制面。
