# M2B MaterialSource Admission 趋势报告

日期：2026-08-02

本轮从 OS
`integration/workflow-task-runtime@cf6f81da8bf41950c8779555c60a7b7349184fbe`
新开 `migration/m2b-material-source-admission`，基线已经包含最新 M2A 与 M1R
迁移重构。本轮实现 Task-wide MaterialSource admission；没有合并、push 或修改其他仓库的
integration 分支。

## 1. 冻结规格与用户决策

规格位于
`m2b-material-source-admission-design.md`，并冻结以下决策：

1. Inventory deduction 是 non-goal；M2B 不读取、预留、扣减或消费 `inventory_lot`；
2. Material/Site 候选按 `Site.sort_order ASC, Site.uuid ASC` 参与完整匹配；
3. fixed Material 位置不匹配返回 `rejected/material_location_mismatch`；
4. 可恢复等待使用真实 `WorkflowTask.status=admission_blocked`。

required MaterialSource 集合严格取 Task snapshot 中的 MaterialSource Node 与该 Task
coordinator-owned pending resolution Job 的交集。disabled 或 debug scope 外、没有 resolution
Job 的 Source 不进入 command；空集合沿普通 no-Material dispatch 路径执行。

## 2. 实现结果

### 2.1 Inventory authority

- `InventoryService.admit_task()` 是唯一 Task-wide admission seam；支持 fixed/automatic
  `existing` 与 `create_new`；
- command 在 Inventory 边界重新验证 closed selector，包括精确 `mount={uuid}`、闭合
  `flow_role`、模板、Site scope 与 canonical UUID；
- assignment 按 Source UUID 确定顺序、按 Site business order 搜索，并以完整 Task-wide
  backtracking 保证唯一 Site 与 Material subtree；
- `create_new` 的 Material、Site occupancy/version、单个完整 Reservation、processed result、
  ledger 与 outbox 在同一 Inventory transaction 提交；W0 失败整体回滚；
- fixed location mismatch 是 deterministic reject；Reservation contention、无候选 Material/Site
  是 durable blocked；blocked 可单调升级为 admitted 或 rejected；
- admission/release 不触碰 `inventory_lot`。

### 2.2 Workflow 与 Scheduler

- blocked 投影原子写入 admission projection，并将 Task 置为 `admission_blocked`；resolution Jobs
  保持 pending；
- admitted 将全部 required resolution Jobs 标为 succeeded、写入 canonical ResourceSlot，并使
  `admission_blocked -> pending`；rejected 将 Task/仍 pending 的 resolution Jobs 标为 failed；
- runtime 在 Task-wide admission proof 完整前不 dispatch 普通 Action；没有 required resolution
  Job 时不要求 admission projection；
- startup 先释放 terminal owner，再按 `create_time ASC, uuid ASC` 扫描 `pending` 与
  `admission_blocked` Task；terminal release 后稳定重扫 waiter；
- 外部 Material/Site durable change 在 Inventory commit 后触发同一稳定 sweep；带 admission
  causation 的内部事件不递归重入当前 per-Task saga；composition reset 会解除 listener；
- W1/W2 重放复用稳定 command/result；terminal 路径先完成 admission projection ACK，再 release；
- `pause/resume/step` 在 `admission_blocked` 时 fail closed，`cancel` 保持可用。

## 3. 测试 provenance

本轮唯一独立 test-author：`/root/m2b_red`，独立 worktree/branch 中的原始 tests-only RED 为
`bf585d32a38264120e33a3b1ca8f0357a26f0d06`，迁移分支保留提交为
`cbf10e8e9a1cd8deb5faaee874ec061a230f9530`。生产实现前运行得到 `3 failed`：

- automatic existing + create_new Task-wide admission 尚未实现；
- fixed location mismatch 错误地 blocked；
- blocked Task 仍停留 pending。

独立 RED 没有被删除、弱化、skip 或 xfail，最终全部通过。首轮评审后，tests-only commit
`021e5fb0279e0184d16bf25aa9242c35344f8bc3` 将 M2B acceptance 从 4 个扩展为 13 个，覆盖：

- required Source 空集合/no-Material dispatch；
- Material/Site change wakeup；
- Inventory closed selector rejection；
- create_new W0 rollback、W1/W2 recovery；
- equal Site sort order 的 UUID tie-break；
- blocked -> rejected 的真实 durable W3 Workflow projection。

最终 W3 由 `32d42864a29089b9efb67c5282a1cc611d7143ee` 强化：原 Workflow command
先 blocked；同 Task 获得不同 material-set Reservation；owner terminal release 自动 sweep 后，
原 command 在真实 Inventory 中 durable rejected，并与 outbox sequence、Workflow projection、
Task failed 和 resolution Job failed 一致，不使用 fake result/port。

旧 M1R fixtures 中临时的 `flow_role="sample"` 由
`c91ec29ae993e0da9bdb0c3c1915a4764143208c` 迁移为 M2A 闭合目录成员
`primary_sample`；生产边界没有为旧值放宽。

## 4. 独立评审与 finding disposition

本轮唯一汇总 reviewer：`/root/m2b_review`，按 Standards 与 Spec 两轴检查代码和测试。

第一次评审固定在
`ed0b25cca17fd2fc6b4807b7e687df608b36fded`：Standards `0B/0NB`；Spec
`4B/0NB`，结论 `CHANGES_REQUIRED`：

1. 空 required-job set 仍被 dispatch guard 要求 admitted projection；
2. Material/Site 变化没有唤醒 `admission_blocked` Task；
3. Inventory seam 未拒绝 mount extra key 与非法 flow role；
4. M2B acceptance 缺少 no-Material、wakeup、closed selector、create_new crash windows、
   tie-break 与 W3 覆盖。

tests-only `021e5fb0` 与实现 `33813a6b77e91f1975424a6da7f37fff7d155b07`
关闭前三项功能缺口并补齐验收矩阵；`32d42864` 将 W3 改为真实 durable 链路。复审确认 Spec
`0B/0NB — ACCEPT`，Standards 仅剩 `1NB`：新增英文注释/日志不符合仓库约定。

`2636f128d20db306c56ac4c661038c0e7ed61777` 仅将本轮新增注释、docstring 与日志文字
本地化为简体中文，不改变条件、状态、调用、Interface 或测试断言。同一 reviewer 对该 exact SHA
最终确认：

- Standards：`0B/0NB`；
- Spec：`0B/0NB`；
- 结论：`ACCEPT`。

## 5. 最终门禁

固定 behavior/review candidate SHA：
`2636f128d20db306c56ac4c661038c0e7ed61777`。

| 门禁 | 结果 |
|---|---:|
| M2B acceptance | `13 passed, 1 warning` |
| M2B/M2A/M1R/D1A/R1B/Inventory round-target | `466 passed, 1 warning` |
| 完整 `pytest -q -rs tests` | `2380 passed, 4 skipped, 68 warnings` |
| changed Python Ruff E/F/I（排除基线 E501） | passed，17 files |
| changed Python Ruff format | passed，17 files |
| changed production `compileall` | passed，6 files |
| `git diff cf6f81da..2636f128 --check` | passed |
| exact candidate worktree | clean |

四个 skip 是三个需环境变量显式开启的 networking process tests，以及一个需显式 Phoenix
executable 的 integration test。warnings 是既有 FastAPI/TestClient deprecation、测试类收集、
可选环境与 Pydantic 提示；本轮没有新增测试 waiver。

## 6. 下一 frontier

M2B 结束于 admission 与 Reservation，不实现 Inventory deduction、搬运计划、设备 claim/fencing、
传感器 observation projector 或前端 selector implementation。前端 MaterialSource node 的 LINQ
参考设计在独立 FE branch 中交付，后续按 FE-M2B0（运行兼容/节点语义）与 FE-M2B1（selector
authoring/inspector）实施。
