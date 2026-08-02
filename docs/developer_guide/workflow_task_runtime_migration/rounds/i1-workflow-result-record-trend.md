# I1：Workflow result-record 与 ResourceSlot 透传轮次记录

日期：2026-08-02

实现分支：`migration/i1-workflow-result-record`

integration 基点：`f4bef43c13677a2dc490c082ff4d2cbc24b8684a`

精确受测/受审候选：`6d7bc811b17fcca420322e3a180d8ce434aea2aa`

Wayfinder：Core #154 冻结 I1 合同；Core #157 仍是跨仓 E2E 验收门。

状态：**本 result-record/implicit pass-through 切片通过完整仓库门禁与同一独立
reviewer 的 Standards/Spec `0B/0NB`。本文不将整个 I1 或跨仓 E2E 标记为完成。**

## 1. 本轮交付

- Workflow canonical output 改为具名 result record；Python authoring 接受
  `TypedDict`、`@dataclass(frozen=True)` 与 inline closed return dict，并统一生成
  `TypedDict` 加 closed mapping return。
- 历史 `workflow_output(...)` 只作为兼容输入读取；normalized/generated source 不再
  输出该 helper，形成单向迁移。
- output symbol 使用与 A1/Workflow input 相同的 Catalog identity index 解析到稳定 Handle
  UUID；compile→generate→compile 保持 graph/result-record fixed point。
- 复用 `registry.action_result_schema.parse_action_result_declaration`，不在 Workflow
  authoring 内复制第二套 result annotation parser。
- 每个 ResourceSlot Workflow input 自动获得同名 server-managed output；其 schema 与
  binding 不可伪造、切换或删除。
- 同名显式 output 只有在 input schema 可赋给 output schema 时才能替代 implicit output。
  因而 singular/list 不混用、nullable 不静默收窄、allowlist 沿用
  producer subset-of consumer 的 D-067 规则。
- canonical result-record 只建立 Workflow output contract；本轮不发布
  `WorkflowTask.output`，不实现 Composite、ExecutionPlan 或设备 completion。

## 2. 独立 RED 与提交 provenance

本轮唯一独立 test-author 为 `/root/i1_result_record_red`。所有 tests-only 提交均在
production 之前或 reviewer finding 修复之前独立形成，并以 cherry-pick 保留来源；没有
删除、skip、xfail 或弱化断言。

| 阶段 | 原始 tests-only commit | 实现分支 commit | RED 证据 |
|---|---|---|---|
| canonical result-record fixed point | `7d9170dec7d454607b8206d2ee39c60995ff45d7` | `b4bb6d4` | public compile 以 `invalid_module_scope` 拒绝 canonical `TypedDict` |
| reviewer compatibility/provenance | `018d148ff0546464956023f31b24803415798712` | `e8e7de7` | singular input 被 list output 压掉；scalar output 可伪造 `implicit: true` |
| D-068 readonly pass-through | `ec820fbca13619a55a9346415220c0c27d23b9da` | `3f9e63e` | 删除同名 implicit output/binding 未被 public validator 拒绝 |

Production GREEN 为：

- `d28aae5a5bc55fb534319b7ca62f58ef6c4b546b`：canonical result-record；
- `6d7bc811b17fcca420322e3a180d8ce434aea2aa`：共享 pass-through compatibility 与
  server-managed implicit authority。

## 3. Finding 收敛

本轮唯一独立 reviewer 为 `/root/i1_startup_reviewer`，同时覆盖 Standards 与 Spec，并固定
精确 SHA。

| 评审 SHA | Standards | Spec | 处置 |
|---|---:|---:|---|
| `d28aae5` | `0B/1NB` | `2B/0NB` | 独立 RED 冻结 singular/list 冲突、scalar implicit 伪造及 pass-through 删除；共享 helper 修复；遗留诊断改用 Workflow result 术语 |
| `6d7bc81` | `0B/0NB` | `0B/0NB` | ACCEPT；focused `23 passed`，Ruff 与 diff-check clean |

Reviewer truth table：

| producer input | consumer/same-name output | 结果 |
|---|---|---|
| singular ResourceSlot | list ResourceSlot | reject |
| nullable ResourceSlot | non-null ResourceSlot | reject |
| non-null ResourceSlot | nullable ResourceSlot | accept |
| restricted allowlist S | allowlist T，`S ⊆ T` | accept |
| broad/unconstrained | narrower/restricted | reject |
| server-managed implicit | 非同名、非精确 schema 或非同名 input binding | reject |

## 4. 精确候选门禁

以 `6d7bc811b17fcca420322e3a180d8ce434aea2aa` 为精确候选：

| 门禁 | 结果 |
|---|---|
| result-record + common-validator focused | `21 passed` |
| 独立 reviewer focused/truth-table | `23 passed` |
| 完整 `tests/workflow` | `1333 passed, 13 warnings` |
| 完整 `tests` | `2372 passed, 4 skipped, 70 warnings` |
| changed-files Ruff / Ruff format | passed |
| `git diff --check f4bef43..6d7bc81` | passed |
| exact-SHA Standards/Spec review | `0B/0NB` / `0B/0NB` |

完整仓库测试本次耗时增加来自另一 OS integration 工作树同时执行测试造成的资源竞争；
测试进程仍持续前进并获得明确成功终态，未将截断日志当作通过证据。

## 5. 下一入口与停止线

1. Task preflight 必须消费同一个 `ValidatedWorkflowIO`，移除
   `task_input.py` 对 input contract/binding 的平行解释。
2. FE 在原 `PersistentWorkflowAuthoringPanel` 增加 Candidate I/O authoring 与 Applied
   Task input form，不引入平行工作台或浏览器默认值权威。
3. Core #157 的真实 OS HTTP/SSE browser E2E 覆盖 Apply/reload/Python↔JSON、
   missing/default/null/falsy、ResourceSlot `{uuid}` 与零 partial write 后，I1 才可进入
   跨仓 Accepted 判断。
4. C1 可以消费已冻结的 I1 seam 做独立准备，但不得把本切片误记为 C1/O1 runtime 完成。
