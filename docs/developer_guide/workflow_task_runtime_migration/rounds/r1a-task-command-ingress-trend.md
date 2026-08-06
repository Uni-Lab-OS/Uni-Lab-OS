# Round R1A：WorkflowTask command durable ingress 趋势与策略报告

日期：2026-08-01

实现分支：`migration/r1a-task-command-ingress`

integration 基线：`92f71a14bad8e00b1d8d64136cbd1153d1041395`

最终 production/test 候选：`c98ff55df10e90094b159187a06589d813963d1d`

Wayfinder：Core active Decision `Uni-Lab-OS/Uni-Lab-Core#150`；功能目录
`Uni-Lab-OS/Uni-Lab-Core#130`；OS delivery `deepmodeling/Uni-Lab-OS#302`。

状态：**R1A 代码、测试和完整仓库门禁全绿；同一独立 reviewer 最终确认
Standards/Spec 均为 0 blocking、0 non-blocking，允许 non-squash 本地合并。**

## 1. 本轮交付

本轮镜像冻结 Backend `09609a27e652c9e56ede636a2883a4fd241e4400` 的共享
Task command 持久入口：

- 增加 `POST /api/v1/workflow-tasks/{task_uuid}/commands` 和 201 Backend envelope；
- 请求包含 `type`、可选 `target_node_uuid`、`idempotency_key`、可选
  `description` 和 `meta_data`；Handler 在调用 Service 前绑定 UUID；
- 支持 `step`、`pause`、`resume`、`cancel`，保持 terminal Task、step run mode、
  target-only-for-step、idempotency key UTF-8 byte limit 等验证顺序；
- SQLite 增加 `workflow_task_command`，镜像字段、CHECK、Task 外键、active
  `(workflow_task_uuid, idempotency_key)` unique partial index 和 pending index；
- 相同 Task/key 与相同 type/target 返回冻结的原 record；不同 type/target 返回 409；
  重启后重放与冲突语义不变；
- command 只以 `pending` 持久化，不推进 Task/Job，不 dispatch 设备，也不发 SSE。

本轮没有修改 Frontend 或 Backend，没有实现 command consumer、runtime state machine、
journal/outbox、feedback、reconciliation、debug Hold 或旧 `/runtime/runs` 兼容层。

## 2. RED、实现与审查 provenance

| 阶段 | 角色 | 提交 | 结果 |
|---|---|---|---|
| 轮次设计冻结 | 主代理 | `c73530a24c3e76029cf5619d7b1db0331c624168` | 冻结 wire、schema、验证顺序、幂等和 pending-only 停止线 |
| 独立 tests-only RED | `r1a_test_author` | 原始 `78b3cf5898dc3cd75ce122f84f23c6b5c1dc58ad`；cherry-pick `ef35ed50294562c6b4cb1b5ed6626ff4e288c965` | 30 failed；只有缺 route、Service 方法和 SQLite 表的预期行为失败，无 collection/fixture error |
| 首个 production 候选 | 主代理 | `1cc2b0bb792d301659a267eedebaba29c1f6499b` | focused 30、Workflow 961、完整 tests 1813 全绿 |
| 精确 SHA 双轴审查 | `r1a_reviewer` | `1cc2b0bb792d301659a267eedebaba29c1f6499b` | Standards 0B/0NB；Spec 1B：HTTP DTO 未在 Handler seam 绑定 target UUID |
| finding 修复 | 主代理 | `c98ff55df10e90094b159187a06589d813963d1d` | UUID Handler binding；分别固定 handler-first 400 和 service task-first 404，覆盖增至 31 项 |
| 同一 reviewer 复核 | `r1a_reviewer` | `c98ff55df10e90094b159187a06589d813963d1d` | 原 finding 关闭；Standards/Spec 0B/0NB，可合并 |

独立测试的原始提交与 cherry-pick 保留相同 patch provenance。finding 修订纠正了错误的
Backend Handler parity 断言并增加 Service 顺序对照，不是删除、skip、xfail 或弱化覆盖。

## 3. 实现与测试规模

相对 integration 基线到最终 production/test 候选的净变化：

| 类别 | 文件数 | 新增 | 删除 |
|---|---:|---:|---:|
| Production | 3 | 207 | 0 |
| Tests | 1 | 636 | 0 |
| 轮次设计 | 1 | 94 | 0 |
| 合计 | 5 | 937 | 0 |

Production 只扩展既有 `workflow_api.py`、`WorkflowService` 和 `WorkflowStore` seam，
没有建立第二套 runtime authority。测试通过真实 HTTP、Service 和 SQLite 覆盖 wire、验证、
幂等、schema 约束、restart persistence、零 partial write 和 body budget。

## 4. 最终门禁

精确候选 `c98ff55df10e90094b159187a06589d813963d1d`：

```text
R1A focused：                 31 passed
Workflow 全集：              962 passed
完整 tests/：               1814 passed, 3 skipped
修改文件 Ruff E/F/I：       passed
Ruff format --check：       passed
compileall：                 passed
git diff --check：          passed
独立 reviewer：             0 blocking, 0 non-blocking
```

完整 suite 的 3 个 skip 和 35 个 warning 来自既有 TestClient/httpx、pytest class
collection、optional SOCKS 与 FastAPI `on_event` 提示；本轮没有新增 warning 类别。

## 5. E2E 与可视证据

R1A 是 API/Service/SQLite pending ingress，没有浏览器页面或用户可见状态，因此没有可
截图的 UI E2E，也不应为满足数量要求伪造 5 张截图。阶段证据由 31 项真实 HTTP/SQLite
合同测试、962 项 Workflow 回归、1814 项完整仓库测试和精确 SHA 独立审查构成。待
R1B 与 UI1 Runtime 候选就绪后，Core HTTP/SSE integration gate 必须提供真实 OS
Playwright 截图和 artifact。

## 6. Finding 收敛与下一入口

本轮最终无 blocking 或 non-blocking finding。R1A 只证明 command 可被可靠接受和重放：

- R1B 仍需实现 pending command 消费、Task/Job durable transitions、journal/outbox、
  feedback、unknown/reconcile、restart recovery 和 runtime invalidation；
- UI1 Runtime 仍需实现严格 FE service、`WorkflowTaskController`、command 操作与 SSE
  rehydration；
- 只有 OS/FE delivery 候选 full SHA 都冻结后，才写 Core integration spec 并把 Decision
  推进到 `stage:testing`；
- 本轮允许 non-squash 本地合入 `integration/workflow-task-runtime`，未经用户授权不 push。
