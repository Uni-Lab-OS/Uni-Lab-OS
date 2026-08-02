# I1：Workflow I/O contract implementation 设计基线

## 1. Outcome、基线与控制面

本轮在已经 Accepted 的 WorkflowTask Runtime 与已经完成的 Phase 02H 之上，交付
Workflow Input/Output Contract 的统一验证、持久化、Python/JSON 无损往返以及 Task
创建时的合同复用。跨仓协议由
[Core #154](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/154) 冻结；本文只描述 OS
owning repository 的 implementation boundary，不另造协议。

跨仓验收门为
[Core #157](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/157)。

实现基线固定为：

- OS `integration/workflow-task-runtime@91b00dd030483058a6d0aafc42f143de829cc1bc`；
- FE integration dependency
  `integration/fe-os-migration@a641fa6fa38b223ec90648a2c308c67d4a57b6fd`；
- implementation branch `migration/i1-workflow-io-contract`。

### 2026-08-02 continuation 基线

旧 implementation branch 保留上述冻结 spec 与 RED provenance，不直接承载后续
A1/M2A 之后的 production。当前续作从最新远端
`integration/workflow-task-runtime@8fad069c16faeb991fade5232eaf84ef32b17146`
建立 `migration/i1-workflow-io-contract-8fad`；独立 RED `ec914c615a7fbcac0df15e88d0029083268954b4`
以提交 `265a013` 原样移植，首个 GREEN 为 `0c21f9f`。后续 integration/review 证据必须固定
续作分支的 exact SHA，不得再把 `91b00dd...` 表述为当前 production 起点。

I1 的 non-Catalog、non-Material slice 可以与 A1、M1 并行。result-record 的 Catalog
projection 与 `AllowedResourceTemplates` symbol identity 必须等待 A1 gate；真实
ResourceSlot UUID 的成功解析必须等待 M1 durable Material adapter。C1 和 R2 只能消费
Accepted 的 I1 合同。

## 2. Phase 02H 已完成，不得重复

以下能力已经由 Phase 02H Task input preflight 交付，I1 只复用并回归，不新建平行
Module、DTO、resolver 或 ticket：

1. Task INSERT 前的 v1 input contract parse、unknown/missing/default、顶层
   null-as-omission、strict normalization 与 finite constraint 校验；
2. 外部 ResourceSlot closed `{"uuid": "<material_uuid>"}` codec、可注入 resolver、
   canonical `{"uuid", "resource_template_uuid"}` 以及 `400/404/409` 分类；
3. 完整 resolved input、exact Workflow graph snapshot、execution plan 与 Jobs 的同事务写入；
4. 使用真实 target `WorkflowHandleTemplate.uuid` 的 Node input binding、provider 互斥、
   required provider 和 Job/plan param projection；
5. 任一 preflight 失败时 Task/Job 零 partial write，后续 Workflow 修改不改变既有 snapshot。

Phase 02H 的权威实现仍位于 `unilabos/workflow/task_input.py`、`service.py`、`store.py`
和 `schema.py`。历史 delivery `deepmodeling/Uni-Lab-OS#301` 应按已发布 commit 证据收口，
不得改名后重复承载 I1。

I1 不改变 `POST /api/v1/workflow-tasks` 的 Backend-shaped request：请求仍不携带 expected
revision。Store transaction 观察到的 persisted Graph 是本 Task 的唯一 snapshot。

## 3. Persisted authority 与 closed wire shape

Authoring Apply 原子维护以下 graph-semantic state：

```text
Workflow.meta_data.unilab.input_contract
Workflow.meta_data.unilab.output_contract
Workflow.meta_data.unilab.output_bindings
WorkflowNode.meta_data.unilab.input_bindings[target_handle_uuid]
```

这些 reserved 字段只能随 graph、Applied Authoring Source、source map 和 Catalog fingerprint
一起 Apply，并在同一 transaction 推进 `Workflow.revision`。普通 Workflow metadata update、
Node CRUD 或 full-graph PUT 不得修改这些字段。`WorkflowTask.workflow_snapshot` 继续冻结完整
Graph；不得增加 `io_snapshot`、`contract_snapshot` 等第二份快照。

Input Contract 保持 ordered closed v1 envelope：

```json
{
  "version": 1,
  "parameters": [
    {
      "name": "temperature",
      "schema": {"type": "number"},
      "required": false,
      "default": 80.0,
      "title": "Target temperature",
      "description": ""
    }
  ]
}
```

Output Contract 同样 ordered、closed，但 output descriptor 不携带 `required` 或
`default`：

```json
{
  "version": 1,
  "outputs": [
    {
      "name": "final_sample",
      "schema": {"$slot": "ResourceSlot"},
      "title": "Final sample",
      "description": "",
      "implicit": false
    }
  ]
}
```

`implicit` 是 server-managed、公开只读标记。它只由 Workflow/Action 的同名
ResourceSlot pass-through 合成；caller、FE 与普通 metadata write 均不得创建、切换或删除
该标记。显式 output 与隐式 output 重名、类型不兼容或产生两个 producer 时 Apply 失败。

Output Binding 只有两个 closed variant：

```json
{"kind": "workflow_input", "parameter": "sample"}
```

```json
{
  "kind": "node_output",
  "workflow_node_uuid": "<node_uuid>",
  "source_handle_uuid": "<source_handle_template_uuid>"
}
```

`output_bindings` 的 key 必须与 Output Contract name 一一对应。每个显式 output 必须恰有
一个 binding；不存在、unknown、重复或动态 binding 均为 Apply error。Node Input Binding
继续严格使用本 Node 的真实 target Handle UUID，不接受 display name、`data_key`、
`handle_key`、Action 名、字段名或 ordinal。

## 4. Common Workflow I/O validator

新增或深化一个 transport-independent Workflow I/O deep Module，使 Compile、Generate、
Validate、Apply 与 Task preflight 不再各自解释 reserved metadata。概念 Interface 为：

```python
@dataclass(frozen=True)
class ValidatedWorkflowIO:
    input_contract: WorkflowInputContract
    output_contract: WorkflowOutputContract
    input_bindings: Mapping[str, Mapping[str, WorkflowInputBinding]]
    output_bindings: Mapping[str, WorkflowOutputBinding]


def validate_workflow_io(
    *,
    graph: Mapping[str, object],
    catalog: TemplateCatalogSnapshot,
) -> ValidatedWorkflowIO: ...
```

具体类名允许按现有 Module 组织调整，但必须保持一个共同 seam：

- `authoring_engine` 把 Python AST 编译成 Candidate Graph 后调用它；
- generate-python 在读取 Candidate/persisted Graph 后调用它，拒绝生成含糊 source；
- authoring validate 与 Apply 调用同一校验，不维护宽松副本；
- `task_input.preflight_task_input()` 从 exact snapshot 获得相同的 parsed Input Contract 和
  Node bindings，不再单独接受另一套 schema vocabulary；
- HTTP handler 只映射 diagnostics/envelope，不承载 domain validation。

该 Module 复用 `schema.py` 的 closed descriptor/value parser 和 Registry annotation parser，
不得复制 JSON Schema validator。它至少验证：

1. contract version、ordered unique name、closed descriptor 与 canonical schema；
2. input declaration 的 required/default/null 三种合法形状；
3. binding 引用的 parameter、Node、template、source/target Handle 均真实存在且 owner 正确；
4. static param、incoming Edge、Workflow input binding 的 provider 互斥；
5. Output Contract 与 Output Binding name 完全覆盖；
6. producer schema 可赋给 consumer/output schema。

schema assignability 必须是显式、确定性的实现：canonical equal 可赋值；integer producer
可赋给 number consumer；nullable producer 不可赋给 non-null consumer；list 递归应用同一
规则；ResourceSlot producer 的保证 allowlist 必须是 consumer allowlist 的子集，unconstrained
producer 不能满足 restricted consumer。不得通过运行样例、字段名或 Python value 猜类型。

Authoring diagnostics 使用既有结构化诊断和 source map。Core #154 尚未另行冻结 Task error
envelope 的字段级扩展，因此本轮不得偷偷向共享 `400 invalid_input` envelope 增加字段；如需
JSON Pointer diagnostics，必须先回到 Core #154/Feishu Protocol 收口。

## 5. value、default、null 与 ResourceSlot

I1 沿用已冻结的 v1 value vocabulary：

```text
str | int | float | bool | opaque dict[str, JSONValue] | ResourceSlot
list[str] | list[int] | list[float] | list[bool]
list[dict[str, JSONValue]] | list[ResourceSlot]
```

- strict no-coercion；boolean 不是 integer，非 finite number 被拒绝；
- required non-nullable 无 default；optional non-nullable 有 non-null default；
  optional nullable 只使用 `T | None = None`；
- 只有 Task request 顶层已声明字段的显式 null 等同 omission，随后由 OS 应用 default；
- Node template default 与 Workflow input default 是不同合同，不递归 materialize；
- output key 必须存在；nullable output 可以显式为 null，但 missing 不等于 null；
- opaque object 是唯一有意开放的内部 JSON 数据边界，contract/descriptor/schema 仍 closed。

外部 Task ResourceSlot 仍只能是 `{"uuid"}`。caller-supplied
`resource_template_uuid`、Material tree、bare UUID 或 sibling 字段必须拒绝。Task snapshot
中的 resolved value 才包含 authority-owned `resource_template_uuid`；collection 保持顺序和
重复，不 flatten。I1 不实现 Material lookup、Reservation、Claim 或 contention retry；M1
注入 production resolver 后不改变本 Module 与 Task HTTP Interface。

## 6. canonical Python 与 JSON fixed point

Workflow output 采用与 Action 相同的 result-record declaration：首选 `TypedDict` 或 frozen
dataclass，并保留兼容 inline return-annotation dict。一个 normalized Workflow function
具有一个确定的 result-record return annotation，最终 return expression 必须静态、完整地
提供每个声明字段，不能缺字段、多字段、条件动态增删或返回 positional tuple。

旧 `workflow_output(...)` 仅可作为限期 migration input 被 compiler 识别；它必须立即降解为
同一个 Output Contract/Bindings。generate-python 和任何 normalized source 只输出
result-record canonical form，不继续双轨。兼容入口的删除时间与 fixture 必须在本轮 round ledger
中记账。

`AllowedResourceTemplates` annotation metadata 通过 selected Template Catalog Snapshot 中的
稳定 symbol identity 解析为 UUID allowlist，generate-python 再从同一 fingerprint 反向恢复
symbol。不得从 display name 猜 UUID，也不得在 fingerprint 不匹配时用当前 Catalog 静默
重写旧 Candidate。该 slice 依赖 A1；A1 未交付前必须 fail closed，不得用空 allowlist 或
注释丢失替代 round-trip。

fixed-point gate 至少证明：

```text
Python -> Candidate Graph -> generated Python -> Candidate Graph
JSON Candidate -> generated Python -> compiled Candidate
```

两侧的 contract 顺序、schema、default/null、input/output bindings、Handle identity、
ResourceTemplate UUID、Catalog fingerprint 和 source map 语义一致。

## 7. Apply、Task snapshot 与 O1 停止线

Apply 的 transaction 顺序为：

```text
BEGIN
  -> load expected current Workflow revision and selected Catalog snapshot
  -> compile/normalize Candidate
  -> common Workflow I/O validation
  -> persist Graph + reserved contracts/bindings
  -> persist Applied Source + source map + Catalog fingerprint
  -> advance Workflow revision exactly once
COMMIT
```

任一 contract、binding、Catalog、source 或 revision 错误都必须 rollback，不能留下 Graph 已改
但 source/contract 未改的状态。Task creation 仍按 Phase 02H 在自己的 transaction 中读取
当时 persisted Graph；request 不新增 expected revision。Task response/snapshot 暴露实际观察
到的 revision，供 FE 在启动前 rehydrate 后判断是否发生竞态。

I1 只冻结 Output Contract 和 Output Bindings，为后续 O1 留下可执行合同。本轮不得：

- 从 Job return/feedback 拼装或发布 `WorkflowTask.output`；
- 在 running、failed、canceled 或其他非成功状态暴露 partial output；
- 修改 Runtime completion state machine；
- 把成功 Task 的完整原子 output 写入提前夹带进 I1。

在 O1 Accepted 前，现有非成功/未实现 output 行为保持 `{}`。

## 8. 实施切片与测试门

在治理阻塞解除后，按独立可合并 slice 推进：

1. common validator：先补 output contract/binding、identity、coverage、assignability RED；
2. atomic Apply：补 revision/source/source-map/Catalog failure injection 与 restart RED；
3. canonical Python：补 TypedDict/frozen dataclass/inline compat 与旧 helper normalization RED；
4. Catalog round-trip：A1 gate 后补 AllowedResourceTemplates symbol/UUID/fingerprint RED；
5. Task reuse：证明 02H preflight 使用同一 parsed contract，snapshot 不漂移且零 partial write；
6. cross-repo gate：与 FE Task form 和 M1 resolver 做真实 HTTP/E2E。

OS focused tests 至少覆盖：

- empty/legacy contract、完整 input/output descriptor matrix 和 unknown closed fields；
- required/default/nullable、null-as-omission、false/0/empty values、strict no-coercion；
- output binding 缺失/unknown/foreign Node/source Handle/错误 owner/错误方向；
- nullable、integer-to-number、list 与 ResourceSlot allowlist assignability；
- implicit ResourceSlot pass-through 只读与冲突；
- Python/JSON fixed point、Catalog fingerprint stale、source map 保持；
- Apply 任一点失败时 graph/source/contract/revision 全部不变；
- Task request `{uuid}`、resolved snapshot `{uuid, resource_template_uuid}`、restart readback；
- Task request 无 expected revision，snapshot 使用 transaction 实际 revision；
- I1 不改变 Runtime，不产生 partial 或成功 `WorkflowTask.output`。

最终候选必须运行 round-target、Phase 02 累积、完整 repository suite、配置的静态检查和
`git diff --check`，并把 exact tested SHA、命令、结果与 finding disposition 写入 round ledger。

## 9. Governance decision 与可移植性

[Core #158](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/158) 已 Accepted，并明确
supersede Core #104 的 2 test-author / 3 reviewer 数量要求。I1 每个 round 使用恰好一名
test-author、一名 implementation owner 和一名 reviewer；同一 round 严格串行，A1/I1/M1
可以在隔离 branch/worktree 中并行。production implementation 仍必须先取得独立 RED commit。

I/O deep Module 与 public DTO 必须保持 transport-independent：validator、canonical schema、
binding identity、diagnostics 与 fixed-point 不能依赖 FastAPI request object、SQLite row shape、
前端本地 store 或单一进程内对象。FastAPI、SQLite、Python source 与 FE services 都只是该合同
的 adapter；替换 transport、数据库 adapter 或进程部署形态不得改变 canonical JSON、错误分类
和 public seam 的行为。

## 10. Non-goals

- 不重做 Phase 02H Task input preflight、ResourceSlot resolver 或 Task transaction；
- 不加强或旁路 `@action` decorator；Action typed contract 与 Catalog owner 属于 A1，I1
  只消费其发布结果；
- 不实现 Material/Site store、Reservation/Claim、MaterialSource 或 selector；
- 不实现 Composite authoring/runtime、ExecutionPlan admission、device dispatch 或 Debugger；
- 不增加第二套 snapshot、schema vocabulary、Graph write API 或 Task request revision 字段；
- 不执行用户 Python，不导入其代码来推断 annotation，不按名称/ordinal 猜 Handle；
- 不提前实现 O1 Task output。
