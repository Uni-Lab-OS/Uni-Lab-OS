# Round 02H：WorkflowTask input preflight 趋势与策略报告

日期：2026-08-01

实现分支：`migration/02h-task-input-preflight`

integration 基线：`01380449868ccf334f4da1a28c7f6f946fb540d1`

最终 production/test 候选：`54116e864a6c4710e9853a2ee19753d878ae376f`

Wayfinder：主功能目录 `Uni-Lab-OS/Uni-Lab-Core#133`；关联目录
`Uni-Lab-OS/Uni-Lab-Core#134`；OS delivery `deepmodeling/Uni-Lab-OS#301`。

状态：**02H、Phase 02 累积目标和完整仓库门禁全绿；同一独立 reviewer 最终确认
Standards/Spec 均为 0 blocking、0 non-blocking，允许 non-squash 本地合并。**

## 1. 本轮交付

本轮新增 transport-independent `unilabos/workflow/task_input.py` 深 Module，通过一次
preflight Interface 和一个可注入 `ResourceSlotResolver` port 收口以下规则：

- 从同一 Task transaction 的 persisted Graph snapshot 读取 ordered v1 Input Contract；
- 在任何 Task/Job INSERT 前完成 closed unknown/missing、top-level null-as-omission、default、
  strict type、finite value 和 constraint validation；
- 把完整 canonical resolved input 按合同顺序写入既有 `WorkflowTask.input`；既有
  `workflow_snapshot` 冻结合同、Graph 和 Node bindings，不增加平行 snapshot 字段；
- ResourceSlot 外部值只接受 `{uuid}`，resolver 返回 immutable identity，并冻结
  `{uuid, resource_template_uuid}`；injected resolver 保持 400/404/409 分类；
- production 使用显式 unconfigured adapter：实际 ResourceSlot 一律在零写入前 409
  fail closed，nullable `None` 和空 slot list 不查询 Material；
- 只按真实 target Handle UUID 解析 closed `input_bindings`，把 binding 写入 Task-scoped
  plan/Job `param`，不修改 persisted Node `param`；
- 对 active plan 重新证明 static/Edge/binding provider 互斥、required provider、debug/single
  node scope 和完整 v1 Handle type compatibility；
- Graph validation 与 Task preflight 共用 `declared_handle_type_matches()`，同时检查 binding
  contract schema 与运行值，覆盖 scalar alias、opaque object、ResourceSlot 和 `list[T]`；
- `WorkflowStore` 只消费已经 preflight 的 `PreparedTaskInput`，并在同一 SQLite transaction
  内按 `read graph -> build -> preflight -> Task INSERT -> Job INSERT` 顺序提交。

本轮没有实现 Material/Site authority、production Material lookup、Reservation、Claim、
Disposition、runtime Material projection、Scheduler/设备执行、Frontend 或 Backend 修改。

## 2. 独立 RED 与评审 provenance

全程恰好一个独立 test-author 和一个未参与测试/实现的 reviewer，且任一时刻只运行一个
subagent。测试作者原始 worktree 提交经 cherry-pick 保留作者 provenance；实现者没有删除、
skip、xfail 或弱化独立断言。

| 阶段 | 角色 | 原始提交 / 实现分支提交 | 结果 |
|---|---|---|---|
| 轮次设计冻结 | 主代理 | `d1419fd` | 冻结深 Module、transaction、ResourceSlot 和 provider 停止线 |
| 原始 02H 合同 | `round02h_test` | `22a10f7` / `2c30c9d` | 61 项：`27 passed, 34 failed`，无 collection/fixture 错误 |
| dependency-only fixture 修正 | 同一 test-author | `2218ada` / `d4d8c29` | 只把历史恶意图注入点移到 persisted seam；断言不变 |
| 首个实现候选 | 主代理 | `9717843` | focused 61、Phase 02 540、完整 tests 1762 全绿 |
| 首次双轴评审 | `round02h_review` | `9717843` | 1B：binding/static 未验证 Handle type；1NB：三处计划文本过期 |
| 首批 finding RED | 同一 test-author | `1b9dcaf` / `46c6370` | numeric binding/static mismatch `2 failed`，均错误创建 Task |
| 首批修复 | 主代理 | `5debef1`、`10edb51` | 共享 numeric matcher；关闭 02C/Apply/409 三处过期文本 |
| 第二次复核 | 同一 reviewer | `10edb51` | numeric 已关；B01 收窄为已知 v1 Handle vocabulary 漏项；N01 关闭 |
| vocabulary RED/对照 | 同一 test-author | `b725e5e` / `ec4c438` | 8 个 mismatch RED；6 个合法 alias/ResourceSlot 对照 GREEN |
| 最终修复候选 | 主代理 | `54116e8` | 完整 v1 schema + value compatibility，保留 unknown extension 兼容 |
| 最终复核 | 同一 reviewer | `54116e8` | Standards 0B/0NB；Spec 0B/0NB；允许本地合并 |

## 3. 实现与测试规模

相对 integration 基线到最终 production/test 候选的净变化：

| 类别 | 文件数 | 新增 | 删除 | 净增 |
|---|---:|---:|---:|---:|
| Production | 4 | 607 | 35 | 572 |
| Tests | 1 | 1564 | 0 | 1564 |
| 轮次设计与整体计划 | 5 | 544 | 125 | 419 |
| 合计 | 10 | 2715 | 160 | 2555 |

Production 主要增长集中在 451 行的 `task_input.py` 深 Module；Service/Store 只增加注入、
错误映射和 transaction result 接线。tests/production 新增行比约为 `2.58`，来自真实
Service/HTTP、恶意 persisted fixture、ResourceSlot adapter、snapshot/restart、active scope、
provider/type matrix 和零 partial write，而不是重复 public DTO。

## 4. 最终门禁

精确候选 `54116e864a6c4710e9853a2ee19753d878ae376f`：

```text
02H focused：                              77 passed
Phase 02 累积计划目标：                   556 passed
完整 tests/：                            1778 passed, 3 skipped
独立 reviewer Graph/Authoring/Catalog：   101 passed
修改文件 Ruff E/F/I：                    passed
Ruff format --check：                    passed
compileall：                              passed
git diff --check：                       passed
```

仓库计划目录的 broad Ruff E/F/I 扫描在 integration 基线与候选均为 44 个既存诊断，
候选没有新增；本轮未顺手修改旧 registry/scheduler/legacy 文件。未限定 rule 的 broad Ruff
还会报告既有 `UP*` 现代化债务，不作为本候选回归。

完整 suite 的 3 个 skip 和 33 个 warning 来自既有 TestClient/httpx、pytest class
collection、optional SOCKS 与 FastAPI `on_event` 提示；02H 没有新增 warning 类别。

## 5. Finding 收敛与遗留

首次 review 的一个根因是 provider 只证明“有值”，没有证明“compatible value”。第一次
修复关闭 JSON `number`，reviewer 随后把同一根因追到完整 v1 Handle vocabulary；第二批独立
RED 覆盖所有缺失 alias/collection 后归零：

```text
34 个初始缺行为
  -> 0（首个实现）
  -> 1 个 type-compatibility 根因 + 1 个文档 NB
  -> numeric 关闭、文档 NB 关闭
  -> 同一根因剩 8 个 vocabulary case
  -> 0B / 0NB
```

02H 自身没有遗留 blocking 或 non-blocking。production Material resolver 与 Reservation
仍按既定停止线进入 M1；M1 完成前，非空 ResourceSlot 的 production Task 不可执行。仓库级
44 个既存 Ruff 诊断属于独立维护债务，不扩大 02H 范围。

## 6. OS、前端、联调与下一入口

- OS：02H repository-local delivery 完成；共享 Task wire DTO 未变化；
- Frontend：未修改；后续只在 I1/UI1 对应 delivery entry gate 到达后写前端 implementation
  spec 和真实 OS Playwright gate；
- Backend：未修改、未写入，也没有 Material remote fallback；
- 联调：02H 不单设跨仓接受门；M1/I1/R1 等功能按
  `fe_os_interaction_migration_matrix.md` 和 Core Wayfinder 各自进入；
- 合并：允许把含设计、独立测试、production 与 finding provenance 的完整提交链 non-squash
  本地合入 `integration/workflow-task-runtime`；未经用户授权不 push。
