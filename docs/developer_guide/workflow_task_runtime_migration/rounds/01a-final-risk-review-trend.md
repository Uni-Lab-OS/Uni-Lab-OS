# Phase 01A：最终回归/事务/安全评审趋势报告

状态：**发现 1 个锁序 blocking，禁止合并。**

评审分支：`review/01a-final-risk`

固定比较点：`2a394737ec7a36f8710e0af27472953451c308bc`

被审精确候选：`f642d357cd62d35f381fac33b607b5239ab53736`

唯一 reviewer：`/root/01a_final_risk_reviewer`

本轮没有修改 Production、Tests、Backend 或前端。

## 1. Blocking：Store → Catalog 锁序反转

位置：

- `unilabos/workflow/store.py:1179-1212`；
- `unilabos/workflow/service.py:1052-1067`。

Apply 在持有 Store 全局 RLock 和 `BEGIN IMMEDIATE` 后执行外部 callback。callback
除了最后读取 Draft，还读取 compiler Catalog fingerprint。

可复现环路：

1. T1 持有 Catalog 锁，随后进入 Store read；
2. T2 Apply 已持有 Store RLock/SQLite writer transaction；
3. T2 在 callback 中等待 Catalog；
4. T1 等待 Store；
5. 两个线程互相等待，整个 Authority 停滞。

reviewer 已用当前候选最小并发探针复现双方阻塞；只有探针 callback 超时后事务回滚，
环路才解除。

此前删除的
`tests/workflow/test_phase01_review_risk_round14_deadlock.py:204`
包含这一仍有效风险。该风险不依赖已废止的 writeback marker 或多 Authority Store
初始化，因此这一部分测试删除过度。

最小建议：

- 事务前取得并保持 Catalog snapshot/guard；
- 全局统一为 `Catalog → Store` 锁序；
- Store transaction 内 callback 只做有界 Draft 校验，不再获取 Catalog 锁；
- 恢复面向当前单 Authority 拓扑的确定性锁反转合同。

## 2. 其余风险结论

reviewer 核查整个 diff、规格、44 个高风险保留/新增测试，未发现以下方面的新问题：

- 单 token Apply DTO、Candidate/Draft/revision/catalog proof；
- 409/422 冲突与事务回滚；
- 事务入口前与线性化后文件 TOCTOU；
- atomic replace、symlink/path containment、missing/deleted/moved Draft；
- 工作区租约、same-process reuse、fork、monitor/close 异常与重试；
- source monitor、SSE 持久事件、Candidate stale/current；
- 新测试清理、skip/xfail。

高风险测试结果：`44 passed`。

## 3. 趋势

| 阶段 | Blocking | Non-blocking |
|---|---:|---:|
| 模块设计最终确认 | 0 | 0 |
| 最终风险评审 | 1 | 0 |

问题数再次从 0 回升到 1，但范围仍是 Apply 线性化 callback 的一个具体锁序，并非
恢复旧 writeback 或多进程 Store 初始化。风险评审发现测试删除范围中混入了一个仍
有效的单进程锁反转合同，说明删除旧复杂度时还需按依赖逐项拆分。

本轮只读，Production/Test 变化均为 0。被审候选已通过
`840 passed, 3 skipped`，但完整套件没有当前锁反转合同，因此不能合并。

## 4. 下一轮策略

1. 新开 Catalog/Store 锁序修正分支；
2. 只启用一个独立 test-author；
3. 从旧 deadlock 测试仅提取仍适用于单 Authority 的锁反转场景，不恢复 writeback
   或多 Authority 行为；
4. 先稳定 RED，再把事务内 callback 收窄为 Draft-only；
5. Catalog proof 在事务前通过明确 snapshot/guard 固定，并统一锁序；
6. 完整门禁后由原风险 reviewer 单独确认；
7. 风险门关闭前不合并、不 push。

## 5. 前端覆盖

本轮没有前端变更或 FE–OS 联调。Backend 未修改。
