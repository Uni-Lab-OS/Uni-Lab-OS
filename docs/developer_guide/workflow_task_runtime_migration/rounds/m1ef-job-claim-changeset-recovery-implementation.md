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

## 3. 自动测试门

独立 RED 最初准确失败在缺少 public `JobClaimAcquireCommand`。实现后的主要证据：

- M1EF + D1A 聚焦组合：`36 passed`；
- migration crash、resolution、ChangeSet、concurrency 与 C1～C7 扩展后均通过；
- `tests/app tests/workflow`：`1746 passed`；
- 完整 `pytest -q -rs tests`：`2371 passed, 4 skipped`；4 skip 均为需显式联网/Phoenix 的既有
  optional test；
- changed production `py_compile`：通过；
- changed files Ruff `E/F/I` 与 format：通过；
- `git diff --check`：通过。

C1～C7 deterministic fault tests 每个窗口都执行 close/reopen，并在收敛后再做一次 restart；
第二次 restart 对测试的完整 `workflow.db + inventory.db` row set 零写入。

## 4. 真实 `unilab` CLI E2E

证据根：

```text
/home/changjunhan/Uni-Lab-Core/.artifacts/m1ef-claim-recovery-e2e-6QKj9I/
```

启动使用当前候选源码、SZLab workspace、ROS backend、FastAPI、`--edge_scheduler` 与
`--test_mode`。可移植图只保留真实 S09 移液站，并使用 PackageCatalog definition id
`szlab_mixer_pipetting_station`；没有增加 Registry/FQID fallback。

真实 HTTP 创建：

- Task `9e706caf-878d-4de7-bac5-acd4023e4f66`；
- Job `d98fe14d-55e3-4f85-8ab4-598ed99c5f8d`；
- device `szlab_mixer_pipetting_station`；
- Action `prepare_liquid_station`；
- Inventory Claim `6ea32f22-3184-537f-9f51-9ca3d24078a8`，token `1`；
- terminal ChangeSet `a2a04876-e412-5c97-8ae2-6a96dcd4db08`。

SZLab test-mode driver 返回 success，但 frozen A1 output contract 拒绝其模拟结果，因此 Job/Task
终态为 `failed / invalid_device_action_result`。M1EF 仍按安全顺序先提交 outcome=`failed` 的
deterministic no-op ChangeSet receipt，再投影 Workflow terminal，最后以 exact receipt 和 Workflow
fingerprint 释放 Claim；Task `cleanup_status=settled`。这覆盖“physical reality 已确认、typed scalar
result 随后无效”的强制顺序。

随后两次用同一 runtime 目录重新启动原生 OS：

- terminal Task 保持同一 UUID、时间戳和结果；restart 日志没有该 Job 的 dispatch/goal；
- Inventory 的 21 张表全部逐表 hash 不变；
- D1A、Task、Job、feedback、runtime journal、frontend event 和 Material projection 表 hash 不变；
- 只有既有 Registry→TemplateCatalog refresh 的 `workflow_node_template`、
  `workflow_handle_template`、`workflow_source_registration`、`workflow_template_catalog` 变化；它不
  触碰 M1EF authority/saga，归 A1 Catalog lifecycle 单独跟踪，不作为 Claim recovery 写入。

最后，两个独立 Python 进程各自打开同一真实 `inventory.db`，同时争用同一个 S09 device Material：

- 恰好一个 `acquired`，token 从 `1` 单调增加到 `2`；
- 另一个返回 `blocked`，指出同一 blocking Claim，且没有 header/member/fence/outbox partial write；
- winner 从未 dispatch，随后以 evidenced `confirmed_not_dispatched` resolution 和 durable no-send
  proof 释放；最终 authority audit 通过。

关键机器证据：`authority-first.json`、`task-after-restart.json`、
`table-hashes-before-second-restart.txt`、`table-hashes-after-second-restart.txt`、
`concurrent-clients.json` 与三次 native OS console log。

## 5. 停止线

本候选没有实现 M2B selector、R2 ExecutionPlan、普通 Workflow typed claim-set wiring、vendor
device protocol、FE operator resolution route 或 TemplateCatalog/Registry 重构。legacy
`@action(lock_resource)` 仍只是 R2 前 compatibility hint，不参与 durable Claim authority。

独立 review 接受前不得 merge；用户明确授权前不得 push 或发布。
