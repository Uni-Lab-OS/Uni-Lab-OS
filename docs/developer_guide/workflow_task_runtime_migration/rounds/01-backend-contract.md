# 01 Backend contract 轮次门禁记录

状态：**候选整理中，禁止合并**。

本轮是在 D-096 确认前已经开始的 legacy-named round。后续轮次不再复用这种
Phase 级分支。

## 1. 轮次身份

| 字段 | 记录 |
|---|---|
| 轮次 | Phase 01 core |
| 实现范围 | 已有 Backend-shaped Workflow/Graph/Task/Job/Authoring contract core 的补测、修复与关闭 |
| 实现分支 | `migration/01-backend-contract` |
| 初始 integration 基点 SHA | `37a86b98a67819a151cc0276fcb0792128b1cee5` |
| 00a 基线合并 SHA | `8ad51e4ff11389d08cdd855ca2963e4a50b2f0a7`；本分支已吸收同一 00a 候选 `1b8b1d972a18b028d0acdea74efb774fed3d8b2c` |
| Production 实现 commit | `34ddedc3a6c15b982920821f2a3904e5ac0f81b9` |
| 对应决策与验收条件 | D-005、D-025、D-033～D-034、D-041、D-058、D-073～D-081、D-096；`01-backend-contract-plan.md` |
| 明确排除项 | 01E Node/Edge admin、01F Task commands、Phase 02 Schema/Authoring production engine |

## 2. 独立测试作者

| 角色 | subagent | worktree | 测试分支 | 测试 commit | 红测命令与结果 |
|---|---|---|---|---|---|
| 合同测试 | `/root/phase01_contract_tests` | `/home/gaojing/.worktrees/uni-lab-os-01-contract` | `test/01-backend-contract-contract` | 源：`c8c24c1`、`2bdea13`、`644a0df`、`7ed572f`；迁移分支：`055a627`、`d6faf0e`、`8b24c0c`、`10e83f3` | 在 `37a86b98` 执行 `python -m pytest -q tests/app/test_phase01_contract_independent.py`：`3 failed`；Graph、WorkflowTask、Workflow-scoped Authoring 公共路由均不存在 |
| 对抗与回归测试 | `/root/phase01_adversarial_tests` | `/home/gaojing/.worktrees/uni-lab-os-01-adversarial` | `test/01-backend-contract-adversarial` | 源：`e42c1b5`、`69ef29d`、`e817544`、`b51adba`、`5a571a6`；迁移分支：`f39f819`、`fb66857`、`9b8fdc0`、`2b67b3b`、`598d266` | 在 `37a86b98` 执行 `python -m pytest -q tests/app/test_phase01_adversarial_independent.py`：`3 failed`；错误仍为裸 FastAPI `422`、Apply 路由缺失、OpenAPI 无全局 SSE seam |

确认：

- [x] 两名测试作者使用独立 worktree；
- [x] 独立测试提交与 production 实现提交可区分；
- [x] 对已有实现，使用测试作者独立设计、回退 production patch 或等效变异验证，
      证明测试会因目标行为缺失而失败；
- [x] 失败不是环境、import 或 fixture 错误；
- [x] 测试 commits 保留作者与提交来源，未 squash；源提交与 cherry-pick
      提交的 stable patch-id 已核对一致；
- [x] 未通过弱化断言、删除、skip 或 xfail 让测试变绿。

## 3. 候选实现与全量测试门

候选 SHA：文档提交后生成；精确 SHA、完整命令输出和评审结论写入
`refs/notes/workflow-migration`，避免在被审提交中自引用。

| 门禁 | 命令 | 结果 |
|---|---|---|
| 轮次独立 tests | `python -m pytest -q tests/app/test_phase01_contract_independent.py tests/app/test_phase01_adversarial_independent.py` | 候选生成后运行并记入 Git note；两位作者已分别在 `adde4f1` 验证 `3 passed` |
| Phase 01 累积 tests | `python -m pytest -q tests/app/test_phase01_contract_independent.py tests/app/test_phase01_adversarial_independent.py tests/app/test_workflow_contract_api.py tests/workflow/test_backend_contract_store.py` | 候选生成后运行并记入 Git note |
| 完整仓库 test suite | `python -m pytest -q -rs tests/` | 候选生成后运行并记入 Git note |
| lint / static checks | `python -m ruff check --select E,F,I --ignore E501 <本轮变更的 Python 文件>` | 候选生成后运行并记入 Git note |
| `git diff --check` | `git diff --check 8ad51e4..HEAD` | 候选生成后运行并记入 Git note |

确认：

- [ ] 所有命令针对同一个候选 SHA；
- [ ] 所有测试与静态检查通过；
- [ ] 没有未登记的 skip、xfail 或 baseline 豁免。

## 4. 独立评审

| 评审维度 | subagent | 评审 SHA | findings | 状态 |
|---|---|---|---|---|
| 决策与冻结 Interface 合规 | `/root/phase01_spec_reviewer` | 精确候选写入 Git note | 待评审 | pending |
| 仓库规范与模块设计 | `/root/phase01_design_reviewer` | 精确候选写入 Git note | 待评审 | pending |
| 回归、事务、恢复、并发与安全 | `/root/phase01_risk_reviewer` | 精确候选写入 Git note | 待评审 | pending |

Finding 状态只能是：

```text
accepted-fixed | rejected-with-evidence | non-blocking-follow-up
```

| Finding | 来源 | 处置 | 修复 commit / 证据 | 复审者 |
|---|---|---|---|---|
| 待评审 | | | | |

确认：

- [ ] 三名评审者均未编写本轮 production 实现；
- [ ] 评审者同时检查了 production diff 和 tests；
- [ ] 所有 blocking finding 已修复并复审；
- [ ] 修复后已重跑受影响测试和完整门禁；
- [ ] 最终 SHA 与通过测试、通过评审的 SHA 一致。

## 5. 合并

| 字段 | 记录 |
|---|---|
| 最终通过门禁 SHA | 通过后写入 Git note |
| integration 合并 commit | 禁止在门禁完成前填写 |
| 合并时间 | |
| 执行者 | |
| push 状态 | 未经明确授权不得 push |

最终确认：

- [ ] 保留 reviewable commits，未 squash 迁移 provenance；
- [ ] 仅在全部测试和评审门关闭后合并；
- [ ] 01E 分支只从本轮已合并后的 integration commit 创建。
