# Phase 01A7：Catalog guard 退出语义最终复审报告

日期：2026-07-31

状态：**Standards 与 Spec 均无 finding，可以合并到
`integration/workflow-task-runtime`。**

评审分支：`review/01a7-catalog-guard-exit-final`

固定比较点：`8f2fe1a0185e71ef36da96eff438344ca3900885`

被审 Production 精确候选：`7afd318e271f92bcacf2be1dc22953ea9f22f2d6`

唯一 reviewer：`/root/01a7_guard_exit_reviewer`

本轮 reviewer 没有修改 Production、Tests、Backend 或前端，只新增本报告。

## 1. 总体结论

Round 01A6 的 guard 退出语义 blocker 已关闭。复核
`8f2fe1a...7afd318` 完整差异后，确认：

- `unilabos/workflow/service.py:1748-1752` 在 Store 之前进入 snapshot；
  guard 工厂或 `__enter__` 失败稳定映射为
  `503 template_catalog_unavailable`，Authority 无变化；
- `unilabos/workflow/service.py:1754-1766` 把 fingerprint 校验异常和 Apply
  body 异常原样传给 cleanup Adapter，并在 cleanup 后继续传播原异常；
- `unilabos/workflow/service.py:1768-1781` 忽略 `__exit__` 的抑制返回值，
  只记录普通 cleanup 异常，因此既不会吞掉 409/Store 回滚异常，也不会把已提交
  Apply 变成失败响应；
- `unilabos/workflow/service.py:217-221` 已把可选
  `CatalogSnapshotProvider` 从主 `AuthoringCompiler` Protocol 拆开；无 snapshot
  的不可变/无状态 Adapter 继续走 fingerprint fallback；
- `tests/workflow/test_phase01a7_catalog_guard_exit_contract.py:159-265`
  覆盖 Catalog 冲突、提交后恢复一致性和 guard 进入失败三个公共合同边界；
- Apply 返回仍是单一 `candidate_hash`，成功 `warnings=[]`；cleanup 日志只进入
  operator logger，没有进入 HTTP warning、错误体或 SSE；
- Round 01A6 的 `Catalog → Store` 锁序、线程清理，以及既有 workspace lease、
  monitor stop、Service close 合同均无回归。

## 2. Standards 轴

**blocking：0；non-blocking：0。**

实现遵守仓库的简体中文注释/日志、Python 3.11 类型提示和 Workflow migration
门禁。手工管理 Context Manager 协议在此不是重复抽象：它精确实现 cleanup-only
Adapter Seam，保留原异常且忽略非法抑制返回值。未发现 Mysterious Name、
Duplicated Code、Feature Envy、Data Clumps、Primitive Obsession、Repeated
Switches、Shotgun Surgery、Divergent Change、Speculative Generality、Message
Chains、Middle Man 或 Refused Bequest。

## 3. Spec 轴

**blocking：0；non-blocking：0。**

设计文件
`docs/developer_guide/workflow_task_runtime_migration/rounds/01a-single-authority-authoring-design.md:116-129`
要求的三种结果均已实现：

1. guard 进入失败在 Store 前返回 503；
2. body 已有冲突或回滚异常时，cleanup 失败只记录诊断，原异常继续传播；
3. Store 已提交后，cleanup 失败不改变成功 JSON 和恢复后的持久状态。

未发现缺失、实现错误或 scope creep。可选 Provider/fallback 的静态 Interface 与
运行时分派保持一致；没有新增 HTTP DTO、持久字段、SSE schema 或客户端 token。

## 4. 测试与只读验证

- 新增 guard 合同、Catalog/Store 锁序、Draft 线性化、lease/monitor/close 与
  Round 14 高风险集：`25 passed`；
- 正式完整测试：`844 passed, 3 skipped, 18 warnings`；
- Catalog/Store 确定性锁序用例独立重复：`50/50 passed`；
- Ruff `E/F/I`：通过；
- Ruff format：通过；
- `git diff --check 8f2fe1a...7afd318`：通过。

额外只读探针确认：

- guard `__exit__` 返回 `True` 时，
  `template_catalog_conflict` 仍以原 409 语义传播；
- Store 事务内先写后抛 `StoreAuthoringConflict` 时，SQLite 写入回滚；即使 guard
  随后抛出 cleanup 异常，外部仍观察到原 `candidate_hash_conflict`，Workflow
  名称保持未修改。

3 个 skip 和 18 个 warning 均为既有项。新增测试没有 skip、xfail、sleep
轮询或残留线程。

## 5. 趋势与下一步

| 指标 | 进入复审 | 复审结束 |
|---|---:|---:|
| guard 异常 blocking | 0 | 0 |
| Catalog/Store 锁序 blocking | 0 | 0 |
| 新增 Standards finding | 0 | 0 |
| 新增 Spec finding | 0 | 0 |
| 正式通过测试数 | 844 | 844 |

问题继续减少且没有扩散：01A6 的并发锁序和 01A7 的 cleanup 异常边界均已闭合。
当前最合适的策略是不再扩大 OS 修正范围，直接将精确候选及其报告提交历史本地合并
到 `integration/workflow-task-runtime`，再在合并提交上运行一次完整门禁。

## 6. 前端与 Backend 覆盖

本轮没有前端实现或 FE-OS 联调，也没有修改 Backend。前端后续必须继续使用独立
FE 分支。
