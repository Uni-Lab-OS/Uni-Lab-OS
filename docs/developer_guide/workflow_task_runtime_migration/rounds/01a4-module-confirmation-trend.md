# Phase 01A4：模块修复确认与 disposition 报告

状态：**原 blocking 已关闭；确认时发现 1 个新的 close 异常 blocking，禁止合并。**

评审分支：`review/01a4-module-confirmation`

被审精确 SHA：`fbe2efa27fccc7bcbff98ae4e5ac1eda02ec9454`

唯一 reviewer：`/root/01a_module_design_reviewer`

Backend 保持只读。本轮没有修改 Production、Tests 或前端。

## 1. 原 blocking disposition

**accepted-with-evidence**

`unilabos/workflow/composition.py:141-157` 在 monitor.stop 抛出时不会进入 Service
close、组合根清理或租约释放。独立合同证明：

- 原 Service 保留；
- 第二进程仍被工作区租约拒绝；
- 阻塞线程退出后可重试 reset；
- 重试成功后租约释放并可重新装配。

原 monitor-stop blocking 已关闭。

## 2. 新 blocking：Service.close 异常仍释放租约

位置：`unilabos/workflow/composition.py:144-157`。

monitor 已停止后，reset 在 `try` 中调用 Service.close，但在 `finally` 中无条件清空
runtime 并释放租约。如果 close 抛错，composition 没有确认 Store 已关闭，却允许新
Authority 取得工作区。

最小修正：

- Service.close 成功后才清空 runtime 和释放租约；
- close 失败时保留 Service 与 lease，允许处理故障后重试 reset；
- 增加 close 首次失败、第二进程仍拒绝、重试成功后可重新装配的合同。

## 3. 两个 non-blocking disposition

### Apply 事务 callback

**accepted-with-evidence**

- 只有一个内部调用者；
- callback 闭包只读取 Draft 与 compiler Catalog；
- 不重入 Store；
- 事务内最终校验是已冻结的线性化合同；
- Draft 全局大小策略不应在本轮另造公开 port。

不需要修改当前 Service/Store seam。

### `/proc/wchan` 竞态同步

**accepted-with-evidence**

- Production 已明确依赖 Linux `fcntl`；
- 当前同步在目标 Linux 环境稳定；
- 没有同样确定且更低耦合的公共 seam；
- 为测试增加 Production hook 会暴露内部事务排序，反而扩大 Interface。

保留该测试，并要求在 Linux CI 运行。

## 4. 趋势

| 阶段 | Blocking | Non-blocking |
|---|---:|---:|
| 模块设计首审 | 1 | 2 |
| monitor 修正门禁 | 0 | 2 |
| 模块修复确认 | 1 | 0 |

原问题和两个判断项已关闭，但相邻的 close 异常路径仍有同一种“未确认停止就释放
Authority”的错误。问题没有扩张到新的模块，仍集中在 composition 生命周期。

本轮为只读评审，Production/Test 变更均为 0。被审 SHA 已通过
`839 passed, 3 skipped`，但没有覆盖 close 异常。

## 5. 下一轮策略

1. 新开 close 异常修正分支；
2. 只启用一个独立 test-author；
3. 优先寻找可通过公开 composition seam 制造的真实 close 失败；
4. 若平台没有自然可控的 sqlite close 失败，只在 composition 所拥有的 Service.close
   collaborator seam 做一次 fail-once 故障注入，并仍通过公开 reset/get/第二进程
   观察结果；
5. 修正后运行完整门禁；
6. 再由原 reviewer 单独确认；
7. 随后进入最终回归/事务/安全评审。

## 6. 前端覆盖

本轮没有前端变更或 FE–OS 联调。Backend 未修改。
