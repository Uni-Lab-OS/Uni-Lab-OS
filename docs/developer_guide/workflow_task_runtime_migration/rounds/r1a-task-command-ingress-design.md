# Round R1A：WorkflowTask command 持久入口设计

日期：2026-08-01

实现分支：`migration/r1a-task-command-ingress`

integration 基线：`92f71a14bad8e00b1d8d64136cbd1153d1041395`

Wayfinder：`Uni-Lab-OS/Uni-Lab-Core#150`；OS delivery：
`deepmodeling/Uni-Lab-OS#302`。

## 1. Outcome 与范围

本轮只补齐冻结 Backend 前端合同中的 durable command ingress：

```http
POST /api/v1/workflow-tasks/{task_uuid}/commands
```

请求和响应使用 Backend snake_case identity、JSON envelope 与错误类别。成功只表示一个
`pending` command record 已持久化，不表示 Task/Job 状态或物理执行已经改变。

本轮不消费 command，不实现 runtime coordinator、journal/outbox、
`workflow.runtime.changed`、设备 dispatch、debug Hold、前端或 Backend 修改。

## 2. 冻结 wire contract

请求字段与 Backend `09609a2` 一致：

| 字段 | 规则 |
|---|---|
| `type` | 必须是 `step`、`pause`、`resume`、`cancel` |
| `target_node_uuid` | 可空；只允许用于 `step` |
| `idempotency_key` | trim 后非空，UTF-8 byte length 不超过 255 |
| `description` | 可空，按现有 Workflow optional text 规则规范化 |
| `meta_data` | JSON object；`null` 规范化为 `{}` |

成功返回 `201` 和 `{"code":0,"data":<command>}`。command read DTO 包含冻结
Backend 字段：base record、`workflow_task_uuid`、`type`、可选
`target_node_uuid`、`idempotency_key`、`status="pending"`、`result={}`、
`trace_context={}`，未消费时省略 `consumed_at`。

验证顺序镜像 Backend：

1. 校验并读取 Task；未知 Task 为 `404 not_found`；
2. terminal Task（`succeeded`、`failed`、`canceled`、`timeout`）返回
   `409 invalid_transition`；
3. 校验 command type；
4. `step` 仅适用于 `run_mode=step`，否则 `409 invalid_transition`；
5. 非 `step` 不得携带 `target_node_uuid`；
6. 校验 optional UUID 与 idempotency key；
7. 原子写入 command。

## 3. 持久化与幂等

在 `workflow.db` 增加 `workflow_task_command`，字段、CHECK、外键和索引镜像冻结 Backend
SQLite migration `000020_workflow_task_command`。唯一键是同一 Task 下 active
`(workflow_task_uuid, idempotency_key)`。

- 新 key：写入一个 UUIDv4、`pending` command；
- 相同 key、相同 `type` 与 `target_node_uuid`：返回原 record，不新写；description、
  meta_data 及新 trace context 不改变既有事实；
- 相同 key、不同 `type` 或 `target_node_uuid`：`409 conflict`；
- 以上语义在 Store close/reopen 后不变；
- 所有 validation/conflict 都不得留下 partial command row。

Store 提供本轮所需的最小深接口：创建并处理唯一冲突、按 task/key 读取、按 UUID 读取，
以及稳定 row projection。pending list/consume 留给 R1B，不提前增加未使用 API。

## 4. 模块接缝

- `unilabos/app/workflow_api.py`：Backend-shaped request model 与薄 route；
- `unilabos/workflow/service.py`：Task 状态、run mode、command 形态和幂等规则；
- `unilabos/workflow/store.py`：schema、事务、唯一索引、command row projection；
- `tests/workflow/test_r1a_task_command_ingress.py`：只通过 public Service/HTTP/Store
  seam 观察行为，不查询或 mock 内部 helper。

## 5. RED 与门禁

独立 test-author 在 `test/r1a-task-command-ingress` 和独立 worktree 先提交 tests-only RED，
覆盖：

1. 四种合法 command 的 201/read DTO；
2. 相同 key 重放、不同 command 冲突和 restart persistence；
3. terminal、step-mode、target-only-for-step、UUID、byte-length 与 unknown Task；
4. `null` JSON object、unknown request field、Backend envelope；
5. zero partial write、schema CHECK/unique/foreign-key 和 route body budget。

实现后依次运行 focused、Phase 01/02 Workflow 回归、完整 `pytest tests -q`、修改文件
Ruff E/F/I、format、compileall 和 `git diff --check`。固定最终候选 full SHA 后由唯一独立
reviewer 做 Standards/Spec 双轴审查。

本轮没有浏览器可视界面；阶段报告提供 HTTP、SQLite、restart 和测试日志证据，不生成
无意义 UI 截图。
