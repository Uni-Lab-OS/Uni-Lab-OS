# Phase 01A2：Draft Apply 线性化修正趋势与策略报告

状态：**修正实现与本地完整门禁已通过，等待原 Spec reviewer 确认，禁止合并。**

本轮基线：`d20a929511d52da5153193b65417e5965b2a3e25`

设计 SHA：`c99731c`

实现候选：`de5df0461b14f590cd923bbfaeaa62f4aa72ab15`

分支：`migration/01a2-draft-linearization`

Backend 继续保持只读。本轮没有修改 Backend、前端，也没有开展 FE–OS 联调。

## 1. 本轮要关闭的问题

第一轮 Spec 评审证明，旧候选在最后一次读取 Draft 后才等待
`BEGIN IMMEDIATE`。外部 coding-agent、Git 或编辑器可以在等待 SQLite writer
期间替换 Draft，Apply 随后仍返回成功。

旧文字合同把文件系统与 SQLite 描述为同一墙钟提交瞬间一致，但外部编辑器不受 OS
文件锁约束，普通 SQLite 事务无法提供这种跨资源原子性。本轮没有恢复 writeback、
marker 或 generation，而是冻结可实现的线性化顺序：

1. 完成 Candidate、编译和 Catalog 预检；
2. 成功取得 SQLite 写事务；
3. 在任何数据库 mutation 前最后读取实际 Draft 和 Catalog；
4. 以该校验为 Apply 线性化点；
5. 校验前的外部修改必须冲突并回滚；
6. 校验后的外部修改是后续的新 dirty edit，必须保留并投影为 stale；
7. Apply 从不写 Draft。

## 2. 独立红测

本轮严格只启用一个 subagent：

- test-author：`/root/01a2_linearization_tests`；
- 独立分支：`test/01a2-draft-linearization-contract`；
- 源测试 commit：`099a3cd64e618decfcdd7ff88de783b43712c50a`；
- 主分支引入 commit：`d5a8b6a`；
- 只新增
  `tests/workflow/test_phase01a2_draft_linearization_contract.py`；
- 没有修改 Production、Backend、既有测试或设计文档。

测试使用真实临时 Draft、HTTP Apply 和 SQLite `BEGIN IMMEDIATE` writer contention。
公开注入的测试 compiler 只标记请求线程已经完成编译；测试通过 Linux
`/proc/.../wchan` 确认请求实际进入 SQLite busy wait 后，再原子替换 Draft。它没有
monkeypatch `WorkflowService`/`WorkflowStore` 私有方法，也没有用 sleep 猜测窗口。

旧候选连续两次稳定得到：

`1 failed, 3 passed`

RED 的唯一失败为 Apply 实际返回 HTTP `200`，合同要求
`409 draft_hash_conflict`。三个既有 PASS 补齐了：

- 同进程同工作区返回同一个 Authority Service；
- 同进程运行中拒绝切换工作区；
- reset 释放租约后可以重新装配。

## 3. 最小实现

Production 只修改两个文件：

- `WorkflowService.apply_authoring` 保留事务外快速预检，并提供一个只读的最终
  Draft/Catalog 校验闭包；
- `WorkflowStore.apply_authoring_candidate` 在成功取得写事务、复核持久 Candidate
  和 Workflow revision 后，任何 graph/source/event mutation 前调用该校验；
- 校验抛出的既有 `WorkflowConflict` 由事务上下文回滚；
- HTTP Apply 请求仍只有一个 `candidate_hash`；
- 没有新增公共 DTO、错误码、数据库字段、marker 或文件写路径。

实现后独立合同为：

`4 passed`

## 4. 本轮代码与测试趋势

相对本轮基线 `d20a929`：

| 范围 | 变动文件 | 新增行 | 删除行 | 净变化 |
|---|---:|---:|---:|---:|
| Production `unilabos/` | 2 | 24 | 1 | **+23** |
| Tests | 1 | 311 | 0 | **+311** |
| AGENTS/设计 | 2 | 39 | 20 | **+19** |

测试行数显著高于实现，是因为竞态合同包含真实 HTTP、文件原子替换、SQLite writer
竞争和失败后状态证明。它没有把同步细节暴露到 Production 公共 Interface。

上一单 Authority 重设计相对旧 Round 14 候选净删 320 行 Production；本轮增加
23 行后仍为净删 297 行。复杂度没有回到旧 writeback/multi-process 方案的水平。

## 5. 门禁证据

| 门禁 | 结果 |
|---|---|
| 独立 Phase 01A2 合同 | `4 passed` |
| 完整 `tests/workflow` | `425 passed` |
| 完整仓库 `tests/` | `837 passed, 3 skipped` |
| 变更范围 Ruff `E4/E7/E9/F/I/B` | 通过 |
| 变更范围 Ruff format | 通过 |
| `git diff --check` | 通过 |

完整仓库由上一候选的 `833 passed` 增为 `837 passed`，正好对应本轮新增的四个合同。
3 个 skip 均为既有条件测试；本轮没有新增 skip、xfail、删除测试或弱化断言。

## 6. 问题趋势与判断

| 阶段 | Blocking | Non-blocking |
|---|---:|---:|
| 第一轮 Spec 评审 | 1 | 1 |
| Phase 01A2 独立红测 | 1 | 0 |
| Phase 01A2 实现门禁 | 0 | 0 |

本轮没有发现新的问题类别：

- 原 blocking 被精确复现后转绿；
- 原租约覆盖缺口由三个公共合同关闭；
- 单 token、Candidate proof、无 post-commit 文件写入均未改变；
- 旧 writeback、marker、多 Authority 初始化问题没有恢复。

因此趋势是问题继续减少，并从“设计解释”收敛成一处 24 行的内部校验 seam。当前
已知实现 blocker 为 0，但尚缺原 reviewer 的精确 SHA 确认，所以不能判断为可合并。

## 7. 下一轮策略调整

1. 冻结实现候选 `de5df04`。
2. 下一轮新开评审分支，只重新启用原 Spec reviewer
   `/root/01a_contract_reviewer`，确认 blocking 和覆盖缺口已经关闭；不得并发启动
   其他 reviewer。
3. reviewer 必须检查事务内校验发生在所有 mutation 前、异常确实回滚、外部写入
   不被覆盖，以及“线性化后新 edit”仍会投影 stale。
4. 同时把 311 行系统竞态测试的 Linux `/proc` 同步方式列为测试可维护性检查点；
   不得用不稳定 sleep 替换。
5. 原 Spec reviewer 确认后，再分别新开模块设计、回归/安全评审轮；每轮仍只有一个
   subagent。
6. 任何 Production 或 Tests 修改都会产生新候选并使旧精确 SHA 结论失效；顺序
   评审和最终完整门禁全部通过前不合并、不 push。

## 8. 前端覆盖结论

本轮**没有覆盖前端实现**。前端模式按钮、只读投影、diff 接受和 FE–OS 联调继续
等待 OS Authoring 候选完成顺序评审并合并；届时必须另开 FE 分支。Backend 仍不得
修改。
