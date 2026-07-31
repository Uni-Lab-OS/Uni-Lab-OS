# Phase 01A5：Service.close 失败与 Authority 租约修正趋势报告

状态：**blocking 修正与完整门禁通过，等待原模块 reviewer 最终确认。**

本轮基线：`2a28c97ba4c19d2ff77ec826789622393bcd84f9`

实现候选：`f642d357cd62d35f381fac33b607b5239ab53736`

分支：`migration/01a5-close-failure-lease`

唯一 test-author：`/root/01a5_close_failure_tests`

Backend 保持只读。本轮没有前端变更或 FE–OS 联调。

## 1. 独立红测

sqlite close 没有安全、确定的自然失败条件，因此独立测试作者在 composition 所拥有的
`WorkflowService.close` collaborator seam 使用 fail-once 包装：

- 第一次 close 抛固定 RuntimeError；
- 第二次调用真实 close；
- 所有状态通过公开 compose/get/reset 和第二进程 compose 观察；
- 不读取 composition 全局或 Store 私有字段；
- 最终清理路径不会泄漏连接或租约。

源测试提交：`05cd9b0cc8ca973c7743a94ab9bd91fde67f8f95`

主分支引入提交：`9f756eb`

旧候选稳定为 `1 failed`，公开事实是：

- close 异常被传播；
- 原 Service 没有保留；
- 第二进程成功打开工作区；
- retry 没有第二次调用 close。

## 2. 最小实现

Production 只修改 `unilabos/workflow/composition.py`：

1. monitor.stop 必须成功；
2. Service.close 必须成功；
3. 然后才清空组合根；
4. 最后释放工作区租约。

Service.close 抛错时，后续清理语句不执行，原 Service 和租约保留；第二次 reset 会
再次调用 close，成功后完成清理。没有新增状态字段、恢复 marker 或公共 Interface。

## 3. 代码与测试变化

| 范围 | 变动文件 | 新增行 | 删除行 | 净变化 |
|---|---:|---:|---:|---:|
| Production | 1 | 13 | 14 | **-1** |
| Tests | 1 | 116 | 0 | **+116** |

本轮没有删除、skip、xfail 或弱化既有测试。

## 4. 门禁

| 门禁 | 结果 |
|---|---|
| close/monitor/线性化目标合同 | `7 passed` |
| 完整 `tests/workflow` | `428 passed` |
| 完整仓库 `tests/` | `840 passed, 3 skipped` |
| 变更范围 Ruff | 通过 |
| Ruff format | 通过 |
| `git diff --check` | 通过 |

完整通过项从 839 增为 840，正好对应新增合同。3 个 skip 均为既有条件测试。

## 5. 趋势

| 阶段 | Blocking | Non-blocking |
|---|---:|---:|
| 模块修复确认 | 1 | 0 |
| Phase 01A5 实现门禁 | 0 | 0 |

问题继续减少，且连续两个生命周期 blocker 都通过删除错误的无条件 finally 语义关闭；
没有增加恢复状态机。Production 两轮各净减 1 行。

## 6. 下一轮策略

1. 冻结候选 `f642d35`；
2. 新开确认分支，只重新启用原模块 reviewer；
3. 确认 close 异常保留租约、retry 可完成以及正常 reset 未回归；
4. 模块门关闭后，新开最终风险评审分支，只启用一个全新回归/事务/安全 reviewer；
5. 风险 reviewer 全绿后运行最终完整门禁并准备本地合并；
6. OS 合并完成后立即另开前端分支。

## 7. 前端覆盖

本轮没有覆盖前端。Backend 未修改。
