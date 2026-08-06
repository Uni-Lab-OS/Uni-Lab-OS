# Phase 01A：integration 合并趋势报告

日期：2026-07-31

来源候选：`e3e2479`

integration 合并提交：`aa9de3e`

## 1. 合并结论

单工作区 Authority、单 token Apply、纯 SQLite Apply、Draft 线性化、
Catalog → Store 锁序以及 monitor/Service 关闭失败租约保持已经合并到
`integration/workflow-task-runtime`。

合并后验证：

- Catalog lock/guard 目标测试：`4 passed`；
- 正式完整测试集 `pytest tests -q`：`844 passed, 3 skipped`；
- Ruff `E/F/I`：本次迁移涉及的 Python 文件通过；
- `git diff --check`：通过；
- integration 工作树：干净。

本阶段没有修改 Backend，也没有前端实现或 FE-OS 联调。

## 2. 集成规模

相对 integration 合并前的 `8ad51e4`：

| 类型 | 文件数 | 新增行 | 删除行 | 净增 |
|---|---:|---:|---:|---:|
| Production | 10 | 6262 | 35 | 6227 |
| Test | 36 | 15785 | 3 | 15782 |
| 设计、规则与轮次报告 | 31 | 9066 | 13 | 9053 |
| 合计 | 77 | 31113 | 51 | 31062 |

这是 Phase 01 core 与 01A 多轮 lineage 的累计合并规模，不是 01A7 单轮新增；
各实现轮的精确增删已经分别记录在对应趋势报告中。

## 3. 趋势

| 指标 | 最终风险评审前 | integration 合并后 |
|---|---:|---:|
| 已知 Phase 01A blocking | 1 | 0 |
| Standards finding | 1 | 0 |
| Spec finding | 1 | 0 |
| 正式通过测试数 | 841 | 844 |

问题继续收敛。最后三项新增测试只覆盖 guard 进入、冲突退出和提交后退出三个明确
边界，没有产生新的产品状态、HTTP 字段或并发模型。

## 4. 门禁说明与下一步

对整个历史目录执行 Ruff format 会发现 10 个早期或既有文件尚未采用当前
formatter 输出；它们不是最后修复轮的变更，也不影响 Ruff `E/F/I` 和正式测试。
为保持 review locality，本次不在合并提交中夹带大范围机械格式化。

下一阶段从该 integration 提交新开分支。前端暂不启动：产品装配目前仍没有
production Authoring compiler 和 `generate-python` 纯转换 Interface。先完成使
代码/画布单编辑权模式可真实工作的最小 OS Authoring 垂直切片，再在独立 FE 分支
实现并联调；仍不修改 Backend。
