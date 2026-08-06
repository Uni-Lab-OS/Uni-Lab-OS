# Phase 01A6：Catalog → Store 最终风险复审趋势报告

状态：**发现 1 个 guard 退出语义 blocking，暂不可进入集成合并。**

评审分支：`review/01a6-catalog-lock-final`

固定比较点：`eb37b0b36d40a72a4a0da00daf1b1931139d833f`

被审 Production 精确候选：`609e34df33ec6d758058df04494b0caef253a489`

报告前分支 HEAD：`fd816c75118cc74ffb9b000a2f920a807ac650fe`

唯一 reviewer：`/root/01a6_catalog_lock_reviewer`

本轮没有修改 Production、Tests、Backend 或前端。

## 1. 总体结论

本轮修复的主要目标已经实现：

- `WorkflowService` 在进入 Store 事务前取得可变 Catalog snapshot guard，并以
  `with` 作用域保持到 Store 调用返回；
- Store 事务 callback 已收窄为 `validate_draft_state`，只读取实际 Draft，不再访问
  Catalog；
- 单 token Apply、Draft 线性化、workspace lease、monitor/reset 和
  `Service.close()` 既有合同均通过回归；
- 缺少 `catalog_snapshot()` 的既有不可变/无状态测试 Adapter 继续使用一次
  fingerprint fallback，完整测试未回归；
- 新并发测试由 `Semaphore` 与 `Event` 建立确定性先后关系，没有使用
  `time.sleep`；同一用例直接重复 50 次均通过，线程均完成清理。

但是，新的 snapshot Adapter 在退出阶段没有闭合异常语义。guard 的 `__exit__`
一旦抛错，可能遮蔽原有业务冲突；如果 Store 已经提交，调用方还会得到裸 HTTP
500，而持久状态其实已经 Apply。该问题会让调用方无法判断是否可以安全重试，因此
必须在合并前关闭。

## 2. Standards 轴

### S-01 — blocking：没有遵守声明的 Context Manager 异常协议

位置：

- `unilabos/workflow/service.py:1731-1759`；
- `unilabos/workflow/service.py:1067-1082`。

`AuthoringCompiler.catalog_snapshot()` 声明返回
`AbstractContextManager[str]`，但 `_catalog_snapshot()` 先用
`ExitStack.enter_context()` 进入，再在生成器 `finally` 中调用
`ExitStack.close()`。`close()` 总是以“无活动异常”退出内部 context，不能把
Apply body 中的 `WorkflowConflict`、Store 错误或回滚异常传给 guard；退出清理
抛出的异常还会替换原异常。

最小探针让 snapshot 返回不同 fingerprint，并在 `finally` 抛出
`RuntimeError`。应传播的 `409 template_catalog_conflict` 被替换为无 machine
code 的 `RuntimeError`，数据库虽然保持未变，但公共错误语义已经丢失。

修复必须明确并测试：

1. guard 进入失败如何映射为 `template_catalog_unavailable`；
2. body 已有 `WorkflowConflict`、Store conflict 或事务回滚异常时，退出失败不能
   遮蔽原异常；
3. body 成功且 Store 已提交后，退出失败不能把已提交 Apply 伪装成失败重试。

### S-02 — non-blocking：Protocol 与 runtime fallback 的可选性表达不一致

位置：

- `unilabos/workflow/service.py:200-205`；
- `unilabos/workflow/service.py:1735-1741`。

`AuthoringCompiler` Protocol 把 `catalog_snapshot()` 声明为所有实现的必选方法，
runtime 却明确支持无该方法的不可变/无状态 Adapter。运行时兼容性已经由
`841 passed` 证明，没有形成当前功能回归；但静态 Interface 无法表达设计中的
“可变实现必选、不可变实现可 fallback”。建议后续拆成窄的可选
snapshot-capable Protocol，或在主 Protocol/文档中明确这一差异，避免真实
Adapter 被错误分类。

Standards 轴合计：1 个 blocking，1 个 non-blocking。

## 3. Spec 轴

### P-01 — blocking：guard 退出失败造成“已提交但返回裸 500”

位置：

- `unilabos/workflow/service.py:1067-1076`；
- `unilabos/workflow/service.py:1756-1759`；
- `unilabos/workflow/store.py:1179-1312`。

违反合同：

- `AGENTS.md:300-310` 要求所有成功/错误使用冻结 JSON envelope；
- `AGENTS.md:845-856` 要求同一事务写入完整 Applied 状态并由 Apply 返回结果；
- `AGENTS.md:959-966` 要求 guard 跨越 Store 事务且冲突语义保持稳定。

可复现证据：

1. 使用真实 `WorkflowService`、`WorkflowStore` 和 source-only Candidate；
2. snapshot guard 正常 yield 正确 fingerprint，Store 正常提交；
3. guard 退出时抛出 `RuntimeError("catalog guard release failed")`；
4. HTTP 响应为 `500 text/plain`，body 为 `Internal Server Error`；
5. 同时数据库中的 `candidate_hash` 已经变为 `NULL`，
   `applied_source` 已经存在。

也就是说，客户端看到失败并重试时会得到 `candidate_not_ready`，但第一次 Apply
实际已经成功。该现象不是普通可重试的 Catalog unavailable，也不能靠 SQLite
rollback 修复，因为退出发生在 Store commit 之后。

进入失败的对照探针表现正确：snapshot `__enter__` 抛出 `OSError` 时返回
`503 template_catalog_unavailable`，且 Authority 状态不变。普通 Draft/Catalog
冲突和事务回滚测试也继续通过。因此 blocker 精确限定在 guard 退出及其与 body
异常的组合，不否定本轮锁序主修复。

Spec 轴未发现 scope creep 或其他缺失项。合计：1 个 blocking，0 个
non-blocking。

## 4. 测试与只读验证

- 高风险目标集：
  `39 passed`；
- 正式完整测试：
  `841 passed, 3 skipped, 19 warnings`；
- 新锁序合同直接重复：
  `50 deterministic repetitions passed`；
- Ruff `E/F/I`：
  通过；
- Ruff format：
  通过；
- `git diff --check eb37b0b...609e34d`：
  通过；
- snapshot 进入失败只读探针：
  `503 template_catalog_unavailable`，Authority 未变化；
- snapshot 退出失败只读探针：
  `500 text/plain`，但 Candidate 已清除且 Applied Source 已提交。

完整测试中的 3 个 skip 与 19 个 warning 均为既有项。新增并发测试没有 skip、
xfail、睡眠轮询或残留线程。

## 5. 问题趋势与策略调整

| 指标 | 进入复审 | 复审结束 |
|---|---:|---:|
| Catalog/Store 锁序 blocking | 0 | 0 |
| guard 异常闭合 blocking | 0 | 1 |
| 新增公共产品合同分支 | 0 | 0 |
| 正式测试数 | 841 passed | 841 passed |

问题没有扩散回多 Authority、writeback 或多 token 设计；新增 blocker 是新
Adapter Seam 的一个确定异常边界。主并发问题已经关闭，但总 blocking 从 0 回升
到 1，因此当前候选仍不可合并。

下一轮应保持范围极小：

1. 先由独立测试作者覆盖 guard exit 遮蔽业务冲突和 post-commit 模糊结果；
2. 明确 snapshot guard 的退出不抛错合同，或在 Service 中保留原业务/已提交结果并
   单独报告 cleanup 故障；
3. 重新验证 Store conflict、rollback、409/503 envelope 与本轮锁序测试；
4. 完整门禁后由独立 reviewer 复核新的精确 SHA；
5. 风险关闭前不合并、不进入前端分支。

## 6. 前端与 Backend 覆盖

本轮没有前端实现或 FE-OS 联调，也没有修改 Backend。Round 01A6 仍是
Uni-Lab-OS 内部 Authoring transaction/Adapter 修正。
