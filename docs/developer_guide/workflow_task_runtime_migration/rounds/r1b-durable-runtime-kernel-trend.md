# Round R1B：durable runtime kernel 趋势与策略报告

日期：2026-08-01

实现分支：`migration/r1b-durable-runtime-kernel`

integration 基线：`d461b93450dfbaf36957562938ba4df108aabfbf`

最终 production/test 候选：`6cc9390623b21061d31800a36f653e7d82750b62`

Wayfinder：Core active Decision `Uni-Lab-OS/Uni-Lab-Core#150`；OS delivery
`deepmodeling/Uni-Lab-OS#303`。

状态：**R1B 代码、测试和完整仓库门禁全绿；同一独立 reviewer 已关闭首轮五个
Blocking，最终 Standards/Spec 均为 0 blocking、0 non-blocking，允许 non-squash
本地合并。**

## 1. 本轮交付

R1B 在 R1A pending command ingress 之后建立 OS 唯一 durable runtime kernel：

- 新增公开 `WorkflowRuntimeCoordinator`，冻结并校验 Task/Job 状态矩阵；
- pending command 按 `(create_time, uuid)` FIFO 消费，一次提交一条；pause/resume
  改变 control、step 生成内部 durable permit、cancel 原子推进 Task/Jobs/cleanup；
- 新增 append-only `workflow_runtime_journal`，并让每次前端可见 mutation 与现有
  `frontend_event` 在同一 SQLite transaction 提交；
- runtime SSE 只有 `workflow.runtime.changed`，payload 严格为
  `{"workflow_task_uuid":"<uuid>"}`，只负责使 REST projection 失效；
- 新增双键幂等 feedback history、Job latest summary，以及 Backend-shaped
  `GET /api/v1/workflow-node-jobs/{uuid}/feedback` cursor pagination；
- in-flight Job 可以进入 `execution_unknown`，多 unknown 按确定顺序维护可行动
  `attention_reason`，只有显式 reconcile 才能离开；
- startup recovery 把遗留 dispatched/running/intervention/cancel_requested Job 标为
  unknown，绝不重放物理动作；
- production composition 只启动一个 runtime worker，并在 Store 关闭和 workspace
  lease 释放前确认 worker 已停止。

停止线保持不变：本轮没有 DAG readiness/admission、Reservation/Claim、设备 dispatch、
物理结果提交、Task output、Debugger、前端改动、旧 Run 路由或 Workflow WebSocket。

## 2. RED、实现与审查 provenance

| 阶段 | 角色 | 提交 | 结果 |
|---|---|---|---|
| 轮次设计冻结 | 主代理 | `78a5b3fdadde327142ff35be919d88c1f62c84a6` | 冻结状态矩阵、事务/outbox、command、feedback、unknown/recovery、worker 和停止线 |
| 独立 tests-only RED | `r1a_test_author` | 原始 `b7b8e2cfba67056bc684f40c9b709dbd729cf9ec`；cherry-pick `a2c08a5f9586ba2a3822e5576077102438bc1314` | collect 191；191 failed，只有缺 runtime module/route/schema 的预期行为 RED；Ruff/format/diff pass |
| 首个 production 候选 | 主代理 | `e7c370efd28856bcc41dac0e6c95d40e52049918` | 独立 191 项、Workflow 1153 项、完整 tests 2005 项全绿 |
| 首轮精确 SHA 审查 | `r1a_reviewer` | `22a86e4b5d19bf511a8213634e14b05e9c23e29e` | 5B/0NB：时间线性化、failed-stop 证据、cancel 两步状态、query parsing、stale attention |
| finding 回归与修复 | 主代理 | `6cc9390623b21061d31800a36f653e7d82750b62` | 新增 9 项回归；focused 增至 200；五个 finding 全部关闭 |
| 同一 reviewer 复核 | `r1a_reviewer` | `6cc9390623b21061d31800a36f653e7d82750b62` | Standards 0B/0NB；Spec 0B/0NB；允许合并 |

独立 tests-only commit 的 patch provenance 保持不变。finding 修复只向独立测试文件
增加 275 行、删除 0 行；没有删除、skip、xfail 或弱化原测试。

## 3. 首轮 finding 收敛

1. **事务时间线性化**：所有七类 mutation 的 `utc_now()` 移到取得 Store
   transaction 之后；强制交错线程测试证明 journal sequence、`create_time` 和最终
   `update_time` 不因排队顺序倒退。
2. **worker failed-stop**：新增跨进程 composition 测试，证明 worker 未退出时原
   Service/Store 仍可用、workspace lease 仍拒绝第二 Authority；重试停机成功后才可
   replacement。
3. **cancel 两步状态**：running Task 即使同事务直接得到最终 canceled projection，
   journal 仍保存 `running→canceling→canceled`；存在 active Job 时停在 canceling；
   非 reconciliation control 固定 paused。
4. **feedback query parity**：Handler 读取原始 query string，镜像 Backend trim、空值
   fallback、十进制 int64/positive limit；`1.0` 和 overflow 在 Job lookup 前返回
   400。
5. **剩余 unknown attention**：partial reconcile 后按 topological/create/UUID 顺序
   选择一个 remaining uncertainty reason；最后一个 unknown 解决时才清空。

## 4. 实现与测试规模

相对 integration 基线到最终 production/test 候选的净变化：

| 类别 | 文件数 | 新增 | 删除 |
|---|---:|---:|---:|
| Production | 5 | 1295 | 3 |
| Tests | 1 | 1319 | 0 |
| 轮次设计 | 1 | 319 | 0 |
| 合计 | 7 | 2933 | 3 |

Production 模块仍以既有 `WorkflowStore` SQLite authority 和
`ProductionWorkflowComposition` 为根；新增 `runtime.py` 是唯一领域 mutation seam，
没有建立平行 scheduler、event store 或 frontend truth。

## 5. 最终门禁

精确候选 `6cc9390623b21061d31800a36f653e7d82750b62`：

```text
R1B focused：                 200 passed
Workflow 全集：              1162 passed
完整 tests/：               2014 passed, 3 skipped
修改文件 Ruff E/F/I：       passed
Ruff format --check：       passed
修改文件 compileall：       passed
git diff --check：          passed
独立 reviewer：             0 blocking, 0 non-blocking
```

完整 suite 的 3 个 skip 和 35 个 warning 来自既有 TestClient/httpx、pytest class
collection、optional SOCKS 与 FastAPI `on_event` 提示；本轮没有新增 warning 类别。

全目录 `python -m compileall -q unilabos` 仍会命中基线已有的
`unilabos/devices/cytomat/cytomat.py:4` 未闭合括号。该 proprietary device driver 不在
本轮授权范围；正式静态门对所有修改 Python 文件执行并通过，完整 pytest 同时覆盖了
可收集的全仓模块。

## 6. E2E 与可视证据

R1B 没有浏览器 UI 或可见页面，因此没有可诚实提供的 UI 截图，也不为满足数量要求
伪造截图。以下六组真实 API/SSE/SQLite/process E2E 证据替代视觉 artifact：

| 证据 | 真实边界 | 自动化覆盖 |
|---|---|---|
| E2E-1 command 与状态 | Service 创建真实 Task/Jobs/commands，Coordinator 在真实 SQLite 事务消费 | FIFO/replay、terminal race、pause/resume、step permit、cancel 多 Job 与两步 Task journal |
| E2E-2 feedback REST/reopen | 写入真实 SQLite，关闭并 reopen Store，再经 FastAPI TestClient GET | batch/idempotency/conflict、latest summary、cursor/limit、Backend error envelope |
| E2E-3 durable SSE | 真实 ASGI `/api/v1/events`，使用 `Last-Event-ID` | exact event/payload、cursor replay、同事务 outbox rollback、无 patch |
| E2E-4 restart recovery | 先持久化 in-flight Job/permit/pending command，再执行 startup recovery/reopen | unknown fence、no blind replay、一次/Task invalidation、二次恢复 zero-write |
| E2E-5 process authority | production composition + spawn 第二解释器 | worker failed-stop 时 Store/Service/lease 保留，第二 Authority 被拒，重试后 replacement |
| E2E-6 repository regression | `/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q tests/` | 2014 passed、3 skipped，覆盖既有 Edge Scheduler、HostLink、Inventory 与 action policy |

对应可执行证据位于
`tests/workflow/test_r1b_durable_runtime_kernel.py`；focused 200 项没有 mock Store、没有
设备 fake，也没有旧 Run/Task-scoped event fallback。

## 7. Interface 迁移与下一入口

本轮同步更新 `fe_os_interaction_migration_matrix.md`：

- OS 已拥有 command ingress + consumption、Task/Job mutation、feedback REST、runtime
  SSE invalidation 和 restart fence；
- FE-D117 已完成的是 Authoring 单写权威，不是 Runtime Controller；
- UI1 Runtime 仍需实现严格 services、`WorkflowTaskController`、command、feedback
  补读和 SSE coherent rehydration；
- 只有 OS/FE 候选 full SHA 都冻结后，Core #150 才写 HTTP/SSE integration spec 并
  进入 `stage:testing`；
- R2 才消费 step permit 并拥有 DAG readiness/admission；D1 才调用设备并提交物理
  result。R1B 本身不是一个可执行 Workflow 闭环。

本轮允许 non-squash 本地合入 `integration/workflow-task-runtime`，未经用户授权不
push。实际 local merge SHA 和 Wayfinder comment 记录在 OS #303。
