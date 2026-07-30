# 旧版 FE–OS 交互迁移矩阵

## 状态与范围

本文档盘点 `/home/changjunhan/Uni-Lab-Core` 下现有的工作流编写、执行、
实时通信和调试交互。它是一份实现迁移地图，不代表可以保留旧版公共合同。

2026-07-30 检查的代码依据如下：

| 仓库 | 检查版本 | 作用 |
|---|---|---|
| `Uni-Lab-Core/uni-lab-fe` | `0fd39af` | 旧版前端行为和测试依据 |
| `Uni-Lab-Core/Uni-Lab-OS` | `f5c1073` | 旧版 OS bridge、runtime 行为和测试依据 |
| `uni-lab-backend-github` | 冻结版本 `09609a2` | 共享前端合同权威 |
| 目标 `Uni-Lab-OS` | 分支 `migration/01-backend-contract` | 唯一实现写入范围 |

目标仓库中的约束决策如下：

- [D-005](decisions.md#d-005-mirror-backend-identity-and-naming)：不得保留
  `run_id`、旧版 wire identity、`/api/v1/runtime/runs` 或兼容适配器；
- [D-025](decisions.md#d-025-frontend-realtime-mirrors-backend-sse)：前端实时通信
  统一使用全局 `/api/v1/events` SSE；
- [D-034](decisions.md#d-034-mirror-backend-graph-editing-routes)：镜像冻结 Backend
  的 Graph 以及 Node/Edge 编辑路由；
- [D-040](decisions.md#d-040-retain-three-os-only-pure-authoring-transforms)：
  保留三个使用 Backend-shaped identity 的纯 Authoring 转换接口；
- [D-041](decisions.md#d-041-sqlite-is-the-local-applied-workflow-authority)：
  本地 Applied Workflow 和 Task 事实存储在 `workflow.db`；
- [D-043](decisions.md#d-043-mirror-backends-current-http-response-envelope)：
  使用冻结 Backend 的响应 envelope；
- [D-046](decisions.md#d-046-debugger-extensions-are-os-only-in-this-migration)：
  共享调试能力仅包括 Backend run mode 和四种 Task command；
- [D-058](decisions.md#d-058-freeze-only-backends-frontend-contract-at-09609a2)：
  只有冻结 Backend 的前端接口属于 parity 范围；
- [D-073～D-081](decisions.md#d-073-persistent-authoring-is-a-workflow-scoped-os-only-resource)：
  持久 Authoring 必须是 Workflow-scoped、服务端持有、CAS 保护并通过 SSE 失效通知。

## 结论

旧代码不是一个小型兼容层，而是一套已经过时的完整公共模型，并分散在四个边界：

1. FE service port 暴露 Canonical v2 和 Run identity；
2. `WorkflowPanel` 把浏览器 buffer 和浏览器选择的文件当作 Authoring authority
   的一部分；
3. 旧版 OS bridge 暴露 Canonical/Run REST 接口和每个 Run 独立的 WebSocket；
4. runtime 测试和 E2E 测试直接断言这些旧 identity 和路由。

旧代码中的用户能力仍然有价值，但大部分集成都必须进行**语义迁移**。复制旧路由
或添加兼容 shim 会违反 D-005。下文使用以下处置分类：

- **直接迁移**：保留行为或测试意图，仅进行局部机械调整；
- **语义迁移**：保留能力，但重写 authority、identity、DTO 或操作顺序；
- **拆分迁移**：将一个旧模块或测试中的不同能力分别移交给不同阶段；
- **已取代**：删除旧公共行为，并在受维护接口上用替代测试证明新行为；
- **延后**：在指定 grill 或阶段关闭前不得实现。

## 1. FE service 与公共路由迁移

旧版公共端口集中在
[packages/services/src/workflow.ts](/home/changjunhan/Uni-Lab-Core/uni-lab-fe/packages/services/src/workflow.ts:4)，
并由
[createServices.ts](/home/changjunhan/Uni-Lab-Core/uni-lab-fe/packages/services/src/createServices.ts:23)
为所有 authority 装配。

| 旧版 FE 操作 | 旧路由或数据形态 | 新操作 | 处置 | 归属阶段 |
|---|---|---|---|---|
| `getWorkflow(workflowId)` | `GET /workflows/{id}/graph`，返回前端自定义的 `WorkflowDocument.revision.canonical` | 共享 Graph 消费方使用冻结 Backend Graph；OS 双视图编辑器通常通过一个 Workflow-scoped Authoring aggregate 完整恢复状态 | 语义迁移 | OS 01/02；FE 08 |
| `saveWorkflow(...)` | `PUT .../graph`，携带 Canonical revision 和可选字符串 revision ID | 共享完整 Graph PUT 携带整数 `revision`、Backend Node write 和 Backend Edge write；OS 双视图 Authoring 必须通过 Draft/Candidate/Apply 保持源码同步，不能把 Canonical JSON 当作持久化事实 | 拆分迁移 | OS 01/02；FE 08 |
| `validateWorkflow(...)` | `POST /workflows:validate`，携带 Canonical 和 parameters | 持久编辑使用 Draft diagnostics 和 Apply revalidation；非持久 Candidate 可以使用 D-040 `/authoring/validate` | 旧路由已取代，能力语义迁移 | OS 02；FE 08 |
| `compilePythonWorkflow(...)` | `POST /authoring/compile`，使用 `base_revision_id`、客户端 `source_uri` 和 Canonical 输出 | 保留纯转换路由，但 wire model 改为 `workflow_uuid`、整数 `revision`、稳定 Node/Edge UUID 和 Backend-shaped Graph；持久编辑通过 Workflow-scoped 路由保存完整 Draft | 语义迁移 | OS 02；FE 08 |
| `generatePythonWorkflow(...)` | `POST /authoring/generate-python`，输入 Canonical | 保留纯转换路由，输入完整 Backend-shaped Candidate Graph，输出确定性的 normalized source | 语义迁移 | OS 02；FE 08 |
| `validateAuthoringCandidate(...)` | 浏览器把完整旧 Candidate 回传给 OS | 非持久调用方可继续使用纯验证；持久 Apply 只发送 `expected_draft_hash`、`expected_workflow_revision` 和 `expected_candidate_hash`，绝不携带 Candidate 内容 | 拆分迁移；旧持久流程已取代 | OS 01/02；FE 08 |
| `createRun(request)` | `POST /runtime/runs`，发送完整 Canonical revision 和旧 debug 字段 | `POST /workflow-tasks` 发送 `workflow_uuid`、`run_mode`、可选 `target_node_uuid`、`input`、description 和 metadata；OS 对已持久化 Graph 建立快照并预创建 Jobs | 旧路由已取代，能力语义迁移 | OS 01/03/04；FE 08 |
| `getRun(runId)` | `GET /runtime/runs/{run_id}` | `GET /workflow-tasks/{task_uuid}` | 语义迁移 | OS 01/04；FE 08 |
| `listRunNodes(runId)` | `GET .../runs/{id}/nodes` | `GET /workflow-tasks/{task_uuid}/jobs` | 语义迁移 | OS 01/04；FE 08 |
| `listRunEvents(runId, cursor)` | Task-scoped REST event page | 不存在 Task-scoped event 路由；相关全局 SSE 到达后，通过 REST 重新获取 Task/Jobs | 已取代 | OS 01/04；FE 08 |
| `command(runId, command, payload)` | `POST .../runs/{id}/commands`，随后获取 Run | `POST /workflow-tasks/{task_uuid}/commands`，携带 `type`、可选 `target_node_uuid`、必填 `idempotency_key`、description 和 metadata；成功响应为 command record | 语义迁移 | OS 01/04；FE 08 |
| `cancelRun(runId)` | 独立的 `POST .../runs/{id}/cancel` | Task command `{type:"cancel", idempotency_key:...}` | 旧路由已取代 | OS 01/04；FE 08 |
| `subscribeRunEvents(...)` | 每个 Run 独立的 `/runtime/events` WebSocket；失败后轮询 Run events | 统一使用全局 `GET /events` SSE，支持单调递增 `id`、`Last-Event-ID`、客户端去重和 REST 状态恢复 | 已取代并使用新实现替换 | OS 01/04；FE 08 |
| `dispose()` | 关闭 Run socket 和轮询 timer | 释放共享 SSE subscription/service | 直接保留生命周期意图 | FE 08 |

旧方法的完整定义位于
[workflow.ts:168](/home/changjunhan/Uni-Lab-Core/uni-lab-fe/packages/services/src/workflow.ts:168)。
冻结共享路由的依据是 Backend `09609a2` 中的
`internal/http/handler/workflow.go`；目标仓库已经实现的路由位于
[workflow_api.py](/home/gaojing/Uni-Lab-OS/unilabos/app/workflow_api.py:96)。

### DTO 处置

| 旧版公共 DTO | 处置 |
|---|---|
| `WorkflowRevision`（`workflow_id`、`revision_id`、`node_id`、`invocations`、`control_edges`） | 从 FE 公共 port 删除；Phase 02 可以保留内部 compiler IR，但 UI 和执行层不得把它当作 wire authority |
| `WorkflowDocument` | 替换为冻结 Backend Graph 和 Workflow DTO |
| `WorkflowValidationResult` | 持久 diagnostics 进入 Authoring aggregate；请求或服务失败使用 Backend envelope |
| `WorkflowAuthoringCandidate/Result` | 持久用法替换为 D-077 `AuthoringAggregate` 和不透明 `candidate_hash`；source map 使用 `workflow_node_uuid` |
| `WorkflowRun` | 替换为 `WorkflowTask`，不得保留 type alias |
| `WorkflowRunNode` | 替换为 `WorkflowNodeJob` 及其 Backend 字段 |
| `WorkflowRunEvent` | 替换为按类型区分的全局 SSE frame；不得假设所有事件都有 `runId/nodeId` |
| `WorkflowRunRequest` | 删除；Task 创建绝不携带 DAG |
| 旧 debugger command union | 共享 command 仅包括 `step`、`pause`、`resume` 和 `cancel`；其他操作等待 P1-1 |

旧 `unwrap()` 同时接受裸对象和 `data` envelope
（[workflow.ts:376](/home/changjunhan/Uni-Lab-Core/uni-lab-fe/packages/services/src/workflow.ts:376)）。
这种宽松 fallback 会掩盖合同漂移，必须删除。新 port 应严格解析
`{"code":0,"data":...}` 和标准 error envelope。

[realtime.ts](/home/changjunhan/Uni-Lab-Core/uni-lab-fe/packages/services/src/realtime.ts:30)
中的 device-status socket 是独立的旧版设备投影。删除 Workflow realtime WebSocket
时，不得把这个设备通道静默改造成 WorkflowTask 通道，也不得在它自己的归属阶段前
一并删除。

## 2. FE 编辑器状态与交互迁移

当前没有独立的权威 Workflow store。Authoring、Run、Node、event 和 debugger
状态全部保存在一个
[WorkflowPanel](/home/changjunhan/Uni-Lab-Core/uni-lab-fe/packages/workflow-editor/src/components/WorkflowPanel.tsx:56)
的 `useState/useRef` 中。现有依赖注入边界可以保留，但其后的 port 和 controller
必须替换。

| 旧版行为 | 新约束 | 处置 |
|---|---|---|
| `canonicalSource`、`pythonBaseline` 和 `pythonSourceMap` 在 CodeMirror buffer 之外分别维护 | 文档状态应为 `远端 AuthoringAggregate + 本地 dirty buffer + 编辑开始时观察到的 draft hash/revision + 待处理的外部失效通知` | 语义迁移 |
| `run`、`runNodes`、`events` 和每个 Run 的 sequence 成为本地投影 authority | 持久 `WorkflowTask`/`WorkflowNodeJob` REST record 才是 authority；全局 SSE 只负责通知失效 | 语义迁移 |
| event 到达后获取旧 Run 和 Node | 保留“事件触发重新获取”的原则，但按 Workflow/Task identity 路由 SSE，并获取 Task/Jobs/Authoring | 语义迁移 |
| localStorage 记忆旧字符串 workflow ID | 使用稳定 Workflow UUID 和带版本的 storage key 保留选择恢复能力；不得缓存 Graph 事实作为 authority | 直接保留意图 |
| 浏览器 File API 读写已注册 Python source | OS 独占已注册 package source；独立的显式导入/导出功能仍可接受用户选择的文件，但它不是持久 Draft authority | 旧 authority 已取代 |
| 客户端自行生成 `source_uri` | OS 返回预注册 logical URI；客户端不得构造或提交任意路径 | 已取代 |
| 700 ms compile/validate 后更新 Canvas，并称为自动应用 | debounce 可以刷新未应用预览，但只有 Apply 能修改 Applied Workflow | 语义迁移 |
| Save 先验证 Canonical、保存 Graph，再选择性覆盖浏览器选中的源文件 | 产品操作拆分为 Draft Save 和显式 Apply；normalized package-source writeback 只能由 OS 执行 | 语义迁移；浏览器写入已取代 |
| Run 发送完整的当前 Canonical 文档 | 有效的 dirty Draft 必须先 Apply，之后才能依据持久 Workflow UUID 创建 Task | 语义迁移 |
| 前端根据一个 start node 计算可执行子图 | 前端可以预览范围，但 Task plan 和 Jobs 由 OS 决定；P1-1 将单起始点替换为非空 start frontier | 延后语义迁移 |
| runtime 颜色依赖旧 node state 和一个 `pausedBeforeNodeId` | 分别渲染 Job status、Task control status、Breakpoint Hold、disabled 和 out-of-scope | 延后至 04/05/08 |
| output panel 读取旧 `node.result` 和 Run events | 读取 Job result/error 投影和最终 `WorkflowTask.output` | 被 P0-5 阻塞 |
| UUID 丢失时按 action/type/ordinal 猜测映射 | 删除 identity 猜测；Phase 02 通过稳定 source anchor 保留 Node UUID | 已取代 |
| 纯前端 `useWorkflowDebug` 模拟执行状态 | 删除；渲染持久 Task/Job/Hold 投影 | 已取代 |
| 根据参数字段名猜测 Material reference | 使用已冻结的 P0-3 Material 合同和待冻结的 P0-4 Action ResourceSlot 合同替换 | 仅被 P0-4 阻塞 |

主要消费位置：

- 状态和 service 调用：
  [WorkflowPanel.tsx:76](/home/changjunhan/Uni-Lab-Core/uni-lab-fe/packages/workflow-editor/src/components/WorkflowPanel.tsx:76)；
- Run 刷新和订阅：
  [WorkflowPanel.tsx:192](/home/changjunhan/Uni-Lab-Core/uni-lab-fe/packages/workflow-editor/src/components/WorkflowPanel.tsx:192)；
- compile/validate 循环：
  [WorkflowPanel.tsx:475](/home/changjunhan/Uni-Lab-Core/uni-lab-fe/packages/workflow-editor/src/components/WorkflowPanel.tsx:475)；
- 保存和浏览器文件回写：
  [WorkflowPanel.tsx:661](/home/changjunhan/Uni-Lab-Core/uni-lab-fe/packages/workflow-editor/src/components/WorkflowPanel.tsx:661)；
- 旧执行提交和 command：
  [WorkflowPanel.tsx:755](/home/changjunhan/Uni-Lab-Core/uni-lab-fe/packages/workflow-editor/src/components/WorkflowPanel.tsx:755)；
- Canonical 投影和 identity 猜测：
  [canonicalWorkflow.ts](/home/changjunhan/Uni-Lab-Core/uni-lab-fe/packages/workflow-editor/src/utils/canonicalWorkflow.ts:14)。

编辑器不应继续在单体 Panel 中逐个替换调用，而应拆出三个 controller 边界：

1. `WorkflowAuthoringController`：aggregate 状态恢复、本地 dirty buffer、Draft
   双 CAS、Apply 三 token CAS、冲突比较和外部失效通知；
2. `WorkflowTaskController`：Task 创建、Task/Job 状态恢复、全局 SSE 路由和普通
   command；
3. `WorkflowDebugController`：launch configuration 和 Hold 投影；仅在 P1-1
   以及 Phase 05 完成后加入。

这是模块边界要求，不强制使用某一种 FE 状态管理库。

## 3. 旧版 OS 源码处置

旧版 OS 是行为依据，但它的公共 Canonical/Run 接口不是迁移目标。

| 旧版源码 | 可保留的行为 | 拒绝或已取代的部分 | 目标阶段 |
|---|---|---|---|
| [local_api.py](/home/changjunhan/Uni-Lab-Core/Uni-Lab-OS/unilabos/app/local_bridge/local_api.py:232) | 薄 composition seam、验证和错误处理经验、现有 Authoring/runtime fixture | `/workflows:validate`、Canonical Graph DTO、`/runtime/runs`、独立 cancel/reconcile、每个 Run 的 events 以及 `/runtime/events` WebSocket | 拆分至 01/02/04/05 |
| [runtime/service.py](/home/changjunhan/Uni-Lab-Core/Uni-Lab-OS/unilabos/runtime/service.py:131) | compile-to-plan 编排、dispatch unknown 处理、持久状态优先于 transport 投影、取消保护、reconciliation 和 debug 验证 | `run_id`、Canonical submission、Run projection 和旧公共 command vocabulary | 拆分至 03/04/05 |
| [event_store.py](/home/changjunhan/Uni-Lab-Core/Uni-Lab-OS/unilabos/runtime/event_store.py:106) | terminal/effect/outbox/cursor 原子提交、持久排序、启动恢复 | 使用 `run_id`/旧 node projection 的表，以及旧 Run event wire schema | 语义迁移至 04 |
| [workflow_store.py](/home/changjunhan/Uni-Lab-Core/Uni-Lab-OS/unilabos/runtime/workflow_store.py:24) | revision conflict 经验和重启持久性测试 | `~/.unilabos/workflows` 下每个 Canonical 一个文件的 authority | 由 01/02 和 D-081 取代 |
| [schedule_ws.py](/home/changjunhan/Uni-Lab-Core/Uni-Lab-OS/unilabos/app/local_bridge/schedule_ws.py:72) | 内部 submit/cancel/debug transport 行为和迟到事件处理 | 前端公共 parity、Run projection，以及复制 Backend Edge protocol 的要求 | 语义迁移至 04/06 |
| [offline_os.py](/home/changjunhan/Uni-Lab-Core/Uni-Lab-OS/unilabos/app/local_bridge/offline_os.py:48) | 确定性执行、debug、cancel 测试行为 | 公共 fake Run authority | 作为 03/04/05/06 的语义 fixture 来源 |
| `unilabos/workflow/{authoring,canonical,canonical_ir,contracts,dag_compile}.py` | no-exec AST parsing、normalization、source map、控制流和参数行为 | 公共 Canonical DTO 和旧字符串 identity | 语义迁移至 02/03 |

旧 bridge 路由集中在
[local_api.py:1218](/home/changjunhan/Uni-Lab-Core/Uni-Lab-OS/unilabos/app/local_bridge/local_api.py:1218)
至
[local_api.py:1596](/home/changjunhan/Uni-Lab-Core/Uni-Lab-OS/unilabos/app/local_bridge/local_api.py:1596)。
替代实现不得在新接口旁继续挂载这些路由。

以下 runtime 语义值得通过替代测试保留：

- 不确定的 dispatch 失败必须进入持久 unknown/attention 状态，不得伪装成可重试或成功；
- 持久 journal 状态优先于已连接 transport session 的投影；
- 一个 terminal transition 及其 effect/outbox/cursor 必须原子提交；
- 重启 reconciliation 必须保留物理不确定性 fence；
- 不能仅因 session 消失就宣称 cancel 成功；
- Task command 必须经过验证并保持幂等；
- 执行必须消费不可变 Task snapshot，而不是当前 Draft 或可变 Graph。

## 4. 测试迁移与重新分类

### FE 单元测试和 E2E 测试

| 测试来源 | 新处置 |
|---|---|
| [services workflow tests](/home/changjunhan/Uni-Lab-Core/uni-lab-fe/packages/services/src/workflow.test.ts:25) | 使用严格 Graph、Authoring、Task、Job、Task command 和全局 SSE 合同测试，替代 Canonical/Run 路由断言 |
| [canonicalWorkflow tests](/home/changjunhan/Uni-Lab-Core/uni-lab-fe/packages/workflow-editor/src/utils/canonicalWorkflow.test.ts:13) | 迁移有价值的 Graph/view 投影断言；删除猜测 identity 的 remap；多起点和控制流用例延后至 P1-1/P1-2 |
| [debugControls tests](/home/changjunhan/Uni-Lab-Core/uni-lab-fe/packages/workflow-editor/src/utils/debugControls.test.ts:31) | 按四种共享 Task command 重写；P1-1 关闭后再加入 Hold |
| [workflow-runtime E2E](/home/changjunhan/Uni-Lab-Core/uni-lab-fe/e2e/workflow-runtime.spec.ts:5) | 拆分为持久 Authoring、普通 Task 执行和 debugger 套件；删除所有旧路由断言 |
| [workflow import/persistence E2E](/home/changjunhan/Uni-Lab-Core/uni-lab-fe/e2e/workflow-import-persistence.spec.ts:6) | 保留选择恢复；Python 流程改为已保存 Draft、未应用预览和显式 Apply |
| [workflow cloud import E2E](/home/changjunhan/Uni-Lab-Core/uni-lab-fe/e2e/workflow-cloud-import.spec.ts:7) | 迁移为显式服务端 import 和 Backend Graph；删除浏览器 source authority 以及旧 validate/Run 路由 |
| [debug action E2E](/home/changjunhan/Uni-Lab-Core/uni-lab-fe/e2e/workflow-debug-actions.spec.ts:38) | 迁移普通 `pause/step/resume/cancel`；其余旧操作延后至 P1-1，不保留 alias |
| [debug scenario E2E](/home/changjunhan/Uni-Lab-Core/uni-lab-fe/e2e/workflow-debug-scenarios.spec.ts:41) | 保留 Code/DAG 视觉同步意图；用 Task/Jobs/SSE 和 P1-1 launch/Hold 语义替换 Run/events |
| [unsaved guard E2E](/home/changjunhan/Uni-Lab-Core/uni-lab-fe/e2e/workflow-unsaved-guard.spec.ts:3) | 保留并加强：外部 SSE 失效通知或 CAS conflict 不得替换或清除 dirty buffer |
| [layout isolation E2E](/home/changjunhan/Uni-Lab-Core/uni-lab-fe/e2e/workflow-layout-isolation.spec.ts:5) | 原样保留；它不是 API 迁移测试 |

持久 Authoring 必须新增以下 E2E 覆盖：

1. clean editor 收到 `workflow.authoring.changed` 后获取 aggregate 并采用外部修改；
2. dirty editor 收到相同事件后保留完整 buffer，并显示待处理的外部修改；
3. Draft hash、Workflow revision、catalog 和 Candidate conflict 按固定顺序返回，
   且保留本地状态；
4. Draft 缺失时成功返回 `draft_missing` aggregate；
5. 外部删除 source 不得删除 Applied Graph，也不得阻止显式执行；
6. 恢复已注册路径后发出 `cause=recovered`；
7. Apply 已提交但 source writeback 失败时返回可恢复 warning，不得返回 Apply 失败；
8. SSE 重连从 `Last-Event-ID` 恢复，并忽略重复 aggregate tuple；
9. dirty 且可运行的文档必须先 Apply 再创建 Task；Apply 失败时不得创建 Task；
10. 后续 Graph 修改不得改变既有 Task snapshot 或已预创建的 Jobs。

### 旧版 OS 测试清单修正

现有[测试清单](test-inventory.md)把以下三个完整旧文件归入 Phase 01：

```text
tests/contracts/test_workflow_revision_contract.py
tests/app/test_runtime_service_boundary.py
tests/app/test_unified_runtime_api.py
```

该归类不能理解为直接迁移：

| 旧测试 | 正确处置 |
|---|---|
| `test_workflow_revision_contract.py` | 旧公共 Canonical 合同已取代；有价值的 compiler invariant 转为 Phase 02 测试，公共 wire 断言由 Phase 01 Graph/Task/Authoring 合同测试替换 |
| `test_runtime_service_boundary.py` | planning/dispatch/recovery 语义拆分至 Phase 03 和 04；不保留任何 Run DTO 或路由 |
| `test_unified_runtime_api.py` | 旧公共路由由 Phase 01 共享路由测试、Phase 04 runtime 行为测试、Phase 05 debugger 测试和 Phase 08 真实 FE E2E 替换 |

Phase 04 下其余 source-only runtime 测试仍是有价值的行为依据，但所有
Run/table/route 断言都必须语义转换为 Task/Job/event identity。

## 5. 可执行阶段计划

| 顺序 | 交付内容 | 进入条件 | 退出证据 |
|---:|---|---|---|
| 1 | 完成 Phase 01 冻结共享接口：个体 Node/Edge 管理和普通 Task command | P0-1 已关闭 | 冻结 Backend parity 测试通过；新模块中没有旧公共 vocabulary |
| 2 | 实现 Phase 02 生产 compiler、template catalog import、package source discovery 和 D-040 Backend-shaped 纯转换 | Phase 01 已关闭；P0-2 已关闭；完整 action ResourceSlot projection 仍需 P0-4 | 稳定 UUID/source-map round trip；真实 package Draft 注册；无公共 Canonical wire DTO |
| 3 | 深化 scheduler，使其消费不可变 Task snapshot 并更新预创建 Jobs | Phase 02 compiler/plan seam 可用；P0-3 已关闭，完整 action input 仍需 P0-4 | 无 Run alias 的 Task/Job 状态测试通过 |
| 4 | 迁移 journal、outbox、recovery、reconciliation、feedback 和前端 runtime SSE 投影 | Phase 03 Node scheduler 完成 | 基于 Task/Job identity 的 restart/unknown/cancel/replay 测试通过 |
| 5 | 先实现普通 debugger，再实现 P1-1 launch/Hold 扩展 | Phase 04 durable runtime 完成；完整 debugger 需要 P1-1 关闭 | Task command 和 Hold E2E 通过；前端不持有 debugger authority |
| 6 | 在前端合同之后集成 device execution 行为 | Phase 04/05 runtime seam 完成 | 真实和 fake driver parity、安全 cancel/recovery 测试通过 |
| 7 | 集成 Material authority 和 ResourceSlot 流程 | P0-3 已关闭；Action projection 需 P0-4 关闭 | 结构化 Material E2E 通过；无字段名 heuristic |
| 8 | 替换 FE port、拆分 Panel controller、迁移 UI，并执行真实 OS E2E | 对应 OS slice 完成；P0-2/P0-3 决策已关闭，完整产品闭环仍需实现它们并关闭 P0-4/P0-5；完整 debugger 需要 P1-1 | 产品 FE 中没有旧路由、DTO、WebSocket 或客户端文件 authority |
| 9 | 替代测试存在后删除已取代的 bridge 代码和 fixture | 所有替代测试套件通过 | 全仓测试通过；清单中没有未处置条目 |

### Phase 01 立即实施项

当前目标已经实现 Graph GET/full PUT、Task 创建和查询、Authoring
GET/Draft/Apply 以及全局 SSE。在开始替换 FE 前，Phase 01 仍需完成：

1. 镜像冻结 Backend 的个体 WorkflowNode 和 WorkflowEdge 管理接口；
2. 实现 `POST /workflow-tasks/{task_uuid}/commands`，包括冻结的幂等和错误行为；
3. 为以上接口补充 route/DTO parity 测试；
4. 证明后续 Graph 修改不会改变既有 Task snapshot 和预创建 Job；
5. 执行阶段测试和完整 baseline gate；
6. merge 前更新阶段 ledger。

不得为了临时解锁旧 FE 而增加 `/runtime/runs` 适配器。所需 OS slice 完成后，
FE 必须直接面向最终 port 实现。

## 6. 完成条件

只有满足以下全部条件，FE–OS 交互迁移才算完成：

- 本矩阵每一项都已实现、由替代测试明确取代，或仍由一个未关闭阶段负责；
- 产品 FE 中不存在 `/api/v1/runtime/runs`、`/api/v1/runtime/events`、
  `run_id`、`WorkflowRun` 或公共 Canonical-v2 identity；
- 新 OS 公共模块中不存在旧 Run 路由或 wire alias；
- 持久 Apply 请求无法表达 Candidate、Graph 或 source 内容；
- 浏览器代码无法把已注册 package Draft 作为 authority 直接读写；
- 全局 SSE 可恢复，并且只负责使持久 REST 状态失效；
- Task 创建绝不携带 DAG，并且始终对当前 Applied Graph 建立快照；
- 普通 Task command 保持幂等，并返回冻结的 command record；
- dirty buffer conflict 和外部修改行为通过真实 browser-to-OS E2E；
- 迁移后的 E2E 使用真实 OS 接口，不依赖旧 local bridge mock 或兼容路由。
