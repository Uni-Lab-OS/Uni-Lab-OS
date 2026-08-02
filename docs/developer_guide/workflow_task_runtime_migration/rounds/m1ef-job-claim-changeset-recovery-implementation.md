# M1EF Job Claim / ChangeSet / Recovery 实现与 E2E 账本

日期：2026-08-03

状态：implementation candidate；待 exact-SHA 独立 Standards/Spec review；未 merge、未 push。

## 1. Provenance

| 项目 | SHA |
|---|---|
| OS `dev` 实施基线 | `90f04339424ac2094a089ee30f9c2bfff6e050de` |
| published spec | `1445072d34521552c4c3b2ae20db531a8f351fef` |
| 独立 tests-only RED | `2c235384e24cca62294a54103349a8bbceb7b9b1` |
| RED cherry-pick | `8a33e1344fe5e8bfe40892af97f5060da16ff580` |
| production + tests implementation | `065210a84ed5eaf2ee5f05b5fadabc621cfa1d8d` |
| first evidence candidate（independent review rejected） | `7b9d1db5b595db28a2c09bd41a146abcce4c07eb` |
| review finding repair（production + tests） | `1c8f8761b5b4d42d84b0556cacc72cafe6d7a70c` |
| latest Backend production reference | `2a3591eaff21d808557e6a645f9092b152fb3504` |

独立 test-author 仅提交 public `InventoryService` 纵向 RED，未参与实现。实现 worktree 为
`migration/m1ef-claim-changeset-recovery`，没有修改原脏 Core 主工作树。

## 2. 已交付能力

1. `inventory.db` exact v6 schema：complete-set `material_claim`、member、monotonic fence
   sequence、resource fence、terminal `material_changeset`/effects；exact v5 才可在一个
   exclusive transaction additive migrate，任何注入崩溃均回滚成完整 v5。
2. public `InventoryService` closed commands：acquire、running、uncertain、ChangeSet commit、
   release 和五种 evidenced resolution；同 command/same payload replay，不同 payload conflict。
3. device、business Material subtree、occupancy-changing Site 的稳定 claim set；business member
   必须被同 Task active Reservation 覆盖；并发冲突 all-or-none、blocked 零 partial write。
4. fenced ChangeSet 支持 Material/Site create、update、reparent、occupancy、soft delete 与 no-op
   receipt；校验 member、attempt/token/version、composition/placement cycle、Site allowlist 与
   occupant identity。
5. `EdgeScheduler` 是唯一 Claim/dispatch/completion coordinator；D1A bridge 只适配 formal
   Task/Job/device/outcome，不直接调用 Inventory mutation。
6. `workflow.db ↔ inventory.db` C1～C7 durable saga：claim projection、execution unknown、
   terminal receipt、Workflow terminal、Claim release、outbox ACK 与 restart replay。
7. 启动前审计 Claim header/member/set fingerprint/fence sequence/resource fence/version/receipt；
   corruption 时在 dispatcher ready 前 fail closed。
8. active Claim 阻止 Task Reservation release；同一 release command 可从 `blocked` 单向推进到
   `released`，不允许换 command 绕过。

## 3. 首轮独立 review 的修复闭环

同一名独立 reviewer 对 `7b9d1db5b595db28a2c09bd41a146abcce4c07eb` 给出
`REJECT / changes required`。所有 blocking finding 都在
`1c8f8761b5b4d42d84b0556cacc72cafe6d7a70c` 修复，并增加对应回归：

| finding | 修复 |
|---|---|
| evidence 未绑定 exact source | 新 E2E 保存 source/tree SHA、原生命令、Python/UniLabOS 版本、SZLab/config/graph hash 与隔离 DB 路径的 `source-manifest.json`；最终候选另存 exact-SHA manifest |
| C7 没有独立 fault window | receipt outbox ACK 与 Claim-release outbox ACK 分成两个独立 hook；C1～C6、C7-receipt、C7-release 分别 close/reopen |
| Claim/ChangeSet mutation 可从通用 HTTP/WS command 进入 | 通用 `execute_command` 只保留 `material.admit/release`；Claim、fence、ChangeSet、resolution 只由 Scheduler 的 typed port 调用 |
| 同 Job 新 attempt 可与旧 live Claim 共存 | transaction 内审计全部历史 attempt；只允许 `max(attempt)+1`、同 Task 且全部旧 Claim 已释放 |
| ChangeSet 可绕过 Claim member baseline version | 提交前完整审计 Claim authority；caller `expected_version` 必须等于 member baseline，当前版本也必须精确匹配 |
| create/reparent Site/Material 不变量不足 | parent/owner/occupant 必须是 live claimed member；统一检查 kind、version、soft-delete、cycle、allowlist 与重复占用 |
| `not_submitted` 混用了 Workflow terminal proof | no-send proof 独立持久化；`not_submitted` 禁止 ChangeSet/Workflow terminal fingerprint，terminal release 则强制二者存在 |
| startup recovery 两库不 fail closed | ready 前双向枚举全部 D1A projection 与 unsettled Claim；owner/task/attempt/token/payload 不一致或 orphan 任一侧均 `reconciliation_required`（503） |
| terminal recovery 只比较 status | 同时精确比较 Job return/error、Task output/error、receipt UUID/fingerprint 与 projection；tamper 保持 Claim live 并拒绝启动 |
| C1+cancel 会遗留 Claim | durable no-dispatch journal 才可经 Scheduler typed resolution 释放；第二次 restart 零写入 |
| C7 顺序与 spec 相反 | ChangeSet → Workflow terminal → Claim release → cleanup projection → receipt ACK → release ACK；两种 ACK 均可独立 replay |

实现候选不把旧 review 结论改写为通过；本账本提交后的 exact SHA 必须由同一 reviewer 重新执行
Standards/Spec review。

## 4. 自动测试门

独立 RED 最初准确失败在缺少 public `JobClaimAcquireCommand`。实现后的主要证据：

- reviewer finding 聚焦复现与 M1EF 组合：`32 passed`；
- v5 migration + M1R + D1A broad regression：`85 passed`；
- migration crash、resolution、ChangeSet、concurrency 与 C1～C7 扩展均通过；
- 修复后完整 `pytest -q -rs tests`：`2382 passed, 4 skipped, 68 warnings`；4 skip 均为需显式联网/Phoenix 的既有
  optional test；
- changed production `py_compile`：通过；
- changed files Ruff `E/F/I` 与 format：通过；
- `git diff --check`：通过。

C1～C6、C7-receipt ACK、C7-release ACK deterministic fault tests 每个窗口都执行 close/reopen，
并在收敛后再做一次 restart；
第二次 restart 对测试的完整 `workflow.db + inventory.db` row set 零写入。

## 5. 隔离的真实 `unilab` CLI + SZLab E2E

证据根：

```text
/home/changjunhan/Uni-Lab-Core/.artifacts/m1ef-claim-recovery-e2e-review-olikUx/
```

启动使用 review-fix source `1c8f8761b5b4d42d84b0556cacc72cafe6d7a70c`、SZLab workspace、
ROS backend、FastAPI、`--edge_scheduler` 与 `--test_mode`。`PYTHONPATH` 精确指向候选 worktree；
`inventory.db`、`workflow.db`、`device_state.db` 和 `workflow_history.db` 全部隔离在 artifact runtime，
没有读取或写入 `~/.unilabos` authority DB。可移植图只保留真实 S09 移液站，并使用
PackageCatalog definition id `szlab_mixer_pipetting_station`；没有增加 Registry/FQID fallback。

真实 HTTP 创建：

- Task `56a8e7db-cd54-4865-99ee-485a01d087f3`；
- Job `3bb1e92e-9dc7-40b0-ad15-192ed9708117`；
- device `szlab_mixer_pipetting_station`；
- Action `prepare_liquid_station`；
- Inventory Claim `db96d171-d63b-53f6-b7b9-4062578ae0a0`，token `1`；
- terminal ChangeSet `7da068ba-85fc-592b-ad78-19881f9ca476`。

SZLab test-mode driver 返回 success，但 frozen A1 output contract 拒绝其模拟结果，因此 Job/Task
终态为 `failed / invalid_device_action_result`。M1EF 仍按安全顺序先提交 outcome=`failed` 的
deterministic no-op ChangeSet receipt，再投影 Workflow terminal，最后以 exact receipt 和 Workflow
fingerprint 释放 Claim；Task `cleanup_status=settled`。这覆盖“physical reality 已确认、typed scalar
result 随后无效”的强制顺序。

随后两次用同一 runtime 目录重新启动原生 OS：

- terminal Task 保持同一 UUID、时间戳和结果；restart 日志没有该 Job 的 dispatch/goal；
- Inventory 的 21 张表全部逐表 hash 不变；
- 排除五张 Registry/TemplateCatalog refresh 表后，D1A、Task、Job、feedback、runtime journal、
  frontend event 和 Material projection 等 15 张 Workflow runtime 表全部逐表 hash 不变；
- `workflow_history.db` 不变；`device_state.db.device_property_latest` 随设备重建刷新，这是设备状态
  观测层的预期启动写入，不是 M1EF authority/saga 写入；
- 两次 restart 日志对原 Job 的 `start now`、`goal sent`、`模拟执行` 均为 `0`。

最后，两个独立 Python 进程各自打开同一个隔离、由 public ResourceTreeSet bootstrap 生成的
`contention-runtime/inventory.db`，同时争用同一个 S09 device Material：

- 恰好一个 `acquired`，全新 authority 分配 token `1`；
- 另一个返回 `blocked`，指出同一 blocking Claim，且没有 header/member/fence/outbox partial write；
- winner 从未 dispatch，随后以 evidenced `confirmed_not_dispatched` resolution 和 durable no-send
  proof 释放；最终 authority audit 通过。

关键机器证据：`source-manifest.json`、`authority-first.json`、`task-after-restart.json`、
`hashes-before-restart.json`、`hashes-after-second-restart.json`、
`restart-hash-comparison.json`、`concurrent-clients.json` 与三次 native OS console log。

## 6. 停止线

本候选没有实现 M2B selector、R2 ExecutionPlan、普通 Workflow typed claim-set wiring、vendor
device protocol、FE operator resolution route 或 TemplateCatalog/Registry 重构。legacy
`@action(lock_resource)` 仍只是 R2 前 compatibility hint，不参与 durable Claim authority。

独立 review 接受前不得 merge；用户明确授权前不得 push 或发布。
