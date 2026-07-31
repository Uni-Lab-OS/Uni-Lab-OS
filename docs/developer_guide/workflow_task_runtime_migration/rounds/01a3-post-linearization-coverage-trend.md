# Phase 01A3：线性化后外部编辑覆盖补强趋势与策略报告

状态：**覆盖缺口已关闭，Production 最终无变化，等待模块设计与风险评审。**

本轮基线：`7ef739c9bd8aa4fff7e8beaa6cbb8cae54b805b7`

本轮候选：`0b36da2`

分支：`migration/01a3-post-linearization-coverage`

唯一 test-author：`/root/01a3_stale_projection_tests`

Backend 保持只读。本轮没有前端变更，也没有开展 FE–OS 联调。

## 1. 独立覆盖补强

独立测试作者只通过 HTTP Draft PUT、Apply、Authoring GET、真实 package Draft 和
运行中的 source monitor 添加一个垂直合同：

1. 保存并 Apply 已物化 Candidate；
2. Apply 成功后外部原子替换 Draft；
3. OS 不覆盖外部 bytes；
4. Applied Source 与 Workflow revision 继续保留；
5. GET 生成绑定当前 Draft 的新 source-only Candidate；
6. 顶层状态使用冻结 D-077 分类。

源测试提交：

- 初始提交：`1282c56bb92ddec3b2b9d1fec55d396bf26bd6e5`；
- 状态合同纠正：`47532146c81b8fa60278c8967f714d945b3f7bf5`；
- 主分支对应提交：`cd0681d`、`0b36da2`。

测试作者没有修改 Production、Backend 或既有测试。

## 2. 完整门禁发现的合同冲突

初始新增测试要求顶层 `state=applied_source_stale`。一次最小 Production 优先级
调整使新增测试转绿，整个 `tests/workflow` 也通过，但完整仓库测试发现既有公共
合同失败：

`tests/app/test_workflow_contract_api.py::test_deleted_source_does_not_delete_applied_workflow`

冻结 D-077 的含义是：

- 当前 Draft 有完整、有效、current 的 source-only Candidate 时，顶层状态为
  `unapplied_source_only`；
- `applied_source_stale` 用于没有 current Candidate 可供应用的源码/保存图不一致；
- 一个 aggregate 仍可同时携带旧 Applied Source 与当前 Candidate，前端不得从多个
  资源自行推导另一个顶层状态。

因此初始新测试把“Applied Source hash 已旧”错误等同于唯一顶层 state。由原测试
作者本人按 `decisions.md` D-077 和既有公共合同纠正，而不是删除测试或减少行为
断言。

纠正后的测试同时证明：

- `state=unapplied_source_only`；
- Applied Source hash 与当前 Draft hash 不同；
- Candidate Draft hash 与当前 Draft hash 相同；
- revision 保持 Apply 后的 2；
- 外部 Draft bytes 未被覆盖。

尝试的 Production 状态优先级提交 `0fe2465` 已由 `bc74574` 完整 revert。最终树没有
该 Production 变化。

## 3. 本轮变化

相对基线：

| 范围 | 变动文件 | 新增行 | 删除行 | 净变化 |
|---|---:|---:|---:|---:|
| Production | 0 | 0 | 0 | 0 |
| Tests | 1 | 84 | 4 | **+80** |

本轮没有删除、skip、xfail 或弱化任何既有测试。新增测试从 reviewer 指出的覆盖缺口
出发，最终与冻结状态分类和既有合同一致。

## 4. 门禁证据

| 门禁 | 结果 |
|---|---|
| Phase 01A2/01A3 目标合同 | `5 passed` |
| 完整 `tests/workflow` | `426 passed` |
| 完整仓库 `tests/` | `838 passed, 3 skipped` |
| 新测试/相关 Production Ruff | 通过 |
| Ruff format | 通过 |
| `git diff --check` | 通过 |

3 个 skip 均为既有条件测试；本轮没有新增 skip 或 xfail。

## 5. 趋势与策略调整

| 阶段 | Blocking | Non-blocking |
|---|---:|---:|
| Spec 修复确认 | 0 | 1 |
| 初始覆盖测试 | 0 | 1 个状态分类冲突 |
| 完整门禁与纠正后 | 0 | 0 |

问题继续减少。完整仓库测试在 workflow 目标域全绿后仍发现跨目录公共合同冲突，证明
完整门禁有实际价值。最终没有为了一个新测试改变冻结状态分类，也没有制造场景特判。

下一步：

1. 冻结候选 `0b36da2`；
2. 下一 round 新开分支，只启动一个模块设计/Standards reviewer；
3. 重点检查 Store 回调 seam 的深度、锁内外工作量、异常传播和 391 行线性化合同测试
   的可维护性；
4. 再下一 round 只启动一个回归/事务/安全 reviewer；
5. 所有 blocking 关闭并重跑完整门禁后才合并 OS；
6. OS 合并后另开前端分支实现模式切换和 FE–OS 联调。

## 6. 前端覆盖

本轮没有覆盖前端。Backend 未修改。
