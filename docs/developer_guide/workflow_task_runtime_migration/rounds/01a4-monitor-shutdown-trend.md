# Phase 01A4：monitor 停机与 Authority 租约修正趋势报告

状态：**blocking 修正与本地完整门禁已通过，等待原模块 reviewer 确认。**

本轮基线：`5dc741e8413c5a8881890c01c1cac4ae6e0cc4b5`

实现候选：`fbe2efa27fccc7bcbff98ae4e5ac1eda02ec9454`

分支：`migration/01a4-monitor-shutdown`

唯一 test-author：`/root/01a4_monitor_shutdown_tests`

Backend 保持只读。本轮没有前端变更或 FE–OS 联调。

## 1. 独立红测

独立测试作者通过公开 composition/reset interface 和真实 source monitor 构造：

- 外部 Draft 触发 monitor 重编译；
- 公开注入的 compiler 阻塞 monitor 线程；
- reset 等待期限内 monitor 无法退出；
- 第二进程尝试打开同一 working_dir；
- 释放 compiler 后重试 reset，并再次装配。

测试没有 monkeypatch monitor.stop/join、composition 全局或 Store 私有方法。

源测试提交：`e59d6f2a8c0fb1110bb75c191cec2c7576b41f5a`

主分支引入提交：`d8b97db`

旧候选稳定 RED：

`1 failed`

公开失败事实为：

- `get_workflow_service()` 已不是原 Service；
- 第二进程结果为 `("opened", "")`。

这证明 monitor 未退出时旧 reset 仍清空 runtime、关闭 Store 并释放租约。

## 2. 最小实现

Production 只修改 `unilabos/workflow/composition.py`：

- 先调用 monitor.stop；
- stop 成功后才关闭 Service、清空组合根并释放租约；
- stop 超时抛出时不进入清理 `finally`；
- 原 Service、monitor、database path、owner pid 和 lease descriptor 全部保留；
- compiler 解除阻塞、monitor 真正退出后，第二次 reset 可以完成清理；
- 清理完成后工作区可以重新装配。

没有修改 monitor 循环、Store、Authoring DTO、数据库 schema 或文件写路径。

## 3. 代码与测试变化

相对本轮基线：

| 范围 | 变动文件 | 新增行 | 删除行 | 净变化 |
|---|---:|---:|---:|---:|
| Production | 1 | 15 | 16 | **-1** |
| Tests | 1 | 168 | 0 | **+168** |

测试行数包含真实 monitor、跨进程租约验证和失败后可重试清理。没有删除、skip、xfail
或弱化既有测试。

## 4. 门禁

| 门禁 | 结果 |
|---|---|
| monitor + 线性化目标合同 | `6 passed` |
| 完整 `tests/workflow` | `427 passed` |
| 完整仓库 `tests/` | `839 passed, 3 skipped` |
| 变更范围 Ruff | 通过 |
| Ruff format | 通过 |
| `git diff --check` | 通过 |

完整测试数量从 838 增为 839，正好对应新增合同。3 个 skip 均为既有条件测试。

## 5. 趋势

| 阶段 | Blocking | Non-blocking |
|---|---:|---:|
| 模块设计评审 | 1 | 2 |
| Phase 01A4 实现门禁 | 0 | 2 |

blocking 已从 1 降到 0，没有出现新的持久化、Apply 或进程竞争问题。Production 净减
1 行，说明修复是清理错误的 finally 生命周期，而不是增加另一层恢复状态。

两个 non-blocking 仍待原 reviewer 处置：

1. SQLite writer transaction 内的 callback 命名与有界工作量；
2. `/proc/wchan` 竞态同步的 Linux/SQLite 实现绑定。

## 6. 下一轮策略

1. 冻结候选 `fbe2efa`；
2. 新开确认分支，只重新启用原模块 reviewer；
3. 确认 monitor stop 超时保留 runtime/租约，重试后释放；
4. 对两个 non-blocking 分别给出 accepted、需修正或有证据拒绝的 disposition；
5. 若需修改，另开独立测试/修正 round；
6. 模块门关闭后再进行一个独立回归/事务/安全评审 round；
7. 全部评审与完整门禁通过前不合并、不 push。

## 7. 前端覆盖

本轮没有覆盖前端。Backend 未修改。
