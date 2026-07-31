# Backend 工作流设计与 OS 迁移决策对比

## 结论

本报告以飞书《HTTP API 服务与工作流调度服务架构设计方案》
`document_id=KhPId3GNco8qCjxzT9Jccujknmc`、`revision_id=12` 为主设计来源，
并逐项核对 Backend 冻结提交 `feat/workflow@09609a2` 的实际代码。根据 D-058，
本报告只判断 Backend 面向前端的 HTTP/SSE Interface；Backend 与 Edge Agent
之间的通信协议和内部部署实现不作为 OS 前端接口的对齐对象。

整体判断如下。

1. D-033、D-034、D-043、D-045 已经追上冻结 Backend 的图编辑接口、
   revision、响应信封和参数默认值。D-014～D-017、D-021～D-023、D-026
   已完全废弃；D-004、D-010、D-011、D-013、D-018～D-020 等决策只保留
   状态标记明确指出的
   部分，不能再从旧正文恢复已失效规则。
2. Python 编写、源码生命周期、源码映射、起始前沿、断点 Hold、显式
   Conditional Join 都是 OS-only 扩展，不是 Backend 已有能力。它们可以保留，
   但不能被称为“完全镜像 Backend”。
3. Backend 的 Edge 注册、HTTP/WS 数据与控制面、Command/Inbox、Job Token、
   ACK/重放、Session 对账、设备锁和 PostgreSQL 部署约束全部排除在前端接口
   对齐范围外。它们不再作为 OS 迁移的“遗漏项”或 parity gate。
4. D-041 选择 SQLite 作为 OS 本地 Workflow 与执行事实权威，是 OS 私有实现。
   它不需要复制 Backend 的 PostgreSQL 进程拆分或 Edge 可靠通信；但其输出的
   前端可见 Task/Job 状态、REST 行为和 SSE 投影仍须匹配确认后的前端合同。
5. 飞书页面当前 revision 12，但正文仍自述其实现基线是 2026-07-28 的
   `5c05941`；本次审查冻结的是 2026-07-29 的 `09609a2`。因此必须区分
   “页面版本 rev12”与“页面所描述的代码版本 5c05941”。文档仍是总体架构
   参考，具体 DTO、路由和实现行为以冻结源码为准。Backend 后来的分支状态
   未经重新冻结，不自动改变本轮合同。
6. 飞书与冻结源码本身也有偏差：文档列出了
   `workflow_task_edge_state`，源码却没有该表、模型或迁移，Edge 激活状态只在
   Reconcile 时派生；文档节点类型漏掉当前代码已有的 `tool_call` 和
   `manual_confirm`。

因此，后续只讨论真正的 OS-only 设计缺口：Input/Output Authoring 的剩余
表示、Draft/Apply 持久接口、断点 Hold、条件 AST/Conditional Join，以及
模板目录和 OS 节点执行支持。Backend 已有的前端合同直接镜像，不再逐项 grill；
Backend 与 Edge Agent 如何通信不参与本轮接口设计。

## 来源与有效期

### 主要来源

- [飞书原始设计文档](https://dptechnology.feishu.cn/wiki/YSlDwYk89iwCGGk8uF1chjoDngh)：
  通过 `lark-cli docs +fetch` 读取当前 revision 12；标题为
  《HTTP API 服务与工作流调度服务架构设计方案》，整理日期 2026-07-28，
  页面正文仍声明对应 `feat/workflow@5c05941`。revision 12 是页面版本，
  不代表正文已经更新到本次冻结 Backend 提交。
- Backend 冻结源码：`/home/xiongyanfei/uni-lab-backend-github`，
  `feat/workflow@09609a2`，提交时间 2026-07-29。
- Backend 当前架构说明：
  `/home/xiongyanfei/uni-lab-backend-github/docs/architecture/workflow-scheduler.md`。
- Backend 最新提交复核：
  `origin/feat/workflow@99c70bd`（2026-07-30），详见
  [latest_backend_workflow_material_audit.md](./latest_backend_workflow_material_audit.md)。
- 历史接口评审文档：
  `/home/xiongyanfei/tangshaodong/backend-api-review/10-workflow-template.md`、
  `11-workflow-graph.md`、`12-workflow-execution.md`，以及物料、物料状态、
  位置与设备模板文档。
- 被审查决策：[decisions.md](./decisions.md)，范围 D-001～D-100。

### 来源优先级

冲突时采用以下顺序：

1. 用户已确认且仍未被后续决策替代的迁移决策，特别是 D-058 的前端接口边界；
2. Backend 冻结 `feat/workflow@09609a2` 的公开 Handler、DTO、Service、
   Repository、Migration 和测试；
3. 飞书 revision 12（正文描述 `5c05941`）的总体架构与业务语义；
4. Backend 当前架构文档；
5. 10～12 等旧接口评审文档。

10～12 不能继续被笼统地称为“未变更合同权威”：其中 12 明确描述的是尚无
DAG Scheduler 的旧实现，直接 Task/Job 状态写接口和外部创建 Job 接口已经被
当前设计删除；10 中直接维护 NodeTemplate/HandleTemplate 的接口也已被
ResourceTemplate 聚合生命周期替代。

## Backend 整体设计与当前迁移方向

| 设计面 | 飞书 revision 12（描述 5c05941）/ 冻结 Backend | D-001～D-100 | 判断 |
|---|---|---|---|
| 进程边界 | HTTP 与 Scheduler 分进程，共享 PostgreSQL；无强依赖同步 RPC | D-058 明确排除 | 不作为 OS 前端接口 |
| 定义写权 | 前端通过 HTTP 写 Workflow/Node/WorkflowEdge | D-027、D-034、D-058 | 一致 |
| 执行写权 | 前端只创建 Task/Command，不直接写 Task/Job 状态 | D-021、D-024、D-046 | 前端权限一致；内部写入方不要求镜像 |
| 图编辑 | 单实体 CRUD、Node PATCH、batch-delete、revision-guarded graph PUT | D-033、D-034 | 一致 |
| Task 创建 | graph PUT 只保存；`POST /workflow-tasks` 创建快照和执行 | D-013、D-033、D-034 | 最终一致 |
| 运行时输入 | Handler 接收但 Service 忽略 `input` | D-059 先实现真正的 Workflow Input Contract | 已确认为 OS-only 执行扩展；Backend authority 不得静默丢值 |
| 执行图 | disabled/group 裁剪、必填输入、单目标 Handle 单入边、稳定拓扑 | D-048、D-050、D-054、D-056 | 部分一致 |
| 条件与汇合 | 条件可多选；active/inactive 派生；多 active 入边为 AND | D-054～D-057 | 有意收窄为 Python 独占分支，并增加 OS-only Join |
| 调试 | Backend 只有 normal/step/single_node 和 step/pause/resume/cancel | D-046～D-053 | Backend 核心一致；起始前沿与断点为 OS-only |
| 设备路由 | Backend 内部 Edge binding 与执行锁 | D-058 明确排除 | 不作为 OS 前端接口；仅 `material_uuid` 等前端 DTO 在范围内 |
| Edge 协议 | HTTP/WS、Command/Inbox、ACK、重放、对账 | D-058 明确排除 | 不作为 OS 前端接口 |
| 反馈与人工介入 | 前端 REST 查询/决定与 SSE 通知 | D-024、D-025、D-058 | 前端面一致；Edge 上下行闭环不比较 |
| 失败与取消 | 前端可见状态、cleanup 字段和 cancel Command | D-058 要求前端精确兼容 | 直接镜像，不再作为设计决策 |
| 调度并发 | Worker Pool、FIFO、公平性与容量上限 | D-058 明确排除 | 私有实现，不作为前端接口 |
| 存储 | Backend Scheduler 用 PostgreSQL | D-041 本地用 SQLite，D-058 排除实现 parity | 私有实现，不作为前端接口 |
| 模板/动作 | ResourceTemplate 聚合拥有动作、NodeTemplate、Handle 生命周期 | D-018、D-032、D-042 | 身份原则一致，聚合删除/同步语义仍需补充 |
| Python 编写 | Backend 不提供工作流级 Python 编写或源码存储 | D-011～D-012、D-029～D-040 | OS-only 扩展 |
| 可观测与验收 | OpenTelemetry/SigNoz、真实 PostgreSQL E2E、故障注入 | D-058 排除 Backend 内部实现 parity | 不从 Backend Edge/部署设计迁移；OS 自身功能仍需测试 |

## D-001～D-100 逐项判定

状态含义：

- **一致**：可直接采用 Backend 当前语义；
- **OS-only 扩展**：可保留，但必须在路由、DTO、存储或能力展示上与 Backend
  能力明确分层；
- **冲突/过时**：已被后续决策替代，或与当前 Backend 不一致；
- **部分一致**：核心方向一致，但有边界、字段或实现差异；
- **部分废弃**：只保留决策状态说明点名的部分，其余旧正文无实施权威；
- **迁移治理**：不是 Backend 业务接口决策；
- **当前代码偏差**：决策与设计方向一致，但当前 Backend 自身存在例外。

| 决策 | 判定 | 与 Backend 的差异及实施结论 |
|---|---|---|
| D-001 | 迁移治理 | 固定高京 reviewed baseline，与 Backend 业务设计无直接冲突。 |
| D-002 | 迁移治理 | “测试并集”指两个 OS 实现已有功能的测试并集；不因前端接口对齐而迁移 Backend 的 PostgreSQL 或 Edge 通信 E2E。 |
| D-003 | 迁移治理 | 合理；两个仓库无共同祖先时按功能迁移。 |
| D-004 | 部分废弃 | 用户已确认决策继续有效；“未覆盖细节默认采用 10～12”已经由 D-058 废弃。12 的直接状态写/外部 Job 创建和 10 的模板直写均不能恢复。 |
| D-005 | 部分一致 | 表名、主键和旧 Run 词汇清理正确。但公开 `WorkflowNodeJob` 返回字段实际是 `workflow_task_uuid/workflow_node_uuid`，不能把关系字段泛化成局部变量使用的 `task_uuid/node_uuid`。 |
| D-006 | 迁移治理 | 与 Backend 设计无关，可直接采用。 |
| D-007 | 迁移治理 | 可采用；门禁按 OS 自身选定的 SQLite、本地 Scheduler 和 Driver 实现设计，不复制 Backend 的 PostgreSQL/Edge 门禁。 |
| D-008 | 迁移治理 | 可采用；本报告属于最终会被提炼的临时迁移文档。 |
| D-009 | 迁移治理 | Phase 00 的只读边界合理。 |
| D-010 | 部分废弃 | revision-guarded full graph PUT 保留；“编辑器唯一保存接口”已废弃。Backend 同时保留 Node PATCH、单实体 CRUD 和 batch-delete。 |
| D-011 | 部分废弃／OS-only | 真实持久 action/control Node 的 Python UUID anchor 保留；source-only parallel/Fork/Join 不再是 Node，也没有 UUID。 |
| D-012 | OS-only 扩展 | 纯 `from_python_script` 编译边界合理，Backend 无此接口。 |
| D-013 | 部分废弃 | “图保存与执行分离”保留；`PATCH /workflows/{uuid}/graph` 已废弃，由 D-034 的 graph PUT/实体写路由替代。 |
| D-014 | 冲突/过时 | 不添加 revision 的决定已经错误并被 D-033 替代。当前 Workflow 有 `revision`。 |
| D-015 | 冲突/过时 | `update_time` 作为全图并发 token 不符合当前实现，已被 D-016、D-033 替代。 |
| D-016 | 冲突/过时 | last-write-wins 已被当前 revision CAS 和 D-033 替代。 |
| D-017 | 冲突/过时 | 所描述的 graph PATCH 不存在；Node PATCH 和 batch-delete 才是冻结合同的局部编辑接口。 |
| D-018 | 部分废弃 | graph write 不拥有模板、graph read 返回模板集合的规则保留；“PUT 携带 Workflow write model”已由 D-034 废弃，请求只写 `revision/nodes/edges`。 |
| D-019 | 部分废弃 | graph PUT 的 Node/Edge 使用真实稳定 UUID，并按 UUID 原位 upsert/soft-delete；其中 graph PATCH 的措辞已随 D-034 废弃。 |
| D-020 | 部分废弃 | 稳定实体 UUID 与时间字段只读保留；请求体内的 `Workflow.uuid` 和 Node `workflow_uuid` 已废弃。graph PUT 由路径确定 Workflow，请求仅为 `revision/nodes/edges`。 |
| D-021 | 冲突/过时 | 执行语义方向与飞书一致，但固定提交 `5c05941` 已先后被 D-044/D-058 淘汰。禁止恢复旧状态写接口仍由后续决策保留。 |
| D-022 | 冲突/过时 | 少了当前统一信封的顶层 `code`，已被 D-043 替代。 |
| D-023 | 冲突/过时 | “SSE 只做弹窗、普通状态走前端 WS”与 Backend 不同，已被 D-025 替代。 |
| D-024 | 部分废弃 | 人工决定使用 REST、revision、option ID 和 `Idempotency-Key` 的规则保留；“普通状态走前端 WS”已由 D-025 废弃。 |
| D-025 | 一致 | 前端统一 `GET /api/v1/events` SSE，使用 `Last-Event-ID` 和 REST 补水；`/edge/ws` 只属于内部 Edge 控制面。 |
| D-026 | 冲突/过时 | 递归物化 JSON Schema property defaults 不是当前 Backend 行为；已由 D-045 替代。 |
| D-027 | 部分一致 | 对前端共享路径、方法、DTO 和含义切换 authority 是正确目标；其“所有功能都共享”已被 OS-only Authoring/Debugger 例外收窄。内部存储、Scheduler 和 Driver 不在 parity 范围。 |
| D-028 | 部分一致 | 禁止新增共享 capabilities 路由仍有效；“Backend 与 OS 都必须实现 authoring/debugging”已被 D-031/D-046 废弃。Edge `capability_revision` 仍只是内部注册信息。 |
| D-029 | 部分废弃／OS-only | 真实持久 Node 的 UUID 锚点保留；source-only Fork/Join 没有 Node UUID。 |
| D-030 | OS-only 扩展 | duplicate anchor fail-closed 与机器修复建议合理，Backend 无对应诊断合同。 |
| D-031 | OS-only 扩展 | compile 只由 OS 提供，边界明确；不能对 Backend base URL 暴露同名假实现。 |
| D-032 | 部分一致 | 使用 graph authority 签发的真实 template/handle UUID 正确；Backend 模板身份必须通过 ResourceTemplate 聚合同步，不能由 OS 猜测。 |
| D-033 | 部分废弃 | revision CAS、Node/Edge 写推进 revision、普通 Workflow metadata PUT 不推进 revision 均保留；D-060/D-066 规定 reserved contract/binding metadata 只能经 OS atomic Apply 修改并推进 revision。 |
| D-034 | 一致 | 当前 Backend 正是 Node PATCH、实体 CRUD、batch-delete 和 revision-guarded graph PUT；运行前先 PUT 保存、再 POST Task 的结论正确。 |
| D-035 | 部分废弃／OS-only | Backend 不保存工作流级 Python/source map 的边界保留；D-041 已废弃“OS 仅文件存储 Applied Source”，本地选定权威改为 SQLite。 |
| D-036 | 部分废弃／OS-only | 草稿写入只编译预览、不自动 Apply/Run 保留；OS-local Apply 由 D-041 改为一次 SQLite 事务，不再单独调用共享 graph PUT。 |
| D-037 | OS-only 扩展 | 语义未变时只更新本地源码映射、不调用 graph PUT，避免无意义 revision 增长，合理。 |
| D-038 | OS-only 扩展 | DAG 语义编辑重生成完整规范化 Python，并要求接受源码 diff，属于 OS 编辑器策略。 |
| D-039 | 部分废弃／OS-only | source revision stale 与 graph/code wins 调和保留；OS-local code-wins 由 D-041 改为 atomic SQLite Apply，不再单独调用共享 graph PUT。 |
| D-040 | OS-only 扩展 | compile/generate-python/validate 三个纯转换接口均不在 Backend；使用 Backend DTO 是兼容策略。 |
| D-041 | OS 私有实现 | SQLite 本地执行不镜像 Backend 的 PostgreSQL 部署；只要求前端可见 Workflow/Task/Job 数据和行为符合共享合同。 |
| D-042 | 部分一致 | authority-scoped catalog 与稳定 UUID 正确；当前 Backend 模板生命周期由 ResourceTemplate aggregate 全量同步和删除控制，不能恢复旧文档 10 的 NodeTemplate/HandleTemplate 直写。 |
| D-043 | 一致／路由例外 | 通用 `code/data/error` 信封直接镜像；按 D-058 精确保留冻结源码的两个前端可见例外：非法 SSE cursor 返回裸 `error`，ResourceTemplate DELETE 返回 200 空对象。无需再决策。 |
| D-044 | 部分废弃 | Backend 只读规则保留；原固定 `f352f54` 已废弃，D-058 改为冻结 `09609a2` 且只对齐面向前端的 Interface。 |
| D-045 | 一致 | 当前实现确实只对 Node create/graph PUT 的根 `param` 使用 `goal_default → goal → {}` 回退，不递归应用 Schema property defaults；Node PATCH 显式 null 变 `{}`。 |
| D-046 | 部分一致 | normal/step/single_node 和 step/pause/resume/cancel 与 Backend 一致；起始节点、断点、源码高亮均为 OS-only。 |
| D-047 | 部分废弃／OS-only | Task-scoped debug config 和快照隔离保留；单数 `start_node_uuid` 已由 D-052 废弃。 |
| D-048 | 部分废弃 | disabled/out-of-scope/breakpoint 的区分保留；单起点措辞由 start frontier 替代。 |
| D-049 | 部分废弃／OS-only | 定向可达子图保留；单起点已由 D-052 的 start frontier 废弃。 |
| D-050 | 部分废弃／OS-only | scope 后 required-input 验证、422 且不落 Task/Job 保留；Provider 只列 param/Edge 的旧限制已被 D-059/D-060 扩为三选一。 |
| D-051 | 部分废弃／OS-only | 静态、coding-agent-friendly Python 子集保留；source-only parallel/Fork/Join 不再是持久 Node。 |
| D-052 | 部分废弃／OS-only | 多起始前沿与 union reachability 保留；cut-input 只接受 param 的旧限制被 D-059/D-060 收窄，允许已声明 Workflow input binding。 |
| D-053 | OS-only 扩展／未完成 | Node-local Breakpoint Hold 和“只冻结支路”不在 Backend；同时该决策明确把多个 Hold 的 step/resume 语义留待后续，仍是阻塞实施的未决项。 |
| D-054 | 部分废弃 | 普通 convergence 不建 Fork/Join、使用真实下游 Node 的规则保留；“绝不允许 Join”被 D-057 收窄为“禁止隐藏 Join，允许显式已发布 Conditional Join”。 |
| D-055 | 部分废弃／OS-only | Python `if/elif/else` 的独占 first-match lowering 保留；“不创建 Join”被 D-057 收窄为可由显式 Conditional Join 终止分支。 |
| D-056 | OS-only 扩展 | 分支值不能逃逸是当前安全限制，来源是 Backend 对同一目标 Handle 只允许一条入边且没有 Phi 合并。 |
| D-057 | OS-only 扩展／有意差异 | 当前 Backend 没有 `join` NodeKind。临时 compute Join 只有在所选 graph authority 的 ResourceTemplate catalog 真正发布同一模板与 Handle UUID 时才可持久化；不能把 OS-local template UUID 发给 Backend。 |
| D-058 | 已确认边界 | Backend `09609a2` 是只读前端合同权威；Backend-to-Edge HTTP/WS、认证、Command/Inbox、重放、对账、锁和部署全部不作为 OS 前端接口或 parity gate。 |
| D-059 | 已确认 OS-only 扩展 | Workflow 定义输入合同、WorkflowTask 提供本次值、Node binding 消费值；OS 在建 Job 前严格校验并固化解析结果，Backend authority 当前不支持有效执行。 |
| D-060 | 已确认 OS-only 表示 | 输入合同存于 `Workflow.meta_data.unilab.input_contract`，Handle UUID 绑定存于 Node metadata；静态 param、入边和 Workflow input 三种 Provider 互斥，不创建伪节点或占位符。 |
| D-061 | 已确认领域类型 | 根输入、Node Handle、父工作流调用和静态插入的子工作流统一使用 `ResourceSlot`；不新增 `MaterialRef`，并与设备执行绑定 `WorkflowNode.material_uuid` 严格区分。 |
| D-062 | 已确认 OS-only 表示 | 外部 Task 只提交 Material Authority 的 `{uuid}` 引用；内部 Handle 可保留引用或单根扁平资源树，并由同一 ResourceSlot resolver 在消费边界解析。静态子工作流不转换 Binding。 |
| D-063 | 已确认 OS-only 约束／被精确化 | ResourceSlot 可复用 Backend 的 `allowed_resource_template_uuids` 精确限制模板；缺省允许任意模板，空数组非法。D-099 将原先笼统的 422 精确化为：结构或模板不匹配 400、物料缺失或已软删除 404、稳定不可运行 409；瞬时争用不作为 HTTP 失败。 |
| D-064 | 已确认 OS-only 组合规则 | 父参数与所有静态子工作流输入的模板约束求交集；有效约束在 Preview 展示并持久化到父合同，空交集使 Compile/Apply 失败，不延迟到 Task 创建。 |
| D-065 | 已确认 OS-only 快照规则 | 复用 Backend 的 `workflow_snapshot` 固化图/合同/binding，`input` 固化完整解析输入；ResourceSlot 保存 `{uuid, resource_template_uuid}`，完整可变物料树在消费 Handle 时重新解析。 |
| D-066 | 已确认 OS-only 输出合同 | `unilab.output_contract` 与 `unilab.input_contract` 同级；命名必需输出由最终 `workflow_output(...)` 绑定，子工作流静态代换，根执行写入现有 `WorkflowTask.output`。 |
| D-067 | 已确认 OS-only 输出约束 | ResourceSlot 生产者模板集合必须是消费者集合的子集；动作输出约束来自真实 Handle Template metadata，禁止从执行设备模板或名称猜测。运行期违约归属于生产者 Job 并阻断下游。 |
| D-068 | 已确认统一物料透传 | Workflow 和 action 的 ResourceSlot 输入缺少兼容同名输出时都合成 `implicit: true` 同名透传；Action 成功后由 runtime 合并输入引用，optional 缺省固定输出 `null`。 |
| D-069 | 已确认模板身份规则 | 隐式 Action 输出在本地模板目录投影/同步时成为真实 Handle，按 Backend 业务键 upsert 保持 UUID；编译器只消费已发布 UUID，禁止临时造 Handle、UUID 或暗中同步目录。 |
| D-070 | 已确认物料集合表示 | `List[ResourceSlot]` 始终是有序 `list[dict]`，每个 dict 是独立根且可递归含 `children`；禁止外层嵌套 list 或把外层兄弟误作一棵 flatten 树。 |
| D-071 | 已确认 OS-only 输出绑定位置 | 根 Output Bindings 只存于 `Workflow.meta_data.unilab.output_bindings`，进入现有 Task snapshot 并与 graph/source 原子 Apply；具体 binding 变体由 D-072 关闭。 |
| D-072 | 已确认 OS-only 输出绑定变体 | v1 只有 `workflow_input(parameter)` 与 `node_output(workflow_node_uuid, source_handle_uuid)`；子工作流和隐式透传归一化，不支持 literal/expression/result-path 变体。 |
| D-073 | 已确认 OS-only 持久 Authoring 路由 | 纯转换保留顶层 `/authoring/*`；持久状态固定为 Workflow-scoped GET、draft PUT 和 apply POST，路径是唯一 Workflow 身份。 |
| D-074 | 已确认 OS-only Draft 同步策略 | 浏览器不直接访问本地文件；clean 时由 OS 的 SSE invalidation 触发 Authoring GET 并同步代码/DAG，dirty 时保留本地缓冲并标记外部变更，保存以起始 hash 冲突保护且只能经显式确认覆盖。 |
| D-075 | 已确认 OS-only Draft 双 CAS | Draft PUT 携带完整 Python、起始 Draft 字节 hash 和 `Workflow.revision`；OS 在同一工作流锁内核验后原子替换并编译，任一冲突为 409，且永不 Apply/执行。 |
| D-076 | 已确认 OS-only Apply 三 token | Apply 只回传 Draft hash、Workflow revision 和不透明 Candidate hash，不接收客户端 graph/apply bundle；OS 对服务器 Candidate 复核后事务提交，执行仍由后续 Task POST 创建。 |
| D-077 | 已确认 OS-only Authoring aggregate | GET 一次返回同版本的 Applied Graph、Draft、Candidate、Applied Source、状态、hash、诊断和 provenance；缺失单体为 null、集合为 `[]`，Graph 精确复用 Backend 投影。 |
| D-078 | 已确认 OS-only 成功响应 | Draft PUT 返回完整 aggregate，保存无效源码仍为 200；Apply 返回 graph/source-only 结果和新 aggregate，提交后源码回写失败只作为可恢复 warning。 |
| D-079 | 已确认 OS-only Authoring 错误 | 保持 Backend 信封，细分 Draft/revision/Candidate/catalog 冲突与 422/503，并规定中文前端文案；错误不夹带替换源码或 Candidate。 |
| D-080 | 已确认 OS-only Authoring SSE | 只用 `workflow.authoring.changed` 作为持久失效通知；状态与 durable event 同事务、提交后发送，复用 Backend 全局 cursor/replay，前端始终经 Authoring GET 补水。 |
| D-081 | 已确认 OS-only Draft 生命周期 | 当前允许实验室工作区等同领域设备包仓库；包内注册的 `workflows/*.py` 是唯一 Draft，`unilabos_data/workflow.db` 是 Applied/Runtime 权威，不复制源码并按 package URI、CAS、watch/recovery 管理。 |
| D-082 | 已确认 OS-only Workflow 类型集合 | Input/Output 共用四种标量、opaque JSON object、ResourceSlot 及其一维同构 list；不开放 Any、任意模型、混合/嵌套声明 list 或非 JSON Python 类型。 |
| D-083 | 已确认 OS-only 严格类型 | 不做字符串、布尔、UUID 或 list element convenience coercion；仅允许 integer 满足 number 及数学整数规范化，所有入口同一验证且失败时不创建 Task/Job。 |
| D-084 | 已确认 OS-only Task 输入缺省语义 | 仅在已声明的顶层 Task input 归一化中令 `null` 等同未提交，再统一补 default/判 required；未知键、opaque object 内部、list element、PATCH 和 Workflow output 不采用该等价。 |
| D-085 | 已确认 OS-only Schema 约束集合 | Input/Output 只支持标量 enum、数值闭区间、字符串/列表长度和 ResourceSlot 模板 allowlist；`title/description` 仅展示，其余 JSON Schema 关键字不开放。 |
| D-086 | 已确认 OS-only 封闭合同 | Contract、parameter、schema、Task input 与最终 output 均拒绝未知顶层字段；外部 ResourceSlot 只允许 `{uuid}`，仅 opaque JSON object 内部保持开放。 |
| D-087 | 已确认 OS-only Output 完成语义 | Output descriptor 没有 `required/default`；每个声明键在成功前都须产生，nullable 也须显式写键；合同违约不得进入 succeeded，失败任务的部分结果另议。 |
| D-088 | 已确认统一参数声明来源 | Workflow 与 Action 共用注解转 Schema 解析器；类型来自 annotation、默认值来自 `=`，`Field` 与 docstring 均可供参数 title/description，非空冲突时结构化 `Field` 优先。 |
| D-089 | 已确认 nullable Python 语法 | 解析同时接受 `Optional[T]` 与 `T | None`，规范化只生成 `T | None`；nullable input 必须是 `T | None = None`，并严格区分 nullable collection 与空 collection。 |
| D-090 | 已确认边界与 ResourceTemplate 语法 | `Field` 可省略；存在时仅接受闭区间/长度和展示元数据。ResourceSlot 用 `AllowedResourceTemplates(@resource符号...)`，由 OS 静态解析 catalog UUID，源码不硬编码 UUID。 |
| D-091 | 已确认 enum Python 语法 | 标量 enum 只用 `Literal[...]`；字符串/布尔/整数/数值分别推导且禁止非法混合，nullable 在外层，列表可约束 item；先做 D-083 严格类型再判断成员。 |
| D-092 | 已确认 OS-only 规范化 Python 与语义补全 | 根参数、输出、UUID anchor 和确定性生成形态已闭合；设备用 `selector: Template = device()` 或 `device("id")` 表达逐 Job 调度或固定实例，AST/Catalog 负责验证。OS 从同一 Catalog 生成 Action-only 类型投影供 Monaco、IDE、CLI/MCP 和 coding agent 补全；实际分配与普通运行态按 D-025 走前端 SSE。 |
| D-093 | 已确认统一 Authority 边界 | 接收 `POST /workflow-tasks` 的 Task Authority 同时是该 Task 唯一的 Material Authority；请求不能另选、代理或跨 authority 回退，跨 authority 必须先显式同步/导入。 |
| D-094 | 已确认 OS Material 真值与运行时投影 | OS 以 Inventory 事务引擎深化出的持久 Material module 为唯一真值；`ResourceTreeSet` 仅是受控执行投影。启动/执行/API 写在固定同步点转换，动作后先幂等持久化物料再完成 Job；未知物理结果必须 fence/quarantine 并对账。 |
| D-095 | 已确认 Material 身份与 barcode 命名 | `Material.uuid`/引用处 `material_uuid` 是唯一身份，迁移路径删除 edge/cloud/instance 别名。OS 内部保留单一 `barcode` 值并对外投影 Backend `Material.code`；设备与业务物料共享 Material 身份表，但 DeviceState 与 Inventory 生命周期分离。 |
| D-096 | 已确认迁移轮次门禁 | 每个可独立合并的切片使用独立分支；至少两名独立测试作者先写红测并保留提交来源；完整测试门通过后，至少三名未参与实现的独立评审者按合同、代码设计和回归风险分别评审；所有阻塞意见关闭后才能合并。 |
| D-097 | 已确认 Site 与组成关系 | OS 持久 Site 精确采用 Backend 身份/字段；`Material.parent_uuid` 只表达组成，`Site.occupied_material_uuid` 只表达位置占用。旧 `resource_relation.slot_id` 一次迁移后退出真值，PLR Site 名仅作运行投影，2D lab layout 保持独立。 |
| D-098 | 已确认双层调度占用／被精确化 | Task 以 `workflow_task_uuid` 持久预留业务物料/数量；Job 选定设备后以 `job_uuid+attempt` 原子 claim 设备、可变物料及占用变化 Site。D-099 明确瞬时获取冲突可发生在 Task 已创建后：Task 保持 pending、Reservation 全有或全无并由协调器重试；未知物理结果跨重启 fence。 |
| D-099 | 已确认 Material disposition、错误与等待边界 | 业务 Material disposition 为 `active/consumed/discarded/quarantined/reconciling`，`reserved/in_use` 从 Reservation/Claim 派生。Backend-shaped Task/Material 请求采用 400/404/409；暂时性 reservation/site/executor/claim 冲突保持 Task/Job pending，由协调器重试。软删除不得静默解绑活跃运行关系。 |
| D-100 | 已确认 P0-4 Action 类型来源 | Action 输入来自函数参数注解；显式命名输出正式支持 `TypedDict` 和 frozen dataclass，并兼容非推荐的内联字典返回注解。三者归一为同一 ordered contract 和 Handle 业务身份；裸 `dict` 不猜测字段，隐式 ResourceSlot 透传仍由 Registry 合成。 |

## 不再作为决策：直接镜像 Backend 的前端合同

D-058 已经给出“前端精确兼容”的总决策。下列内容在
`feat/workflow@09609a2` 已有确定的公开路由、DTO、错误和 Service 行为；
它们是实现清单和合同测试来源，不再逐项 grill：

| 直接镜像项 | 冻结 Backend 行为 | OS 结论 |
|---|---|---|
| Job 初始投影 | Task 创建事务中预建计划内全部 pending Job | 原子预建；Task 创建成功后 Job 查询立即可见 |
| Task/Job 状态机 | Task、Job、control、cleanup 字段、枚举、终态及迁移已固定 | 精确复制前端 DTO 和可观察迁移，不另造状态 |
| 写入所有权 | 前端只创建 Task/Command，不直接写 Task/Job 状态 | 只开放 Backend 既有写入口；OS Scheduler/Executor 是内部写入者 |
| Task Command | `step/pause/resume/cancel` 的 DTO、错误和投影已固定 | 精确复制；OS-only breakpoint 命令另设路由 |
| 派生 WorkflowEdge 状态 | 不存在 `workflow_task_edge_state` 持久模型 | Node-only 持久事实；Edge active/inactive 在 reconcile 时派生 |
| Job Feedback | REST 历史查询加 `job.feedback` SSE | 精确复制查询、排序、响应和事件 |
| Intervention/重试 | open intervention、revision、option、幂等 decision 和 SSE 已固定 | 精确复制前端闭环；不复制 Backend-to-Edge 上下行 |
| Manual Confirmation | 真实 `manual_confirm` Node/Job 及详情、approve/reject Interface | 作为共享能力实现，不降级为只读或 capability flag |
| 前端查询 | Job detail、feedback、open intervention 等均有正式 REST | 路由清单与合同测试直接来自冻结源码 |
| SSE | 事件名、payload、全局 cursor 和 `Last-Event-ID` 已固定 | 精确复制，包括 D-043 点名的 cursor 错误例外 |
| Graph read/write | read 返回图和模板集合；write 只接收 `revision/nodes/edges` | 保持读写 DTO 非对称，不再重新设计 |
| ResourceTemplate 删除 | 级联业务效果和 HTTP 200 空对象均为前端可见行为 | 按冻结源码镜像；不得改成“被引用则拒绝”或 204 |
| Template catalog 同步 | D-042/D-069 已确定 authority 分区、显式同步、fingerprint、稳定业务键和 stale/missing 失败 | 按既有决策实现；不得由编译器暗中同步或造 UUID |
| `tool_call` 节点 | 冻结 Planner 已接受该 NodeKind | 前端合同和 OS 执行语义都应实现；内部 executor 可按 OS 设计 |

上述项目如果 OS 当前缺失，应登记为实现差距，而不是重新开启合同决策。

## 仍然存在的设计缺口

这些缺口不是 Backend 已经回答的问题，必须在实施对应功能前继续 grill：

| 缓急 | 真正待决项 | 已确认边界 |
|---|---|---|
| P0 | 外部 `WorkflowTask.output` 的 ResourceSlot/`List[ResourceSlot]` 表示 | D-065/D-070 只冻结了 input；output 的引用字段、顺序、null 和失败任务是否暴露部分结果尚未确认 |
| P0 | Action contract 的 ResourceSlot 声明入口 | D-067～D-069 冻结了投影后的 Handle metadata 和隐式 output，但注册 action 时如何声明 input/output schema、allowlist 和 List 尚未冻结 |
| P1 | OS debug launch 和 Breakpoint Hold Interface | `start_node_uuids`/`breakpoint_node_uuids` 已确认；仍缺独立 launch 路由、Hold 持久模型、事件/查询/命令、多个 Hold 的 step/resume 目标语义，以及 out-of-scope 前端投影 |
| P1 | 条件表达式与 Conditional Join catalog 合同 | first-match 和显式 Join 已确认；仍缺精确 AST/Python 子集、Handle 生成，以及临时 compute Join 的正式模板/稳定 UUID/目录发布定义 |
| P2 | OS `tool_call` executor 的内部实现边界 | 前端语义必须镜像；仍需选择 OS 内部如何执行、隔离和恢复 tool call，这不是新增前端协议 |

## 飞书设计与冻结 Backend 代码的偏差

这部分必须区分“OS 决策不同”和“Backend 文档自身已落后”。后者不应被 OS
盲目复制。

| 偏差 | 飞书 revision 12（正文描述 `5c05941`） | 冻结 `09609a2` 实现 | 影响 |
|---|---|---|---|
| authority commit | 声明实现提交 `5c05941` | 本轮冻结提交 `09609a2` | D-058 已冻结该提交；后来工作区不自动升级合同 |
| WorkflowEdge 激活状态持久化 | 核心新增表列出 `workflow_task_edge_state` | 无对应 model、migration 或 repository；`ExecutionState.EdgeStates` 在 reconcile 内派生 | 这里指 DAG 的 WorkflowEdge，不是 Edge Agent 通信；与已确认的 Node-only runtime 方向一致 |
| NodeKind | 仅列 device_action、compute、condition、script、group | Planner 另有 `tool_call`、`manual_confirm` | 共享前端语义直接镜像；OS executor 是实现缺口，不再选择“只读/拒绝” |
| Job 创建所有权 | 表称 Node Job 由 Scheduler 写，HTTP 不能直接创建 | `CreateWorkflowTask` 在创建 Task 的事务中预建每个计划节点的 pending Job | 直接镜像冻结行为 |
| Task input | 文档明确 Task 无运行时输入 | Handler DTO 接受 `input`；Service 创建时写入 `{}`，没有使用请求值 | D-059 已明确 OS 有效实现、Backend authority 显式不支持，禁止静默忽略 |
| 响应信封 | 统一合同为 `code/data/error` | 通用 Handler 符合；SSE 非法 cursor、ResourceTemplate DELETE 有前端可见例外 | D-043/D-058 已确定精确镜像两个例外 |
| ResourceTemplate 删除 | 历史文档/Handler 注释称只删除未被物料引用的模板 | Repository 会软删除关联 Material、Workflow Node/Edge 和模板定义；Handler 返回 200 | D-058 的精确前端业务语义要求 OS 镜像该级联；属于高风险实现测试项而非待决合同 |
| 参数默认值文档 | 飞书只说参数绑定 Node | 架构文档前文称 Schema default 不物化，后文又记载 `goal_default/goal/{}` 根回退 | 冻结源码与 D-045 一致；应修正文档内部矛盾 |

关键实现定位：

- 冻结公开工作流路由与 DTO：
  `/home/xiongyanfei/uni-lab-backend-github/internal/http/handler/workflow.go:22`
  及 `:83`、`:189`、`:350`。
- graph revision 与 UUID reconciliation：
  `/home/xiongyanfei/uni-lab-backend-github/internal/service/workflow/graph_edit.go:11`，
  `/home/xiongyanfei/uni-lab-backend-github/internal/repository/workflow_graph_edit.go:15`。
- Task 创建、快照和预建 Job：
  `/home/xiongyanfei/uni-lab-backend-github/internal/service/workflow/execution.go:15`。
- Planner 的 NodeKind、disabled、Handle 唯一性和必填输入：
  `/home/xiongyanfei/uni-lab-backend-github/internal/service/workflow/planner.go:13`。
- 派生 Edge activation 和 AND admission：
  `/home/xiongyanfei/uni-lab-backend-github/internal/service/workflow/decision.go:36`
  及 `:294`。
- Task/Job/lock 模型与真实 JSON 字段名：
  `/home/xiongyanfei/uni-lab-backend-github/internal/model/workflow.go:95`。
- ResourceTemplate 聚合同步和级联删除：
  `/home/xiongyanfei/uni-lab-backend-github/internal/service/template/registry.go:14`，
  `/home/xiongyanfei/uni-lab-backend-github/internal/repository/template_graph.go:181`。
- Backend 架构文档的数据表清单仍列
  `workflow_task_edge_state`：
  `/home/xiongyanfei/uni-lab-backend-github/docs/architecture/workflow-scheduler.md:919`；
  冻结模型与 migration 中不存在该表。

## 下一轮 grill 顺序

不要再从“执行状态、Job 预建、Task Command、feedback/intervention/manual
confirmation、SSE、ResourceTemplate DELETE、`tool_call` 是否对前端可见”
开始讨论；这些都已由 D-058 和冻结 Backend 回答。

建议严格按以下依赖顺序，一次只完成一个决策：

1. **Action ResourceSlot 注册语法**：把同一 schema/allowlist/List 规则落到
   registry → NodeTemplate/HandleTemplate projection。
2. **外部 Task output 表示与失败边界**：冻结单个/集合 ResourceSlot、
   scalar、nullable、顺序，以及失败 Task 是否暴露部分 output。
3. **Debug launch 与 Hold**：依次决定独立启动路由、持久模型、多 Hold
   step/resume、事件/查询和前端 out-of-scope 展示。
4. **条件 AST 与 Conditional Join template**：冻结表达式子集、显式源码语法、
   有限 Handle arity 和目录发布。
5. **`tool_call` executor 安全边界**：冻结本地工具 allowlist、credentials、
   副作用边界、进程隔离、取消和重启恢复；不再改变共享前端合同。
   `manual_confirm` 的前端闭环直接镜像。

第 1～2 项完成即可闭合工作流定义、Python/JSON 双向编写、Input/Output 合同和
运行前 Apply；第 3 项完成后再交付完整调试器；第 4～5 项可在对应模板和执行
能力阶段补齐。OS-local Scheduler/Driver 的可靠执行继续按 OS 自身实现和测试
维护，不从 Backend-to-Edge 文档复制接口。
