# M1EF Job Claim / ChangeSet / Recovery 实现与 E2E 账本

日期：2026-08-03

状态：implementation candidate；第二轮 review finding 已修复，待同一 reviewer 对最终 exact SHA
复审；未 merge、未 push。

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
| second evidence candidate（same reviewer rejected） | `ddcf32ffff6f37aad3d512e8f13c254892b8dd03` |
| second review repair（production + tests） | `48dd2d3a9ede732b6741fa1527156093acb83daa` |
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

## 3. 独立 review 的修复闭环

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

同一 reviewer 随后对 `ddcf32ffff6f37aad3d512e8f13c254892b8dd03` 再次给出
`REJECT / changes required`。不能把首轮 finding 的关闭误写成候选已接受；第二轮的两个 Spec blocker
和三个 Standards non-blocker 在 `48dd2d3a9ede732b6741fa1527156093acb83daa` 收敛：

| second-review finding | 修复 |
|---|---|
| Material reparent 只沿 composition 检查，可经 Site occupancy 形成组合环 | Material create/reparent 与 Site placement 共用唯一 composition + occupancy 可达性校验；增加 owner → occupant → owner 回归 |
| released Claim + pending Workflow 未进入启动双向恢复 | `start()` 将完整 authority audit 快照交给恢复；包含 released Claim，任何 released + nonterminal 组合在 ready/dispatch 前 `reconciliation_required` |
| acquire command replay 返回旧 `acquired/reserved` 快照 | processed command 只证明命令 identity；重放时重新读取 durable Claim，released 返回 `rejected` 和当前 Claim |
| 两份 cycle validator 已漂移 | 删除 material-only CTE，三类 mutation 只调用同一个组合图 helper |
| 新增领域注释混入英文句子 | 将违规的 Claim corruption 说明改为中文项目文档表达 |
| exact manifest 的 runtime 路径指向上一轮目录 | 最终 source manifest 只引用本轮 exact candidate 自己的 runtime 与四个隔离数据库 |

实现候选保留两次 rejected provenance，不把历史结论改写为通过；最终 exact SHA 必须由同一 reviewer
重新执行 Standards/Spec review。

## 4. 自动测试门

独立 RED 最初准确失败在缺少 public `JobClaimAcquireCommand`。实现后的主要证据：

- 第二轮 blocker 的三个新回归：`3 passed`；
- M1EF 聚焦组合：`30 passed`；
- M1R + D1A + M1EF broad regression：`78 passed`；
- migration crash、resolution、ChangeSet、concurrency 与 C1～C7 扩展均通过；
- 第二轮修复后完整 `pytest -q -rs tests`：`2384 passed, 4 skipped, 68 warnings`；4 skip 均为需显式联网/Phoenix 的既有
  optional test；
- changed production `py_compile`：通过；
- changed files Ruff `E/F/I` 与 format：通过；
- `git diff --check`：通过。

C1～C6、C7-receipt ACK、C7-release ACK deterministic fault tests 每个窗口都执行 close/reopen，
并在收敛后再做一次 restart；
第二次 restart 对测试的完整 `workflow.db + inventory.db` row set 零写入。

## 5. 隔离的真实 `unilab` CLI + SZLab E2E

第二轮修复的 pre-ledger code candidate 证据根：

```text
/home/changjunhan/Uni-Lab-Core/.artifacts/m1ef-claim-recovery-e2e-final-48dd2d3a/
```

启动使用 second-review-fix source `48dd2d3a9ede732b6741fa1527156093acb83daa`、SZLab workspace、
ROS backend、FastAPI、`--edge_scheduler` 与 `--test_mode`。`PYTHONPATH` 精确指向候选 worktree；
`inventory.db`、`workflow.db`、`device_state.db` 和 `workflow_history.db` 全部隔离在 artifact runtime，
没有读取或写入 `~/.unilabos` authority DB。可移植图只保留真实 S09 移液站，并使用
PackageCatalog definition id `szlab_mixer_pipetting_station`；没有增加 Registry/FQID fallback。

真实 HTTP 创建：

- Task `54484ca6-9106-4420-9271-66a1866c972c`；
- Job `4f196f69-820d-4de5-9181-5ab857412eac`；
- device `szlab_mixer_pipetting_station`；
- Action `prepare_liquid_station`；
- Inventory Claim `129c3954-3f7f-5b9c-b4fa-ebe024990efa`，token `1`；
- terminal ChangeSet `af4c3f94-6905-580b-8a6b-2b2282df7dbe`。

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

关键机器证据：`authority.json`、`task-terminal.json`、`task-after-restart.json`、
`task-after-second-restart.json`、`hashes-before-restarts.json`、
`hashes-after-second-restart.json`、`restart-comparison.json`、`concurrent-clients.json` 与三次 native
OS console log。账本提交后还要在最终 exact SHA 上重跑门禁和原生 E2E，并生成只指向该 exact runtime
的最终 manifest，随后才能交给同一 reviewer。

## 6. 停止线

本候选没有实现 M2B selector、R2 ExecutionPlan、普通 Workflow typed claim-set wiring、vendor
device protocol、FE operator resolution route 或 TemplateCatalog/Registry 重构。legacy
`@action(lock_resource)` 仍只是 R2 前 compatibility hint，不参与 durable Claim authority。

独立 review 接受前不得 merge；用户明确授权前不得 push 或发布。
