# Phase 01A6：Catalog → Store 锁序修正趋势报告

日期：2026-07-31

分支：`migration/01a6-catalog-store-lock-order`

基线：`eb37b0b36d40a72a4a0da00daf1b1931139d833f`

生产候选：`609e34d`

## 1. 本轮结论

最终风险评审发现的 Catalog/Store 环锁已经关闭。Apply 现在先取得稳定的 Catalog
snapshot guard，再进入 Store 写事务；事务内回调只校验实际 Draft，不再读取或
锁定 Catalog。公共 HTTP Interface 仍只接收一个 `candidate_hash`，没有新增
客户端 token、DTO 或持久状态。

本轮只修改 Uni-Lab-OS，没有修改 Backend，也没有覆盖前端。前端仍须在独立 FE
分支实现编辑模式和 FE-OS 联调。

## 2. TDD 与验证证据

独立测试作者先提交 `94bf028a`，主分支以 `5a7c499` 引入。旧实现上的 RED
确定显示：

- 未调用 `catalog_snapshot()`；
- Store 写事务内再次读取 Catalog fingerprint；
- 持 Catalog 的线程不能在 guard 释放前完成 Store 读取。

生产修正 `609e34d` 完成后：

- 目标锁序、Round 14 风险和 Draft 线性化测试：`14 passed`；
- 完整 Workflow 测试：`429 passed`；
- 正式完整测试集 `pytest tests -q`：`841 passed, 3 skipped`；
- Ruff `E/F/I` 门禁：通过；
- Ruff format：通过；
- `git diff --check`：通过；
- 没有 skip、xfail 或弱化合同来使新增测试转绿。

直接在仓库根运行无路径限制的 `pytest -q` 会额外收集两个既有硬件示例目录：
Modbus 示例在导入期连接 `192.168.3.2:502` 并缺少 `Coil.data_type`，相机示例使用
不可解析的顶层 `cameraUSB` 导入。它们不属于既有正式 `tests/` 门禁，也不经过
本轮变更路径。

## 3. 代码规模

相对本轮基线：

| 类型 | 文件数 | 新增行 | 删除行 | 净增 |
|---|---:|---:|---:|---:|
| Production | 2 | 51 | 15 | 36 |
| Test | 1 | 270 | 0 | 270 |
| 设计与规则文档 | 2 | 39 | 22 | 17 |
| 合计 | 5 | 360 | 37 | 323 |

Production 变更集中在 `WorkflowService` 的 Catalog snapshot Adapter 和
`WorkflowStore` 的 Draft-only callback，两处共同形成一个窄的内部 Seam；没有
扩散到路由、数据库 schema、SSE 或 Backend-shaped 模型。

## 4. 问题趋势

| 指标 | 进入本轮 | 本轮结束 |
|---|---:|---:|
| 已知 blocking | 1 | 0 |
| 新增公共合同问题 | 0 | 0 |
| 新增内部锁序问题 | 0 | 0 |
| 正式测试数 | 840 passed | 841 passed |

问题总量继续下降。本轮没有因为修复而发现新的产品设计分支，也没有引入额外的
Authority、writeback、token 或持久模型；变化只收紧了内部锁序。剩余不确定性从
“实现行为未知”缩小为“独立 reviewer 是否确认精确候选没有遗漏”。

## 5. 策略调整

下一步不继续扩写 OS 功能。使用唯一一个独立风险 reviewer 针对精确生产候选
`609e34d` 复查：

1. Catalog guard 是否始终在 Store 事务之前获取并保持到事务结束；
2. Store callback 是否只读 Draft；
3. snapshot 异常、冲突和事务回滚是否仍保持稳定错误语义；
4. 既有单 token、TOCTOU、租约和 shutdown 风险是否无回归。

复审通过后，先在集成分支合并并复跑正式完整测试；随后才进入独立前端分支。这样
把后续发现限制在真实 FE-OS 契约差异，不再继续扩大 OS 内部并发设计。
