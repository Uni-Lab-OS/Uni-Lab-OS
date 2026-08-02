# D1A-S1 设备页单 Action Task：OS 实施规格

状态：Implementation baseline  
上游协议：`Uni-Lab-OS/Uni-Lab-Core#162`  
OS delivery：`Uni-Lab-OS/Uni-Lab-OS#16`  
Integration gate：`Uni-Lab-OS/Uni-Lab-Core#163`  
实施基线：`08b86898c7b56cce4e5851abb56ecc05b03e3bb0`

## 1. 本轮 Outcome 与停止线

D1A-S1 恢复设备页运行一个无物料 Action 的能力。一次接受必须形成正式、持久的
`WorkflowTask` 和唯一 `WorkflowNodeJob`，并沿现有
`EdgeScheduler -> JobExecutionBackend -> HostNode` 执行。HTTP、ROS goal accepted
和终态分别记账，任何 2xx 都不能直接写成功。

本轮不实现自动设备候选、公平性、换设备 retry、ResourceSlot、Material、Site、
ChangeSet 或隐式 material pass-through。A1 合同含这些语义时返回
`422 unsupported_contract`，不得删字段或把它们降级成普通 JSON。

旧 `/api/v1/runtime/runs*`、旧 runtime WebSocket、前端直连 Edge、临时
Workflow CRUD 组合和第二套 Task/Job 状态机均不在本轮范围内。

## 2. 公开 wire contract

新增两个 Backend-envelope 路由：

```text
POST /api/v1/device-action-tasks
GET  /api/v1/device-action-tasks/{task_uuid}
```

POST 请求严格且拒绝多余字段：

```json
{
  "authority_id": "os-local",
  "template_catalog_fingerprint": "sha256:<64-hex>",
  "workflow_node_template_uuid": "<uuid>",
  "device_id": "robot",
  "input": {"duration_seconds": 5},
  "idempotency_key": "<caller-generated uuid>",
  "description": "设备页单动作运行"
}
```

请求没有 `action_name`。公开 `DeviceActionTaskView` 仅投影：

- `task_uuid`、`job_uuid`；
- `authority_id`、`template_catalog_fingerprint`、
  `workflow_node_template_uuid`、公开 Action `name/display_name`；
- `device_id`、`status/control_status/cleanup_status`；
- closed `input/output/error_info`、Job status、feedback cursor 和时间戳。

禁止出现 system `workflow_uuid`、system Node UUID、source revision/content/path、
内部 assignment/lease token。公开 error code 固定为 `invalid_input`、`not_found`、
`template_catalog_conflict`、`idempotency_conflict`、`device_action_mismatch`、
`unsupported_contract` 和 `admission_unavailable`；不得返回 FastAPI `detail`。

普通 `POST /workflow-tasks/{task_uuid}/commands` 的 `cancel` 与
`GET /workflow-node-jobs/{job_uuid}/feedback` 可以消费公开 Task/Job UUID；system
Task/Job 必须从普通 Workflow、Authoring、Task list/detail 和 Job detail 隔离。
全局 SSE 只新增 `device_action_task.changed`，data 只有 `task_uuid`。

## 3. 模块 seam 与持久事实

新增 `unilabos/workflow/device_action_task.py` 作为 deep module，API 只依赖它的
request/view service，不自行拼事务或执行 payload。它复用同一个 `WorkflowStore`、
`TemplateCatalog`、`WorkflowRuntimeCoordinator` 和 production Edge stack。

SQLite 增加一等表，而不是在任意 `meta_data` 中编码 origin：

1. `device_action_system_source`
   - 业务键 `(authority_id, workflow_node_template_uuid)`；
   - 稳定 `workflow_uuid`、稳定 `workflow_node_uuid`；
   - `origin_kind` 固定 `system/device-console`；
   - 当前 Catalog fingerprint、source revision 和 closed contract snapshot。
2. `device_action_task`
   - `workflow_task_uuid`、唯一 `workflow_node_job_uuid`；
   - 公开 template identity、请求的 concrete device、canonical input；
   - nullable admitted device/claim 状态；
   - canonical payload hash、调用者 UUID `idempotency_key`；
   - authority/device 范围唯一幂等键。

system Workflow/Node 仍写入正式 `workflow`/`workflow_node`，以满足 Task 外键、
snapshot 和审计；一等 source 表负责标记其系统身份。普通 Workflow/Graph/Authoring
读写和普通 Task/Job projection 在 store/service 查询边界 fail closed：列表反连接
system source，按 UUID detail/Authoring/写路由返回 `not_found`。内部审计不加入前端
router。

## 4. 原子 materialization

锁顺序固定为 `TemplateCatalog guard -> WorkflowStore transaction`。同一事务完成：

1. 读取 authority snapshot 并 CAS `template_catalog_fingerprint`；
2. 按 UUID 解析 A1 typed Action，拒绝非 Action 或含物料语义的合同；
3. 从 live HostNode Action registry 校验具体 device、Action 名、type 和合同相符；
4. 用 A1 closed schema 严格归一化 input：required/default/null/type 均复用现有
   Workflow schema vocabulary，不做字符串转数字等便利 coercion；
5. 创建或推进稳定 system source revision。具体 device 不写 editable/system graph，
   Action 参数只通过 Workflow input contract/binding 进入 Task snapshot；
6. 创建 `run_mode=single_node` 的 Task、唯一 Job、D1A row 和幂等记录。

相同 scope/key 且 canonical payload 相同返回原 view；不同 payload 返回
`409 idempotency_conflict`。任何失败回滚整个事务，提交前不得 Admission/dispatch。

Catalog 合同变化推进同一 system Workflow revision；旧 Task 保留旧 snapshot。source
UUID 和 Node UUID不因点击或合同 revision 改变。

## 5. Admission、dispatch、feedback 与终态

`DeviceActionTaskRuntimeBridge` 是正式 Workflow runtime 到现有 Edge scheduler 的
适配器，不拥有第二套领域状态：

- 创建后 Task/Job 保持 durable `pending`；请求的 device 已冻结，但 admitted
  assignment/claim 仍为空；
- Edge scheduler 按现有 busy/ordering 门决定何时 admission。一个 scheduler 临界区
  中的 pre-dispatch hook 先持久写固定 assignment/claim、Task `running`、Job
  `dispatched`，再以数据库中的同一个 Job UUID 调用 `JobExecutionBackend`；
- 持久 dispatched 后而 transport 未确认的崩溃按现有 startup recovery 进入
  `execution_unknown`，不得重新盲发；持久写入前崩溃仍可安全重试 admission；
- backend 的 running/feedback 回报经 coordinator 写 Job running 与单调 feedback
  history；SSE 只做 invalidation；
- terminal result 先用 A1 output contract 严格归一化，再在一个 runtime 事务写
  Job terminal 和单节点 Task terminal；Task `output` 与 Job closed result 相同；
- driver/HostNode 不可用、dispatch/cancel 结果不确定进入
  `execution_unknown`/reconciliation，不写伪 failed/succeeded；
- durable cancel command 由 runtime worker 消费，再请求 scheduler/backend/HostNode
  取消。人工 `force_unlock` 仍是独立安全处置，不能当作 Task cancel。

busy holder 存在时不把 Job 送进 `DeviceActionManager` 的私有 queue；Task/Job 保持
durable pending/admission-blocked，由 scheduler 的释放触发和启动恢复重新参与 admission，
FE 不轮询推进。

## 6. 组合根与生命周期

production `compose_workflow_runtime` 持有 D1A service/runtime bridge；FastAPI
composition 注入 live catalog 和 `get_edge_scheduler/get_edge_backend` capability。
任一 capability 未就绪时创建返回 `503 admission_unavailable`，不得退回旧运行接口。

启动顺序：Workflow startup recovery -> D1A pending replay/reconciliation -> 接受新
请求。停机必须停止 D1A worker/listener 后再关闭 WorkflowStore，且不重复注册 backend
listener/router。

## 7. 本轮预先冻结的公开测试缝

独立 RED 只从公开 seam 观察，不绑定私有函数名：

1. HTTP POST/GET、Backend envelope、幂等 replay/conflict、Catalog stale、严格输入、
   Action mismatch、unsupported contract；
2. 普通 Workflow/list/detail/Authoring/Task/Job 与 SSE 响应中 system source 字段为 0；
3. 正式 Task/Job 状态、固定 Claim/busy holder、feedback、typed result、cancel 和 restart；
4. 同一 busy Action 的第二个 Task durable 等待，释放后由 scheduler 推进且不产生第二
   holder。

测试可以注入 live catalog、scheduler/backend/HostNode ports，但不得 mock 掉
`WorkflowStore`、Task/Job transaction 或 HTTP projection。

## 8. Gate 与证据

本轮遵循 `AGENTS.md` round gate：恰好一个独立 test-author 先提交失败 tracer；测试
commit 保留 provenance；实现后运行目标测试、全仓 pytest、静态检查和
`git diff --check`；再由恰好一个未参与实现/测试的 reviewer 审查 exact tested SHA。

OS ticket 记录 base/test/implementation/review full SHA、命令与 finding disposition。
跨仓真实 Edge E2E、至少五张截图、network ledger 和 system source 负向证据归 Core
#163。未完成 FE gate、Core pin 与 Feishu Accepted 前，本轮不宣称 `stage:accepted`。
