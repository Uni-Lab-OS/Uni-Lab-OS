# I1：WorkflowTask common-validator 复用轮次记录

日期：2026-08-02

实现分支：`migration/i1-task-common-validator`

integration 基点：`42fa578492fe48779362be6c1257f5d607596870`

精确受测/受审候选：`50da27c2cc2da105082d34e59a5dbbd9a9f43492`

Wayfinder：Core #154 冻结 I1 合同；Core #157 仍是跨仓 E2E 验收门。

状态：**本 Task common-validator 切片通过完整仓库门禁与同一独立 reviewer 的
Standards/Spec `0B/0NB`。本文不把 FE authoring、真实 browser E2E 或整个 I1 标记为
Accepted。**

## 1. 本轮交付

- 新增 Backend-shaped graph adapter `validate_workflow_graph_io()`；它把 persisted/snapshot
  Node 与 Handle identity 投影到同一个 `ValidatedWorkflowIO`，不在 Task Module 复制
  contract、binding 或 assignability validator。
- `preflight_task_input()` 在首次 Task/Job INSERT 前消费公共 canonical input contract 与
  input bindings；删除原 `_validate_graph_bindings` 平行实现。
- input→Action schema assignability、server-managed implicit output 与 output binding
  authority 因而在 Apply 与 Task create 使用同一规则。
- adapter 对 Handle UUID 做 canonical UUID 验证；恶意 persisted Handle 与 binding 即使
  同步伪造，也不能进入 Task/Job snapshot。
- 创建期 Material root 提取直接消费同一次 validated canonical contract，不做第二次
  input-contract parse。
- 已创建 Task 的 immutable historical snapshot 继续通过独立 frozen wrapper 读取当时的
  input contract；旧 snapshot 即使没有后来新增的 D-068 output，也能完成 Material root
  recovery。
- Inventory admission 仍发生在 Task durable create 之后的独立边界；本轮没有把
  Inventory reservation 移入 Task INSERT transaction。

## 2. 独立 RED 与 provenance

本轮唯一独立 test-author 为 `/root/i1_result_record_red`。tests-only 工作树为
`/home/changjunhan/Uni-Lab-Core/.worktrees/uni-lab-os-i1-task-common-validator-red`，所有
tests-only 提交均以 cherry-pick 保留来源，没有 skip、xfail 或弱化断言。

| 阶段 | 原始 tests-only commit | 实现分支 commit | RED / guard |
|---|---|---|---|
| common validator reuse | `fd0c344d097d0a3b45bb37d6adfdb2887c105452` | `b81b085` | Task 错误接受 input `number` → target `number(minimum=10)`；错误接受 scalar `implicit:true`；两者均应 invalid_input/zero write |
| M1R fixture 对齐 | `abdb742cf0f12fbabbb513150453cfce7c89308a` | `f205b26` | ResourceSlot fixture 补 D-068 同名 output/binding；原 admission 断言不变 |
| reviewer Handle identity | `8146791b44bab6b03642c8adf3a7a923333f8a87` | `86003ba` | persisted `not-a-uuid` Handle 可写入 Task/Job；另加 historical snapshot recovery GREEN guard |

Production commits：

- `2038c1dca00c3ce8e4cdec3b803f593a6a21e0e6`：Task preflight 复用公共 I/O；
- `21ab707`：创建期 root extraction 复用已验证 canonical contract；
- `50da27c2cc2da105082d34e59a5dbbd9a9f43492`：Handle canonical identity fail closed。

## 3. Finding 收敛

本轮唯一独立 reviewer 为 `/root/i1_startup_reviewer`，同时覆盖 Standards 与 Spec。

| 评审 SHA | Standards | Spec | 处置 |
|---|---:|---:|---|
| `2038c1d` | `0B/1NB` | `2B/0NB` | 创建期仍二次 parse input contract；Handle UUID 未 canonical validate |
| `50da27c` | `0B/0NB` | `0B/0NB` | ACCEPT；两项 blocker 与生命周期命名均关闭 |

最终 reviewer 复跑：

- Task input + M1R：`82 passed`；
- common I/O validator + D-067 compatibility：`24 passed`；
- Ruff 与 `git diff --check`：passed。

## 4. 精确候选门禁

以 `50da27c2cc2da105082d34e59a5dbbd9a9f43492` 为精确候选：

| 门禁 | 结果 |
|---|---|
| Task input + M1R focused | `82 passed` |
| 完整 `tests/workflow` | `1363 passed, 13 warnings` |
| 完整 `tests` | `2311 passed, 4 skipped, 70 warnings` |
| changed-files Ruff / Ruff format | passed |
| `git diff --check 42fa578..50da27c` | passed |
| exact-SHA Standards/Spec review | `0B/0NB` / `0B/0NB` |

## 5. 下一入口与停止线

1. FE 在原 `PersistentWorkflowAuthoringPanel` 增加 Candidate I/O authoring；所有
   Compile/Generate/Validate/Apply 仍走 OS authoring service。
2. Applied WorkflowTask 启动表单从当前 Applied graph 的 input contract 生成，浏览器不
   自行补默认值；缺失、显式 null 与 falsy value 必须保持区分。
3. ResourceSlot wire value 只提交 closed `{ "uuid": "..." }`；Material identity、类型与
   conflict 仍由 OS/Inventory authority 解析。
4. 真实 OS HTTP/SSE browser E2E 与 Core #157 evidence 完成前，不把 I1 或 C1 标记为
   Accepted。
