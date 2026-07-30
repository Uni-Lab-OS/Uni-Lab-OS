# 迁移轮次门禁记录模板

每个实现轮次复制一份本模板，命名为
`rounds/<round>-<topic>.md`。没有完整记录的轮次不得合并。

## 1. 轮次身份

| 字段 | 记录 |
|---|---|
| 轮次 | |
| 实现范围 | |
| 实现分支 | `migration/<round>-<topic>` |
| integration 基点 SHA | |
| 对应决策与验收条件 | |
| 明确排除项 | |

## 2. 独立测试作者

| 角色 | subagent | worktree | 测试分支 | 测试 commit | 红测命令与结果 |
|---|---|---|---|---|---|
| 合同测试 | | | `test/<round>-contract` | | |
| 对抗与回归测试 | | | `test/<round>-adversarial` | | |

确认：

- [ ] 两名测试作者使用独立 worktree；
- [ ] 测试在 production 实现前提交；
- [ ] 红测因目标行为缺失而失败，不是环境、import 或 fixture 错误；
- [ ] 测试 commits 保留作者与提交来源，未 squash；
- [ ] 未通过弱化断言、删除、skip 或 xfail 让测试变绿。

## 3. 候选实现与全量测试门

候选 SHA：

| 门禁 | 命令 | 结果 |
|---|---|---|
| 轮次 targeted tests | | |
| Phase 累积 tests | | |
| 完整仓库 test suite | | |
| lint / static checks | | |
| `git diff --check` | `git diff --check` | |

确认：

- [ ] 所有命令针对同一个候选 SHA；
- [ ] 所有测试与静态检查通过；
- [ ] 没有未登记的 skip、xfail 或 baseline 豁免。

## 4. 独立评审

| 评审维度 | subagent | 评审 SHA | findings | 状态 |
|---|---|---|---|---|
| 决策与冻结 Interface 合规 | | | | |
| 仓库规范与模块设计 | | | | |
| 回归、事务、恢复、并发与安全 | | | | |

Finding 状态只能是：

```text
accepted-fixed | rejected-with-evidence | non-blocking-follow-up
```

| Finding | 来源 | 处置 | 修复 commit / 证据 | 复审者 |
|---|---|---|---|---|
| | | | | |

确认：

- [ ] 三名评审者均未编写本轮 production 实现；
- [ ] 评审者同时检查了 production diff 和 tests；
- [ ] 所有 blocking finding 已修复并复审；
- [ ] 修复后已重跑受影响测试和完整门禁；
- [ ] 最终 SHA 与通过测试、通过评审的 SHA 一致。

## 5. 合并

| 字段 | 记录 |
|---|---|
| 最终通过门禁 SHA | |
| integration 合并 commit | |
| 合并时间 | |
| 执行者 | |
| push 状态 | 未经明确授权不得 push |

最终确认：

- [ ] 从最新 integration 基点开始；
- [ ] 保留 reviewable commits，未 squash 迁移 provenance；
- [ ] 仅在全部测试和评审门关闭后合并；
- [ ] 下一轮分支只从本轮已合并后的 integration commit 创建。
