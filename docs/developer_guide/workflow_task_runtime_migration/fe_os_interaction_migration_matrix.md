# FE–OS 交互迁移矩阵与 Phase 02H 起整体计划

## 状态与范围

本文档盘点旧版工作流编写、执行、实时通信和调试交互，并给出从 Phase 02H
开始的 OS、前端、Scheduler/设备和跨仓联调整体计划。它是 OS 仓库中的实现迁移
地图，不代表可以保留旧版公共合同，也不取代 Core Wayfinder 的工作状态和跨仓
决策权威。

2026-07-30 检查的代码依据如下：

| 仓库 | 检查版本 | 作用 |
|---|---|---|
| `Uni-Lab-OS/uni-lab-fe` | `0fd39af3014b29035ee8e2280b9d753b2b9f96a2` | 旧版前端行为和测试依据 |
| `Uni-Lab-OS/Uni-Lab-OS` | `f5c10733e7e37218ab5c660ecef9c41bb94c72ab` | 旧版 OS bridge、runtime 行为和测试依据 |
| `Uni-Lab-OS/uni-lab-backend` | 冻结版本 `09609a27e652c9e56ede636a2883a4fd241e4400` | 共享前端合同权威 |
| 发布 `Uni-Lab-OS/Uni-Lab-OS` | R1B 受测/受审候选 `6cc9390623b21061d31800a36f653e7d82750b62`；R1B non-squash merge `c540337d87a29003d02ea9653e6a042ca201897a`；UI1C 证据记录 `5d5ceb77f3f385de9a5050f3c1583d6a03c85b88`；UI1D 证据记录 `726aca42760f42abdde8b28341a656489ac56450` | Phase 01、02A～02H、02G1、R1A 和 R1B 已合入并把 `integration/workflow-task-runtime` 推送到组织仓库；UI1D 跨仓证据已更新；后续 OS 工作树统一由 Core 下的 submodule Git 仓库管理 |
| 发布 `Uni-Lab-OS/uni-lab-fe` | FE-D117 候选 `c779d473a2553c07b5e0a8551649567085501c28`；UI1A production/test 候选 `5ca7cd2b2baa5d0656626af25874fd597b19c267`；UI1B 纠偏候选 `e864e491463191473ab4f691cc7c26a1c5d4c6e3`；UI1C 候选 `eb5e2a30b391a5c7aae7400bf616bcdfa0175065`；UI1D 候选 `27212c7674f746d0ac941ccf592dd57644983272`；当前 FE integration `bb0bb249afd0dd6ded0025fb8c34e534aec5c278` | UI1D 已删除旧 Run DTO/client/hook、Runtime WebSocket、polling fallback 和 local bridge E2E；原生产工作台完成真实 OS final gate，候选与 non-squash integration 均已推送；Core pin/证据已发布，独立 exact-SHA review 与 Feishu Testing 对齐待完成 |

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

后续功能合同由 Core Wayfinder 的功能索引继续承载：Authoring/source 为
`Uni-Lab-OS/Uni-Lab-Core#128`、`Uni-Lab-OS/Uni-Lab-Core#129`，前端交互为
`Uni-Lab-OS/Uni-Lab-Core#130`，Debugger 为 `Uni-Lab-OS/Uni-Lab-Core#131`，
Conditional Join 为 `Uni-Lab-OS/Uni-Lab-Core#132`，Workflow I/O 为
`Uni-Lab-OS/Uni-Lab-Core#133`，Material/Site 为 `Uni-Lab-OS/Uni-Lab-Core#134`，
Action typed contract 为 `Uni-Lab-OS/Uni-Lab-Core#135`，subworkflow/Task output 为
`Uni-Lab-OS/Uni-Lab-Core#136`，Tool Call 为 `Uni-Lab-OS/Uni-Lab-Core#138`。
MaterialSource 及其分配边界继续由 `Uni-Lab-OS/Uni-Lab-Core#140～#146` 细化，
`Uni-Lab-OS/Uni-Lab-Core#148` 明确延期。

当前迁移快照必须按功能而不是旧 phase 编号解读：

- **OS 已完成**：02H Task input preflight、02G1 本地 Authoring authority 回环、R1A
  command durable ingress，以及 R1B command 消费、Task/Job 状态机、journal/outbox、
  feedback history、unknown/reconcile、restart recovery 和唯一
  `workflow.runtime.changed` invalidation；R1B 受测/受审候选已本地 non-squash 合并；
- **前端已完成**：FE-D117 已关闭 Authoring 单写权威和真实浏览器 delivery gate；
  UI1A 已新增严格 Backend envelope 的 Task/Job/command/feedback service port，以及
  只承载 invalidation 的全局 SSE 重连、游标和去重；UI1B 已在原
  `PersistentWorkflowAuthoringPanel` 中复用 `WorkflowDag`、`WorkflowNodeCard`、
  `CodeEditor/useCodeMirror`、`WorkflowDebugger` 和 `WorkflowOutput`，仅增加 Task
  controller/view adapter，并接回原起点/断点控件与共享代码 marker 投影；完成
  coherent Task/Jobs rehydration、normal/step 创建入口和四种共享 command 接口；
  UI1C 在同一 controller/UI 上完成每 Job feedback cursor/分页/去重、partial-read
  stale projection、显式重试、SSE `Last-Event-ID` 重连、同库 OS restart/recovery 和
  Authoring/Runtime stream 恢复提示；UI1D 已删除旧 Run/socket/polling/public DTO、
  旧 local bridge 与孤儿 E2E，并以 fail-closed 的 `PersistentWorkflowAuthoringPanel`
  作为唯一 WorkflowPanel production 路径；
- **前端 Runtime 纵向迁移已完成**：UI1D 候选和 FE integration 已推送；真正 Debug
  launch/multi-start/Hold、Catalog 与 device execution 仍由各自后续功能票拥有，
  不属于 UI1D；
- **联调进度**：R1B、UI1A、UI1B 和 UI1C 候选均已进入已推送的 OS/FE integration
  历史，并由 Core `main` 的 submodule gitlink 固定；UI1B 已通过独立真实
  FE→OS happy path（原控件可见、起点/断点设置与取消、DAG/code gutter 同步、
  Task create/read、两个 Job、pause/resume/cancel accepted 与 applied 区分、
  SSE/REST 补读、reload 恢复）并产出 10 张截图；UI1C 真实 FE→OS fault/restart
  场景已验证 cursor `0→1→2`、Jobs 503 保留一致投影、键盘重试、断线重连携带
  `Last-Event-ID`、同库 startup recovery 的 `execution_unknown` 和 reload 恢复，
  产出 8 张截图与网络账本。UI1D 在 FE `27212c7`、OS `3eb8a59` 上通过
  Authoring/UI1B/UI1C 真实 OS 回归 7/7 和 final gate 1/1；最终账本记录 50 个请求、
  50 个响应、9 张截图、旧路由 0、WebSocket 0、应用/page error 0，并验证 step
  command 重放、409 conflict 与 terminal race。普通 Task create 在调试配置存在时
  仍不携带 `start_node_id` 或 `breakpoints`。Core integration spec 已在 UI1D
  production change 前写入 `Uni-Lab-OS/Uni-Lab-Core#152`；Core gitlink 和团队证据
  已发布于 `c3e0003a2ebd9f0dcbb8db4bb0c8fcb123121b7b`，下一步是独立 exact-SHA
  review 和 issue/Feishu Testing 对齐。此前不得把 Core Decision
  `Uni-Lab-OS/Uni-Lab-Core#150` 推进到 `stage:accepted`。

### 当前 Interface 迁移状态

| 功能 Interface | OS | 前端 | 联调 |
|---|---|---|---|
| `POST/GET /api/v1/workflow-tasks` 与 `GET .../jobs` | 已按 Backend DTO 持久化 Task、snapshot、预创建 Job；02H 完成 input preflight | UI1A service port 与 UI1B controller/原 UI adapter 已接入；subscribe-before-read、Task/Jobs coherent bundle、拓扑排序、normal/step 创建与 reload 恢复有测试 | UI1B happy path 与 UI1D step-mode/final gate 均通过真实 OS；浏览器未发送 DAG 或旧 Run 请求 |
| `POST .../workflow-tasks/{uuid}/commands` | R1A 完成 durable ingress；R1B 完成 FIFO consumption、result、pause/resume/step permit/cancel | UI1B 已把原调试控制条接到四种共享 command，并把 HTTP 201 record 与 Task 权威状态分开展示；请求中禁用重复提交 | pause/resume/step/cancel accepted→SSE/REST applied、相同 key 重放、不同 payload 409 与 terminal race 均通过 UI1D final gate |
| `GET /api/v1/workflow-node-jobs/{uuid}/feedback` | R1B 完成 sequence cursor、双键幂等 history、summary 与 restart persistence | UI1C 已把每 Job cursor、分页、UUID/sequence/idempotency 去重和 stale/retry 投影接入原 `WorkflowOutput` Feedback tab；结构化 data 与 source node 可见 | 真实 OS 已验证 cursor `0→1→2`、三条 history 不重复、503 后保留已确认数据、显式重试与 reload 恢复 |
| `GET /api/v1/events` 的 `workflow.runtime.changed` | R1B 完成同事务 outbox、全局 cursor/replay，payload 仅有 `workflow_task_uuid` | UI1C 已补齐连接状态、非主动 EOF/网络错误、重连 `Last-Event-ID` 和 on-open REST rehydration；event 仍只作 invalidation | 真实 OS 已验证进程停止显示正在重连、同端口同库重启后恢复连接、startup recovery 投影 `execution_unknown`；无 Runtime WebSocket/旧 Run 请求 |
| Debugger 起点/断点配置投影 | OS-only debug launch/projection 仍由 `deepmodeling/Uni-Lab-OS#299` 实现；普通 Task Interface 不接受调试配置 | UI1B 在最新活跃 FE 基线上直接复用原 `WorkflowNodeCard` 按钮、DAG overlay、右键/双击与 CodeMirror marker；配置只进入当前前端会话预览，不调用旧 Run/WS 接口 | 真实 OS Playwright 已覆盖设置、取消、DAG/gutter 同步和普通 Task 请求隔离；多起点、durable Hold 和真实 debug launch 仍归 FE #1 / Core #6/#137 |
| Workflow-scoped Authoring aggregate/Draft/Apply | OS 02G1 已完成本地 authority 回环 | FE-D117 已本地合入 `e67feb1d` | Authoring delivery 已有 5 项 Playwright；最终 Core/Feishu 接受仍随 X1 统一 pin |
| 设备动作 Catalog 与单节点执行 | R1 不提供旧 `/workflow-node-templates` 或 local bridge 单节点 Run；最终 Catalog/执行分别归 A1/D1 | UI1D 保留设备目录与参数表单组件，但删除临时 `createRun/getRun/poll/cancel`；入口明确移交 WorkflowTask，生产接口缺失时 fail closed | 不用 fake bridge 冒充已联调；Catalog/editor 归 Core #135，真实 device adapter/result commit 归 D1 后续 gate |
| DAG readiness/admission 与 device result | R2/D1 未开始；R1B 明确不 dispatch | 只能展示 durable projection，不得自行推进状态 | 必须等待 R2/D1，不能用 R1B kernel 冒充可执行闭环 |

本工作树的 `decisions.md` 目前只包含较早账本；D-102～D-116 在完成
`Uni-Lab-OS/Uni-Lab-Core#2` 的不可变 source publication 前，不应靠个人工作树路径
作为团队证据。实施票必须引用 Core Issue、Feishu revision 或
`owner/repo@<full-sha>:<path>`。

## 文档定位与 Spec 生命周期

Wayfinder 管工作状态和跨仓依赖，spec 按知识阶段逐步落位。不得先在某个实现仓库
写一份“最终跨仓合同”，再让其他仓库被动追随。

| 时机 | 要写的 spec | 位置 | 推进条件 |
|---|---|---|---|
| Intake / Grill | 问题、Outcome、非目标、候选方案、Fog、依赖 | Core Map 或对应功能目录下的跨仓 Decision Issue；`stage:protocol-definition` | 决策边界和 owner 已明确；尚不创建猜测性的生产接口 |
| 协议冻结、准备实施 | wire/state machine、authority、原子边界、错误、幂等和兼容停止线 | 同一 Core Decision 的 Decision 段；同步更新 Feishu Protocol 草案及 revision | 各消费仓确认；创建 owning repo delivery children 后进入 `stage:implementation` |
| 仓库实施开始 | 模块 seam、文件影响面、事务、迁移步骤、独立 RED 测试和本仓 gate | OS：`docs/developer_guide/workflow_task_runtime_migration/rounds/<slice>-design.md`；前端：`docs/migration/workflow/<slice>.md`；Scheduler 同理放其 `docs/`，并由各自 delivery Issue 引用 | spec 只解释本仓实现，不重定义 Core 合同；一个 mergeable slice 一张 delivery Issue |
| 联调开始前 | 真实拓扑、fixture、HTTP/SSE 时序、重启/冲突场景、各仓候选 full SHA、通过标准 | Core integration gate Issue；可执行 E2E 放 Core，仓库局部合同测试留在 owning repo | 所有必需 delivery children 已合入候选 SHA；Decision 进入 `stage:testing` |
| 接受 | 最终 Protocol、Implementation mapping、Testing evidence、已知限制 | Feishu 对应 Protocol/Implementation/Testing 页面及 revision；Core 记录 submodule pin 和 CI/evidence URL | 实现、跨仓 E2E、Feishu 和 Core pin 一致后进入 `stage:accepted` 并关闭 Decision |
| 迁移清理前 | 长期维护接口和不变量 | 各 owning repo 的正式 Interface/module 文档与 `AGENTS.md`；不留在本临时目录 | 替代测试存在，旧路由/DTO 删除，临时迁移目录可移除 |

每个 active Decision 只能有一个 `stage:*`。Core 只拥有跨仓 Decision、Map、
integration gate 和 submodule pin；OS、前端、Scheduler/设备适配分别拥有自己的
delivery Issue。设计 Grill 结束、历史 Decision 关闭或本地测试通过，都不等于
`stage:accepted`。

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
[packages/services/src/workflow.ts](https://github.com/Uni-Lab-OS/uni-lab-fe/blob/0fd39af3014b29035ee8e2280b9d753b2b9f96a2/packages/services/src/workflow.ts#L4)，
并由
[createServices.ts](https://github.com/Uni-Lab-OS/uni-lab-fe/blob/0fd39af3014b29035ee8e2280b9d753b2b9f96a2/packages/services/src/createServices.ts#L23)
为所有 authority 装配。

| 旧版 FE 操作 | 旧路由或数据形态 | 新操作 | 处置 | 当前切片 |
|---|---|---|---|---|
| `getWorkflow(workflowId)` | `GET /workflows/{id}/graph`，返回前端自定义的 `WorkflowDocument.revision.canonical` | 共享 Graph 消费方使用冻结 Backend Graph；OS 双视图编辑器通常通过一个 Workflow-scoped Authoring aggregate 完整恢复状态 | 语义迁移 | OS 02D～02G；UI1 Authoring |
| `saveWorkflow(...)` | `PUT .../graph`，携带 Canonical revision 和可选字符串 revision ID | 共享完整 Graph PUT 携带整数 `revision`、Backend Node write 和 Backend Edge write；OS 双视图 Authoring 必须通过 Draft/Candidate/Apply 保持源码同步，不能把 Canonical JSON 当作持久化事实 | 拆分迁移 | OS 02G；UI1 Authoring |
| `validateWorkflow(...)` | `POST /workflows:validate`，携带 Canonical 和 parameters | 持久编辑使用 Draft diagnostics 和 Apply revalidation；非持久 Candidate 可以使用 D-040 `/authoring/validate` | 旧路由已取代，能力语义迁移 | OS 02E/02G；UI1 Authoring |
| `compilePythonWorkflow(...)` | `POST /authoring/compile`，使用 `base_revision_id`、客户端 `source_uri` 和 Canonical 输出 | 保留纯转换路由，但 wire model 改为 `workflow_uuid`、整数 `revision`、稳定 Node/Edge UUID 和 Backend-shaped Graph；持久编辑通过 Workflow-scoped 路由保存完整 Draft | 语义迁移 | OS 02D/02E；UI1 Authoring |
| `generatePythonWorkflow(...)` | `POST /authoring/generate-python`，输入 Canonical | 保留纯转换路由，输入完整 Backend-shaped Candidate Graph，输出确定性的 normalized source | 语义迁移 | OS 02D/02E；UI1 Authoring |
| `validateAuthoringCandidate(...)` | 浏览器把完整旧 Candidate 回传给 OS | 非持久调用方可继续使用纯验证；持久 Apply 只发送一个 opaque `candidate_hash`，服务端重新编译并校验 Draft、Workflow revision 和 Catalog fingerprint，绝不携带 Candidate 内容 | 拆分迁移；旧持久流程已取代 | OS 02G；UI1 Authoring |
| `createRun(request)` | `POST /runtime/runs`，发送完整 Canonical revision 和旧 debug 字段 | 普通执行使用 `POST /workflow-tasks`；OS-only 调试使用独立 `POST /debug/workflow-tasks` 和非空 `start_node_uuids`/`breakpoint_node_uuids`；两者都由 OS 对 persisted Graph 建立 snapshot/plan | UI1D 已从 FE public port 和生产调用删除 | 02H/R1/R2；UI1 Runtime 已完成；DBG 待办 |
| `getRun(runId)` | `GET /runtime/runs/{run_id}` | `GET /workflow-tasks/{task_uuid}` | 语义迁移 | R1；UI1 Runtime |
| `listRunNodes(runId)` | `GET .../runs/{id}/nodes` | `GET /workflow-tasks/{task_uuid}/jobs` | 语义迁移 | R1；UI1 Runtime |
| `listRunEvents(runId, cursor)` | Task-scoped REST event page | 不存在 Task-scoped event 路由；相关全局 SSE 到达后，通过 REST 重新获取一致的 Task/Jobs/debug projection | 已取代 | R1；UI1 Runtime |
| `command(runId, command, payload)` | `POST .../runs/{id}/commands`，随后获取 Run | 普通命令使用 `POST /workflow-tasks/{task_uuid}/commands`；Hold/step family 使用独立 debug command route；成功响应只表示接受，随后重新补读 projection | 语义迁移 | R1A/R1B 与 UI1 普通 command 已完成；DBG 独立 command 待办 |
| `cancelRun(runId)` | 独立的 `POST .../runs/{id}/cancel` | Task command `{type:"cancel", idempotency_key:...}` | UI1D 已删除旧调用 | R1A/R1B durable cancel 与 UI1 真实 OS 联调已完成 |
| `subscribeRunEvents(...)` | 每个 Run 独立的 `/runtime/events` WebSocket；失败后轮询 Run events | 统一使用全局 `GET /events` SSE，支持单调递增 `id`、`Last-Event-ID`、客户端去重和 REST 状态恢复 | UI1D 已删除旧 socket/polling 实现 | R1；UI1 Runtime 已完成 |
| `dispose()` | 关闭 Run socket 和轮询 timer | 释放共享 SSE subscription/service | 直接保留生命周期意图 | UI1 Runtime |

旧方法的完整定义位于
[workflow.ts:168](https://github.com/Uni-Lab-OS/uni-lab-fe/blob/0fd39af3014b29035ee8e2280b9d753b2b9f96a2/packages/services/src/workflow.ts#L168)。
冻结共享路由的依据是 Backend `09609a2` 中的
`internal/http/handler/workflow.go`；目标仓库已经实现的路由位于
[workflow_api.py](https://github.com/deepmodeling/Uni-Lab-OS/blob/1d84efc77f6cd87622e4663c383ce168c5b586fa/unilabos/app/workflow_api.py#L96)。

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
| 旧 debugger command union | 普通 command 仍只有 `step`、`pause`、`resume` 和 `cancel`；OS-only launch/Hold/step-family 只走 D-112～D-116 的独立 debug Interface，不保留旧 alias |

旧 `unwrap()` 同时接受裸对象和 `data` envelope
（[workflow.ts:376](https://github.com/Uni-Lab-OS/uni-lab-fe/blob/0fd39af3014b29035ee8e2280b9d753b2b9f96a2/packages/services/src/workflow.ts#L376)）。
这种宽松 fallback 会掩盖合同漂移，必须删除。新 port 应严格解析
`{"code":0,"data":...}` 和标准 error envelope。

[realtime.ts](https://github.com/Uni-Lab-OS/uni-lab-fe/blob/0fd39af3014b29035ee8e2280b9d753b2b9f96a2/packages/services/src/realtime.ts#L30)
中的 device-status socket 是独立的旧版设备投影。删除 Workflow realtime WebSocket
时，不得把这个设备通道静默改造成 WorkflowTask 通道，也不得在它自己的归属阶段前
一并删除。

## 2. FE 编辑器状态与交互迁移

当前没有独立的权威 Workflow store。Authoring、Run、Node、event 和 debugger
状态全部保存在一个
[WorkflowPanel](https://github.com/Uni-Lab-OS/uni-lab-fe/blob/0fd39af3014b29035ee8e2280b9d753b2b9f96a2/packages/workflow-editor/src/components/WorkflowPanel.tsx#L56)
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
| 前端根据一个 start node 计算可执行子图 | 前端只预览非空 `start_node_uuids` 的范围；实际 scope、Task plan、Jobs 和 `out_of_scope_node_uuids` 由 OS debug projection 决定 | DBG/UI1 语义迁移 |
| runtime 颜色依赖旧 node state 和一个 `pausedBeforeNodeId` | 分别渲染 Job status、Task control status、Breakpoint Hold、disabled 和 out-of-scope | R1/DBG/UI1 |
| output panel 读取旧 `node.result` 和 Run events | 读取 Job result/error 投影和最终 `WorkflowTask.output` | O1/UI1；禁止 partial output |
| UUID 丢失时按 action/type/ordinal 猜测映射 | 删除 identity 猜测；Phase 02 通过稳定 source anchor 保留 Node UUID | 已取代 |
| 纯前端 `useWorkflowDebug` 模拟执行状态 | 删除；渲染持久 Task/Job/Hold 投影 | 已取代 |
| 根据参数字段名猜测 Material reference | 使用 A1 的真实 material Handles 和 M1/M2 的 ResourceSlot/selector 合同替换 | A1/M1/M2/UI1；禁止 heuristic |

主要消费位置：

- 状态和 service 调用：
  [WorkflowPanel.tsx:76](https://github.com/Uni-Lab-OS/uni-lab-fe/blob/0fd39af3014b29035ee8e2280b9d753b2b9f96a2/packages/workflow-editor/src/components/WorkflowPanel.tsx#L76)；
- Run 刷新和订阅：
  [WorkflowPanel.tsx:192](https://github.com/Uni-Lab-OS/uni-lab-fe/blob/0fd39af3014b29035ee8e2280b9d753b2b9f96a2/packages/workflow-editor/src/components/WorkflowPanel.tsx#L192)；
- compile/validate 循环：
  [WorkflowPanel.tsx:475](https://github.com/Uni-Lab-OS/uni-lab-fe/blob/0fd39af3014b29035ee8e2280b9d753b2b9f96a2/packages/workflow-editor/src/components/WorkflowPanel.tsx#L475)；
- 保存和浏览器文件回写：
  [WorkflowPanel.tsx:661](https://github.com/Uni-Lab-OS/uni-lab-fe/blob/0fd39af3014b29035ee8e2280b9d753b2b9f96a2/packages/workflow-editor/src/components/WorkflowPanel.tsx#L661)；
- 旧执行提交和 command：
  [WorkflowPanel.tsx:755](https://github.com/Uni-Lab-OS/uni-lab-fe/blob/0fd39af3014b29035ee8e2280b9d753b2b9f96a2/packages/workflow-editor/src/components/WorkflowPanel.tsx#L755)；
- Canonical 投影和 identity 猜测：
  [canonicalWorkflow.ts](https://github.com/Uni-Lab-OS/uni-lab-fe/blob/0fd39af3014b29035ee8e2280b9d753b2b9f96a2/packages/workflow-editor/src/utils/canonicalWorkflow.ts#L14)。

编辑器不应继续在单体 Panel 中逐个替换调用，而应拆出三个 controller 边界：

1. `WorkflowAuthoringController`：aggregate 状态恢复、本地 dirty buffer、Draft
   双 CAS、Apply 三 token CAS、冲突比较和外部失效通知；
2. `WorkflowTaskController`：Task 创建、Task/Job 状态恢复、全局 SSE 路由和普通
   command；
3. `WorkflowDebugController`：launch configuration、Hold 和 composite-stop
   projection；在 R1/R2、Claims、O1 和 D-112～D-116 OS delivery 完成后加入。

这是模块边界要求，不强制使用某一种 FE 状态管理库。

## 3. 旧版 OS 源码处置

旧版 OS 是行为依据，但它的公共 Canonical/Run 接口不是迁移目标。

| 旧版源码 | 可保留的行为 | 拒绝或已取代的部分 | 目标阶段 |
|---|---|---|---|
| [local_api.py](https://github.com/Uni-Lab-OS/Uni-Lab-OS/blob/f5c10733e7e37218ab5c660ecef9c41bb94c72ab/unilabos/app/local_bridge/local_api.py#L232) | 薄 composition seam、验证和错误处理经验、现有 Authoring/runtime fixture | `/workflows:validate`、Canonical Graph DTO、`/runtime/runs`、独立 cancel/reconcile、每个 Run 的 events 以及 `/runtime/events` WebSocket | 拆分至 01/02/04/05 |
| [runtime/service.py](https://github.com/Uni-Lab-OS/Uni-Lab-OS/blob/f5c10733e7e37218ab5c660ecef9c41bb94c72ab/unilabos/runtime/service.py#L131) | compile-to-plan 编排、dispatch unknown 处理、持久状态优先于 transport 投影、取消保护、reconciliation 和 debug 验证 | `run_id`、Canonical submission、Run projection 和旧公共 command vocabulary | 拆分至 03/04/05 |
| [event_store.py](https://github.com/Uni-Lab-OS/Uni-Lab-OS/blob/f5c10733e7e37218ab5c660ecef9c41bb94c72ab/unilabos/runtime/event_store.py#L106) | terminal/effect/outbox/cursor 原子提交、持久排序、启动恢复 | 使用 `run_id`/旧 node projection 的表，以及旧 Run event wire schema | 语义迁移至 04 |
| [workflow_store.py](https://github.com/Uni-Lab-OS/Uni-Lab-OS/blob/f5c10733e7e37218ab5c660ecef9c41bb94c72ab/unilabos/runtime/workflow_store.py#L24) | revision conflict 经验和重启持久性测试 | `~/.unilabos/workflows` 下每个 Canonical 一个文件的 authority | 由 01/02 和 D-081 取代 |
| [schedule_ws.py](https://github.com/Uni-Lab-OS/Uni-Lab-OS/blob/f5c10733e7e37218ab5c660ecef9c41bb94c72ab/unilabos/app/local_bridge/schedule_ws.py#L72) | 内部 submit/cancel/debug transport 行为和迟到事件处理 | 前端公共 parity、Run projection，以及复制 Backend Edge protocol 的要求 | 语义迁移至 04/06 |
| [offline_os.py](https://github.com/Uni-Lab-OS/Uni-Lab-OS/blob/f5c10733e7e37218ab5c660ecef9c41bb94c72ab/unilabos/app/local_bridge/offline_os.py#L48) | 确定性执行、debug、cancel 测试行为 | 公共 fake Run authority | 作为 03/04/05/06 的语义 fixture 来源 |
| `unilabos/workflow/{authoring,canonical,canonical_ir,contracts,dag_compile}.py` | no-exec AST parsing、normalization、source map、控制流和参数行为 | 公共 Canonical DTO 和旧字符串 identity | 语义迁移至 02/03 |

旧 bridge 路由集中在
[local_api.py:1218](https://github.com/Uni-Lab-OS/Uni-Lab-OS/blob/f5c10733e7e37218ab5c660ecef9c41bb94c72ab/unilabos/app/local_bridge/local_api.py#L1218)
至
[local_api.py:1596](https://github.com/Uni-Lab-OS/Uni-Lab-OS/blob/f5c10733e7e37218ab5c660ecef9c41bb94c72ab/unilabos/app/local_bridge/local_api.py#L1596)。
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
| [services workflow tests](https://github.com/Uni-Lab-OS/uni-lab-fe/blob/0fd39af3014b29035ee8e2280b9d753b2b9f96a2/packages/services/src/workflow.test.ts#L25) | 使用严格 Graph、Authoring、Task、Job、Task command 和全局 SSE 合同测试，替代 Canonical/Run 路由断言 |
| [canonicalWorkflow tests](https://github.com/Uni-Lab-OS/uni-lab-fe/blob/0fd39af3014b29035ee8e2280b9d753b2b9f96a2/packages/workflow-editor/src/utils/canonicalWorkflow.test.ts#L13) | 迁移有价值的 Graph/view 投影断言；删除猜测 identity 的 remap；多起点按 DBG，临时 Conditional Join 按 J1 分别进入 |
| [debugControls tests](https://github.com/Uni-Lab-OS/uni-lab-fe/blob/0fd39af3014b29035ee8e2280b9d753b2b9f96a2/packages/workflow-editor/src/utils/debugControls.test.ts#L31) | 按四种共享 Task command 重写；独立增加 D-112～D-116 的 launch/Hold/step-family 合同测试 |
| [workflow-runtime E2E](https://github.com/Uni-Lab-OS/uni-lab-fe/blob/0fd39af3014b29035ee8e2280b9d753b2b9f96a2/e2e/workflow-runtime.spec.ts#L5) | 拆分为持久 Authoring、普通 Task 执行和 debugger 套件；删除所有旧路由断言 |
| [workflow import/persistence E2E](https://github.com/Uni-Lab-OS/uni-lab-fe/blob/0fd39af3014b29035ee8e2280b9d753b2b9f96a2/e2e/workflow-import-persistence.spec.ts#L6) | 保留选择恢复；Python 流程改为已保存 Draft、未应用预览和显式 Apply |
| [workflow cloud import E2E](https://github.com/Uni-Lab-OS/uni-lab-fe/blob/0fd39af3014b29035ee8e2280b9d753b2b9f96a2/e2e/workflow-cloud-import.spec.ts#L7) | 迁移为显式服务端 import 和 Backend Graph；删除浏览器 source authority 以及旧 validate/Run 路由 |
| [debug action E2E](https://github.com/Uni-Lab-OS/uni-lab-fe/blob/0fd39af3014b29035ee8e2280b9d753b2b9f96a2/e2e/workflow-debug-actions.spec.ts#L38) | 迁移普通 `pause/step/resume/cancel`，并用独立 debug Interface 覆盖 launch/Hold/step family；不保留 alias |
| [debug scenario E2E](https://github.com/Uni-Lab-OS/uni-lab-fe/blob/0fd39af3014b29035ee8e2280b9d753b2b9f96a2/e2e/workflow-debug-scenarios.spec.ts#L41) | 保留 Code/DAG 视觉同步意图；用 Task/Jobs/SSE 和 D-112～D-116 launch/Hold/composite projection 替换 Run/events |
| [unsaved guard E2E](https://github.com/Uni-Lab-OS/uni-lab-fe/blob/0fd39af3014b29035ee8e2280b9d753b2b9f96a2/e2e/workflow-unsaved-guard.spec.ts#L3) | 保留并加强：外部 SSE 失效通知或 CAS conflict 不得替换或清除 dirty buffer |
| [layout isolation E2E](https://github.com/Uni-Lab-OS/uni-lab-fe/blob/0fd39af3014b29035ee8e2280b9d753b2b9f96a2/e2e/workflow-layout-isolation.spec.ts#L5) | 原样保留；它不是 API 迁移测试 |

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
| `test_unified_runtime_api.py` | 旧公共路由由共享 route 测试、R1 runtime 行为测试、DBG 测试和 UI1 真实 FE E2E 替换 |

Phase 04 下其余 source-only runtime 测试仍是有价值的行为依据，但所有
Run/table/route 断言都必须语义转换为 Task/Job/event identity。

## 5. Phase 02H 起整体交付计划

旧 Phase 03～09 只保留为历史来源分类，不再作为执行顺序。Material 不能等到
Scheduler 和 device execution 之后才实现，前端也不能等所有 OS 功能结束后一次性
迁移。上文各表的“归属阶段”列按历史 provenance 阅读；实际 owner、依赖和接受门
以本节为准。

### 5.1 Phase 02H 边界修正

Phase 02H 只关闭 Task Input Contract 的通用 preflight：

- 从 persisted Workflow metadata 读取 ordered Input Contract；
- 完成 unknown/missing/default/null/type/constraint 校验；
- 规范化 scalar、opaque object 及其 list；
- 在创建任何 Task/Job 前拒绝无效输入；
- 将 resolved input 写入 `WorkflowTask.input` 和 immutable snapshot；
- 通过真实 Handle UUID 把 Workflow input binding 投影到 Task-scoped Job
  `param`，不修改 persisted Node `param`；
- 定义 ResourceSlot resolver port 和失败分类的仓库内合同测试，但不在缺少
  production Material Module 时宣称非空 ResourceSlot 已可执行。

原计划把完整 Material lookup、Reservation、Claim、Disposition 和删除保护同时
写进 02H，却又在“不在本计划内”排除了 local Material authority。这一矛盾按如下
方式消解：显式 ResourceSlot 的 production 解析和 Task Reservation 移入 M1；
MaterialSource 自动选择和创建移入 M2。M1 完成前，带 ResourceSlot 的 Task 不得部分
持久化、不得使用旧 Inventory fallback，也不得被当成 02H 退出证据。

02H 是 OS repository-local delivery。若它不改变已冻结的共享 wire contract，不新建
Core Decision；其 delivery Issue 以 `Uni-Lab-OS/Uni-Lab-Core#133` 为主父项并
链接 `Uni-Lab-OS/Uni-Lab-Core#134`。本轮实现前写
`rounds/02h-task-input-preflight-design.md`，先由独立测试作者提交 RED，再进入实现。

### 5.2 功能切片、仓库 owner 与联调门

| 切片 | OS delivery | 前端 delivery | Scheduler/设备 delivery | 联调与 Spec 落位 |
|---|---|---|---|---|
| **02H Input preflight** | ordered contract、严格规范化、snapshot、Job param binding；ResourceSlot resolver seam | 仅同步最终 Task input DTO/错误展示，不提前做 Material selector | 无 | OS round spec；真实 OS API 覆盖 scalar/default/null、ResourceSlot injected 404/409 分类和 production fail-closed，不单独设置跨仓接受门 |
| **A1 Action typed contract** | D-100～D-108：参数/具名结果、真实 Backend Handles、material port、variable selector、默认值、结果归一化；退役 `@action(handles=...)` | Catalog 驱动端口、参数表单和 selector；删除字段名/ordinal 猜测 | 无 | `Uni-Lab-OS/Uni-Lab-Core#135` 下保持 active Decision，分别创建 OS/FE children；联调验证 Catalog fingerprint、Python/JSON round-trip 和 UI 投影 |
| **I1 Workflow I/O** | Input/Output Contract、input/output bindings、Executable Node Contract 和 schema/codec | Workflow input/output 编辑、校验和 Task 表单 | 无 | `Uni-Lab-OS/Uni-Lab-Core#133`；协议冻结后分别写 OS/FE implementation spec，联调 contract/default/ResourceSlot codec |
| **C1 Composite authoring** | persistent Composite Invocation、稳定 boundary Handles/mappings、transparent/gated 声明 | Composite node、边界端口、映射和模式编辑 | 无 | `Uni-Lab-OS/Uni-Lab-Core#136`；依赖 A1/I1，做保存/刷新/生成 Python 往返联调 |
| **M1 Material/Site authority foundation** | Material、Site、Warehouse、Disposition、软删除、Task Reservation、Job Claim、幂等 ChangeSet 的持久权威；显式 ResourceSlot production resolver | 只消费已冻结 Material/ResourceSlot DTO；不拥有分配、锁或错误分类 | Claim 中的 device identity/可用性 adapter；不拥有 Material 真值 | `Uni-Lab-OS/Uni-Lab-Core#134` 下创建或复用 active implementation Decision；OS spec 先冻结 schema/transaction/lock order，Core integration spec 覆盖争用、重启和 400/404/409 |
| **M2 MaterialSource admission v1** | 非执行 MaterialSource declaration node；单一固定 template、可选具体 Material、一个 ResourceSlot 输出；创建/选择/Reservation 全有或全无 | MaterialSource、SiteSelector、CandidateSiteSet 编辑；进度/Site occupancy 等 `Uni-Lab-OS/Uni-Lab-Core#145` | Scheduler 只接收稳定 Site UUID 集合；不解析前端索引语法 | `Uni-Lab-OS/Uni-Lab-Core#140`、`Uni-Lab-OS/Uni-Lab-Core#141`、`Uni-Lab-OS/Uni-Lab-Core#142` 仍处 `stage:protocol-definition` 时只写 Core/Feishu Protocol spec；冻结并建 delivery children 后才写各仓 implementation spec。`Uni-Lab-OS/Uni-Lab-Core#143～#146` 未冻结部分不得占位实现；`Uni-Lab-OS/Uni-Lab-Core#148` 延期 |
| **R1A Task command durable ingress** | 已完成四种共享 command 的 Backend-shaped 201 envelope、Handler UUID binding、pending record、SQLite 约束、同 Task key 幂等/冲突和重启持久化；不消费 command | 无；只冻结前端未来要消费的 wire，不在本轮改 FE | 无 | Core Decision `Uni-Lab-OS/Uni-Lab-Core#150`；OS delivery `deepmodeling/Uni-Lab-OS#302`；本仓 spec/trend 位于 `rounds/r1a-*`，因为无浏览器界面不单设 E2E gate |
| **R1B Durable runtime kernel** | 已完成并 non-squash 合入：受测/受审候选 `6cc9390`，merge `c540337d`；FIFO command、Task/Job 状态机、journal/outbox、feedback、unknown/reconcile、重启恢复、唯一 `workflow.runtime.changed`；不含 DAG/device | UI1A service port `5ca7cd2`、UI1B 纠偏 `e864e49`、UI1C resilience `eb5e2a3`、UI1D final candidate `27212c7` 已完成，当前 FE integration `bb0bb24`；原起点/断点/gutter、Feedback/Output/Debugger surface 均继续复用，旧 Run/WS/polling 已删除 | transport session 只是执行投影，不成为终态权威 | Core #150；OS/FE integration 已推送；FE #6 delivery 已形成可远端解析候选；Core #152 真实 final gate、submodule pin 与团队证据已发布，等待 exact-SHA 独立 review 和 Feishu Testing 同步 |
| **R2 Admission、ExecutionPlan 与 sole coordinator** | 从 immutable Task snapshot 生成计划；derived Edge resolution；资源 readiness；完整 Reservation 后 admission；ready Job dispatch 前完整 Claim | 展示 pending/等待原因；不得运行 DAG walker 或乐观写终态 | 若外部 Scheduler 参与，只接收 versioned plan/约束并返回建议；OS coordinator 保留唯一 readiness/admission/terminal owner | Core 对跨 OS/Scheduler 边界建 Decision；OS 与 Scheduler 各自 delivery spec；Core E2E 验证 duplicate request、contention、restart 和单一 owner |
| **D1 Device execution 与 result commit** | RobotCommand、Mutation Session、baseline/增量 ChangeSet、显式和隐式结果归一化、Fenced Claim/reconciliation | 展示 running、reconciling、结果和可行动错误 | device adapter/driver 实现厂商协议和 query/reconcile，不解释 Workflow graph | Core integration gate 固定 fake 与真实 driver fixture；OS/设备各自仓库写实现 spec；未知物理结果不得被 HTTP success 覆盖 |
| **O1 Composite runtime 与 Task output** | Planner lowering；transparent/completion-gated readiness；Composite 本身无 Job；成功时原子写完整 Task output，其他状态为 `{}` | 展示 composite frame、真实内部 Job 和最终 output；不展示 partial output | 无新增 owner | `Uni-Lab-OS/Uni-Lab-Core#136`；联调覆盖 transparent frame、gated completion、ResourceSlot output 和 SSE/REST rehydration |
| **UI1 前端纵向迁移** | 提供最终 Authoring、Catalog、Task/Job、Material 和 debug Interface | FE-D117 Authoring、UI1A service port、UI1B 原 UI/controller + 起点/断点/gutter 复用 + 真实 OS happy path、UI1C feedback/fault/restart、UI1D 旧接口退役/final gate 均已完成；真正 Debug launch/multi-start/Hold 仍归 DBG，Catalog/device execution 归 A1/D1 | 无 | 总览 `Uni-Lab-OS/uni-lab-fe#2`；实现票 `#3～#6`；UI1D spec 为 `docs/migration/workflow/ui1d-runtime-final-gate.md`；Debugger 为 FE `#1` / Core `#6/#137`；HTTP/SSE integration gate 为 Core `#152` |
| **DBG Debugger** | D-112～D-116：debug launch/projection、durable Holds、scoped permits、causal step fences、冻结 source/composite projection | launch/frontier/breakpoint、Hold scope、step/continue/step-over/out、三类未执行状态和固定文案 | Claim/admission 继续服从 R2，不给 debugger 第二个 scheduler | 使用 `deepmodeling/Uni-Lab-OS#299`、`Uni-Lab-OS/uni-lab-fe#1` 和 `Uni-Lab-OS/Uni-Lab-Core#137`；Core spec 覆盖 multi-start、branch-local、composite、SSE restart、OS restart、409 stale snapshot |
| **J1 Conditional Join** | 只实现已冻结的临时 `compute` Join、最多 16 输入、确定性 codec/round-trip 和 runtime lowering | 对应临时 Join 编辑与错误展示 | Scheduler 消费 lowering 后计划，不拥有 Join 语法 | `Uni-Lab-OS/Uni-Lab-Core#132` 建 delivery children 和联调测试；正式 Backend Join 继续延期，不把临时表示写成长期公共扩展 |
| **X1 退役与接受** | OS 已删除 `/runtime/runs`、Run identity 和已取代 local bridge；其他 scheduler/plan 兼容清理由所属后续阶段负责 | UI1D 已删除 Run DTO/client/hook、旧 socket、轮询 fallback、旧 fallback panel 和 local bridge E2E；静态门禁记录 163 个 production 文件、15 个退役文件、0 个旧引用 | 删除被新 plan/claim 合同取代的兼容锁或桥 | Core #152 记录各仓 full SHA、E2E artifact 与 submodule pin；独立 review、Core/Feishu Testing 对齐后才能推进 `stage:testing/accepted` |

Tool Call `Uni-Lab-OS/Uni-Lab-Core#138` 整体延期；`manual_confirmation` 仍属于
R1 的共享 Runtime 行为，不得因 Tool Call 延期而阻塞。Material 进度/Site
occupancy UI 必须等待 `Uni-Lab-OS/Uni-Lab-Core#145`，Backend 预分配后交给 OS
的跨 Authority 事务必须等待 `Uni-Lab-OS/Uni-Lab-Core#144`，
`apply_deduct_resource` 的去留必须等待 `Uni-Lab-OS/Uni-Lab-Core#146`。

### 5.3 推荐依赖与并行关系

```text
02H
 ├─ A1 ──┐
 ├─ I1 ── C1
 ├─ M1 ── M2
 └─ R1A ── R1B
          │
A1 + I1 + C1 + M1 + R1B
          ▼
         R2 ── D1 ── O1 ── DBG
          └─────────────── J1

UI1 随每条已稳定 Interface 纵向进入；X1 只能最后执行。
```

M1 和 R1A/R1B 可以在 02H 后与其他不冲突切片并行；R1A/R1B OS delivery、UI1A
service port、UI1B 原 UI/controller + 真实 OS happy path、UI1C
feedback/fault/restart hardening 和 UI1D 退役/final gate 已完成，下一 Runtime 顺序是
Core #152 exact-SHA review/Testing 对齐 → R2；R2 仍依赖 A1、I1、C1 和 M1
的可执行合同。M2 受 active protocol Decision 阻塞。R2 可以先对
纯 scalar Action 建立执行链，但任何消费 ResourceSlot 的成功路径必须等 M1；
任何自动选择 Material 的成功路径必须再等 M2。Debugger 在 R1/R2、Claims、
Composite runtime 和一致 projection 全部可用之后进入真实联调。

### 5.4 Wayfinder 落票规则

1. `Uni-Lab-OS/Uni-Lab-Core#1` 只维护 Outcome、功能顺序、Frontier、Blocked 和 Fog，不塞入每个仓库
   的文件清单。
2. 每个跨仓合同只在对应功能目录下保留一个 active Decision；历史 D-001～D-116
   目录票只作 provenance，不能作为“已实现”票复用。
3. OS、前端、Scheduler/设备各自创建 repository-local delivery child；跨仓依赖
   使用 native dependency 或完整 Issue URL，不能用裸跨仓 `#number`。
4. 每个 delivery child 只覆盖一个 mergeable slice，并链接本仓 implementation
   spec、测试 commit、PR/full SHA 和 CI。D-096 的独立测试作者、完整 suite、精确
   SHA review 和不 squash provenance 继续适用于 OS migration round。
5. Core integration gate 只在协议已冻结且 delivery children 明确后创建；进入
   `stage:testing` 时固定候选 full SHA，任何代码变化都使对应 review/E2E 证据失效。
6. 只有 merged delivery、跨仓 E2E、Feishu 接受版本和 Core submodule pin 一致，
   才把 Decision 标为 `stage:accepted` 并关闭。

### 5.5 当前落票清单

| 功能 | Primary Wayfinder 位置 | 需要的 delivery / integration ticket |
|---|---|---|
| 02H | `Uni-Lab-OS/Uni-Lab-Core#133`；关联 `Uni-Lab-OS/Uni-Lab-Core#134` | 一个 `deepmodeling/Uni-Lab-OS` delivery；共享 wire 不变时不新增 Core Decision |
| A1 | `Uni-Lab-OS/Uni-Lab-Core#135` | 一个 OS child、一个 `Uni-Lab-OS/uni-lab-fe` child、一个 Catalog/editor integration gate |
| I1 | `Uni-Lab-OS/Uni-Lab-Core#133` | 分开的 OS/FE children；不能和 C1 合成一个不可独立合并的 delivery |
| C1、O1 | `Uni-Lab-OS/Uni-Lab-Core#136` | authoring、runtime/output 分成两轮 OS delivery；各自配 FE child，O1 另有 runtime E2E |
| M1 | `Uni-Lab-OS/Uni-Lab-Core#134` | 历史 D-093～D-099 不重新打开；在功能目录下建立 active implementation Decision，再挂 OS child 和 contention/restart integration gate |
| M2 | `Uni-Lab-OS/Uni-Lab-Core#140`、`Uni-Lab-OS/Uni-Lab-Core#141`、`Uni-Lab-OS/Uni-Lab-Core#142` | 保持各自 Decision 身份；协议冻结后按 MaterialSource、Admission Reservation、Deck/Warehouse 分开建 delivery，互相用 dependency 连接 |
| R1、UI1 Runtime | 全局 Map `Uni-Lab-OS/Uni-Lab-Core#1`；功能目录 `Uni-Lab-OS/Uni-Lab-Core#130`；active Decision `Uni-Lab-OS/Uni-Lab-Core#150` | R1A OS child `deepmodeling/Uni-Lab-OS#302` 和 R1B OS child `deepmodeling/Uni-Lab-OS#303` 已进入组织仓库 integration；FE umbrella `Uni-Lab-OS/uni-lab-fe#2` 下 UI1A `#3`、UI1B `#4`、UI1C `#5`、UI1D `#6` 均有已推送 integration；Core #152 已冻结 spec，真实 final gate、submodule pin 与团队证据已发布，等待独立 review 和 Feishu Testing 同步 |
| R2 | 资源 Admission 主挂 `Uni-Lab-OS/Uni-Lab-Core#134`，控制流关联 `Uni-Lab-OS/Uni-Lab-Core#132` | 若接入外部 Scheduler，先建跨 OS/Scheduler Decision，再分别建 OS 与 `Uni-Lab-OS/uni-lab-scheduler` children |
| D1 | 结果合同主挂 `Uni-Lab-OS/Uni-Lab-Core#135`，Material effect 关联 `Uni-Lab-OS/Uni-Lab-Core#134` | OS runtime/adapter delivery 与设备 package delivery 分开；Core gate 固定 fake/real driver fixture |
| DBG | `Uni-Lab-OS/Uni-Lab-Core#131` | 复用 `deepmodeling/Uni-Lab-OS#299`、`Uni-Lab-OS/uni-lab-fe#1` 和 `Uni-Lab-OS/Uni-Lab-Core#137`，不另造平行 Debugger umbrella |
| J1 | `Uni-Lab-OS/Uni-Lab-Core#132` | OS compiler/runtime child、FE editor child和临时 Join integration gate |
| X1 | `Uni-Lab-OS/Uni-Lab-Core#126`、`Uni-Lab-OS/Uni-Lab-Core#2`、`Uni-Lab-OS/Uni-Lab-Core#125` | Core cleanup/acceptance gate；记录各仓 full SHA、submodule pin、Feishu revision 和删除旧接口的替代测试 |

`Uni-Lab-OS/Uni-Lab-Core#1` 的 Frontier 只需要按上述顺序指向当前第一个未阻塞的
Decision/delivery；不要把整张实现表复制进 Map body。若一个功能同时关联两个目录，
选择唯一 primary parent，另一个使用完整 Issue URL 关联，避免双重状态权威。

## 6. 完成条件

只有满足以下全部条件，FE–OS 交互迁移才算完成：

- 本矩阵每一项都已实现、由替代测试明确取代，或仍由一个明确 owner、依赖和
  `stage:*` 的 active Issue 负责；
- 产品 FE 中不存在 `/api/v1/runtime/runs`、`/api/v1/runtime/events`、
  `run_id`、`WorkflowRun` 或公共 Canonical-v2 identity；
- 新 OS 公共模块中不存在旧 Run 路由或 wire alias；
- 持久 Apply 请求无法表达 Candidate、Graph 或 source 内容；
- 浏览器代码无法把已注册 package Draft 作为 authority 直接读写；
- 全局 SSE 可恢复，并且只负责使持久 REST 状态失效；
- Task 创建绝不携带 DAG，并且始终对当前 Applied Graph 建立快照；
- 普通 Task command 保持幂等，并返回冻结的 command record；
- dirty buffer conflict 和外部修改行为通过真实 browser-to-OS E2E；
- 迁移后的 E2E 使用真实 OS 接口，不依赖旧 local bridge mock 或兼容路由；
- ResourceSlot 成功路径使用 production Material Module、完整 Task Reservation 和
  Job Claim，不依赖旧 Inventory 或字段名 heuristic；
- Composite、Task output、Debugger 和临时 Conditional Join 均有对应的 OS/FE
  delivery 证据及 Core integration gate；
- 相关跨仓 Decision 已同步 Feishu Protocol/Implementation/Testing revision，Core
  已固定通过 E2E 的各仓 full SHA/submodule pin，并进入 `stage:accepted`；
- 长期合同已迁入正式 Interface/module 文档，本临时目录只保留可删除的迁移 provenance。
