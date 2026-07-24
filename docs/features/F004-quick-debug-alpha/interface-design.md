# 接口设计: Quick Debug Alpha（W1–W2）

## 不可变边界

- OS 内通用 `RuntimeService` 是 source 解析、Canonical 编译、TaskDag 下发和持久投影的唯一入口；OS executor 是 ready、运行 lease、物理终态和恢复的唯一权威。
- 复用 `TaskDag`、`DagWalk`、`TaskDagRunner` 与 local bridge；v1 payload 通过 compatibility adapter 进入 Canonical。
- W1–W2 不出现 `PlannedOccupancy`、`PlanRevision` 或 scheduler client。
- Runtime API 设备无关：所有设备/工站均提交同一种 Workflow/Run 合同；`local_api.py` 不 import 任何具体设备包，禁止 `/api/runtime/<device>/...` 路由。
- 每个实际执行 OS 进程只有一个 `ResourceLockManager`；所有 `TaskDagRunner` 共用，保证跨 run 互斥。Quick Debug 只使用一份 SQLite journal 文件；bridge 可写入 submission/transport projection，但 run terminal 只能由 executor 追加。分进程部署的 reconcile 必须经通用 ScheduleSession 命令交给执行 OS 的 lock manager，bridge 不得直接释放自己的影子 lease。
- 设备产品 Profile 只声明 template、资源拓扑、默认 binding、driver key 与 legacy-to-Workflow 映射。删除某个 Profile 不影响通用 Runtime API 和执行器。
- Workflow authoring store 以本地文件为权威，SQLite `workflow_index/draft` 仅作二级索引和崩溃恢复；执行只接受已编译且 source hash 匹配的不可变 Revision。

## 核心接口

```python
class ActionContract(BaseModel):
    version: Literal["2"]
    kind: Literal["atomic", "device_macro"]
    inputs: tuple[ActionPort, ...]
    outputs: tuple[ActionPort, ...]
    resource_claims: tuple[ResourceClaimTemplate, ...]
    estimated_duration_s: float | None
    recovery: RecoveryPolicy | None

class ResourceLockManager:
    async def acquire_all(self, request: LeaseRequest) -> Lease | None: ...
    async def release(self, lease_id: str) -> None: ...
    async def mark_unknown(self, lease_id: str, reason: str) -> None: ...

class DeterministicReadyPolicy:
    async def admit(self, ready_nodes: Sequence[DagNode]) -> list[AdmittedNode]: ...

class EstimatedTimelineBuilder:
    def build(self, dag: TaskDag) -> EstimatedTimeline: ...

class RuntimeService:
    async def start_run(self, source: WorkflowSource) -> RunRef: ...
    def get_run(self, run_id: str) -> RunProjection | None: ...
    async def cancel_run(self, run_id: str) -> RunProjection | None: ...
    async def reconcile_run(self, run_id: str, decision: ReconcileDecision) -> RunProjection: ...
```

Canonical binding 使用 tagged value：`LiteralValue`、`RuntimeParameterRef`、`NodeOutputRef`。结果先经 schema 校验并持久化，随后才能释放 data successor。

生产回调链固定为 `publish_job_status(return_info) -> MessageProcessor -> TaskDagRunner -> NodeExecutionResult(ResultEnvelope)`；不得在回调边界丢失 named outputs。

## SQLite journal

必须在同一事务写入 node terminal、result、material effect、cursor 和 outbox。RUNNING 重启为 RECONCILING；只有 contract 明确 safe retry 且设备确认未启动时才能重试。

SQLite journal 与工作流文件存储严格分责：

- 文件：规范化 Python/Canonical artifact/source map，Git 可 diff，原子写入；
- SQLite：文件 URI/hash/index、显式 draft、Run submission/event/result/cursor/lease/outbox；
- 冲突：文件是作者真值，journal 是运行真值；两者都不能由 UI 视觉投影反向猜测或覆盖。

## pTLC 能力口径

- Workflow 参数：Workflow 级有序声明 schema、节点级 `RuntimeParameterRef`、运行前类型/default/required 校验、Run submission parameters 与共享 Workbench 表单已闭环；未声明参数 fail closed。
- 分支：真实 Operation 的条件在 OS 内求值并只激活 taken branch；`ConditionalBinding/Phi` 跨 join 保留结果。通用 Pythonic 任意语句作者态仍按白名单逐步扩展。
- 循环：仅静态 finite `for`；没有运行时 `while/repeat`。
- Debugger：当前没有 step/step-over/breakpoint/run-to/from-node 公共状态机与 API；journal reconcile/resume 只用于安全恢复。

## pTLC 包边界

- 物理 action/device macro 由 `Uni-Lab-Templates/packages/ptlc_station` 的 `@action` 装饰器声明并可独立构建安装；Profile 只引用这些注册能力和通用 driver key。
- `run_script`、`if`、结果变量、join 和 `finally` 属于通用 Operation/Workflow codec 与 OS DAG，不属于 pTLC Runtime。
- Human 节点使用 OS 既有 `host_node.manual_confirm`，设备包不复制 human action。
- PLC/ST 内部 FSM 仍是下位机自治实现，Uni-Lab 不管理 ST 源码。

## 测试策略

- 单元测试均使用 fake action dispatcher/fake PLC/fake clock。
- Hypothesis 覆盖锁 acquire-all、调度无双持有、编译 hash 确定性和 DAG earliest-start 不变量。
- pTLC 先跑 golden mock，不连接真实 PLC。
