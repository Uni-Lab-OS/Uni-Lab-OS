# Phase 01A7：Catalog guard 退出语义修正趋势报告

日期：2026-07-31

分支：`migration/01a7-catalog-guard-exit`

基线：`8f2fe1a0185e71ef36da96eff438344ca3900885`

生产候选：`7afd318`

## 1. 本轮结论

01A6 最终风险评审发现的“Apply 已提交但 guard 退出异常返回裸 500”已经关闭。
Catalog snapshot guard 现在是 cleanup-only Adapter：

- 进入失败仍在 Store 之前返回 `503 template_catalog_unavailable`；
- Apply body 已有冲突或回滚异常时，guard 退出失败只记录日志，不遮蔽原异常；
- Store 已经提交时，guard 退出失败只记录日志，保留成功 JSON envelope；
- guard 的 `__exit__` 返回值不能吞掉 Apply 异常。

可选 snapshot 能力已经从主 `AuthoringCompiler` Protocol 拆成独立的
`CatalogSnapshotProvider`，不可变/无状态 Adapter 的 fingerprint fallback 与静态
Interface 重新一致。

本轮只修改 Uni-Lab-OS，没有修改 Backend，也没有覆盖前端。

## 2. TDD 与验证证据

独立测试作者提交 `13901cd3`，主分支以 `d9bf36f` 引入。旧实现上的 RED 为：

- Catalog 冲突加 guard 退出失败：预期 409，实际裸 500；
- Store 已提交加 guard 退出失败：预期 200，实际裸 500；
- 对照项 guard 进入失败：原实现已正确返回 503。

生产修正 `7afd318` 后：

- guard 退出、锁序、Draft 线性化和 Catalog TOCTOU 目标集：
  `17 passed`；
- 完整 Workflow 测试：`432 passed`；
- 正式完整测试集 `pytest tests -q`：`844 passed, 3 skipped`；
- Ruff `E/F/I`、Ruff format 和 `git diff --check`：全部通过；
- 新测试没有 skip、xfail、sleep 轮询或弱化断言。

## 3. 代码规模

相对本轮基线：

| 类型 | 文件数 | 新增行 | 删除行 | 净增 |
|---|---:|---:|---:|---:|
| Production | 1 | 41 | 19 | 22 |
| Test | 1 | 265 | 0 | 265 |
| 设计与规则文档 | 2 | 23 | 0 | 23 |
| 合计 | 4 | 329 | 19 | 310 |

Production 变化仍局限于 `WorkflowService` 的 Catalog snapshot Adapter，没有修改
Store、路由、DTO、schema、SSE 或 Backend-shaped 模型。

## 4. 问题趋势

| 指标 | 进入本轮 | 本轮结束 |
|---|---:|---:|
| guard 异常 blocking | 1 | 0 |
| Catalog/Store 锁序 blocking | 0 | 0 |
| 新增公共产品合同问题 | 0 | 0 |
| 正式通过测试数 | 841 | 844 |

问题重新下降并保持局部化。本轮新增的测试数量恰好对应进入失败、冲突退出和提交后
退出三个异常边界；修正没有扩散到新持久状态、客户端字段或业务分支。当前剩余
风险只是在独立 reviewer 对精确候选上的确认，不是未决设计。

## 5. 策略调整

下一轮使用唯一一个独立 reviewer，只复查精确候选 `7afd318`：

1. 409、回滚异常和已提交成功是否都不再被 guard 退出异常遮蔽；
2. guard 进入失败是否仍在 Store 前稳定返回 503；
3. cleanup 日志是否不泄漏为公共 warning 或非 JSON 响应；
4. 可选 `CatalogSnapshotProvider` 是否保持 fallback 兼容；
5. 01A6 锁序、单 token Apply、租约和 shutdown 风险是否无回归。

复审通过后立即进入 integration 合并与合并后完整测试。前端仍需等产品
Authoring compiler/transform Interface 可用后，在独立 FE 分支实现。
