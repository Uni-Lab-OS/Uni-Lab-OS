# Round 02H：WorkflowTask input preflight 设计基线

## 1. 目的、基线与控制面

本 Round 从已合并 02D～02G 的
`integration/workflow-task-runtime@01380449868ccf334f4da1a28c7f6f946fb540d1`
开始，只交付 WorkflowTask input 的通用 preflight、不可变快照与真实 Handle binding。

Wayfinder control plane：

- 主功能目录：[Core #133](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/133)；
- ResourceSlot 关联目录：[Core #134](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/134)；
- OS delivery：[deepmodeling/Uni-Lab-OS#301](https://github.com/deepmodeling/Uni-Lab-OS/issues/301)，
  已作为 Core #133 的原生 sub-issue；
- 当前有效规范：D-059、D-060、D-062、D-063、D-065、D-082～D-086；
- 本轮不改变冻结 Backend-shaped `POST /api/v1/workflow-tasks` wire DTO，因此不新建
  Core Decision。

实现分支固定为 `migration/02h-task-input-preflight`。本设计提交之后，先由一个独立
test-author 在独立 worktree/branch 编写 RED，主实现才能开始。

## 2. 范围与停止线

### 2.1 本轮必须交付

1. 从同一 Task transaction 读取的 persisted
   `Workflow.meta_data.unilab.input_contract` 解析 ordered v1 Input Contract；
2. 在任何 `workflow_task` / `workflow_node_job` INSERT 前完成 unknown、missing、default、
   top-level null-as-omission、strict type 和 finite constraint preflight；
3. 规范化 scalar、opaque JSON object、同质一维 list，并返回不共享可变容器的 canonical
   Task input；
4. 把完整 resolved input 写入既有 `WorkflowTask.input`；既有
   `WorkflowTask.workflow_snapshot` 同时冻结本次 Graph、Input Contract 和 Node bindings，
   不新增第二个 snapshot 字段；
5. 用 selected Graph Authority 中真实的 target `WorkflowHandleTemplate.uuid` 解析
   `WorkflowNode.meta_data.unilab.input_bindings`，把值投影到 Task-scoped Job `param` 和
   execution plan Node `param`，绝不修改 persisted `WorkflowNode.param`；
6. 冻结一个可注入 ResourceSlot resolver port、canonical
   `{uuid, resource_template_uuid}` 结果和 400/404/409 失败分类；
7. production Material Module 缺失时，所有非空 ResourceSlot 值在 Task/Job 零写入前
   fail closed。

### 2.2 明确不做

- 不实现 Material/Site durable store、production lookup、Task Reservation、Job Claim、
  Disposition、soft delete、runtime Material projection、Mutation Session 或 ChangeSet；
- 不读取旧 Inventory，不请求 Backend，不接受 authority selector，不按字段名、display name、
  `data_key`、列表位置或 Python 类型名猜 Material/Handle；
- 不让 warning-only 或 fallback 把 Material-backed Task 送入 Scheduler；
- 不实现 MaterialSource 自动创建/选择、CandidateSiteSet 或 Admission 分配；
- 不修改 Frontend、Backend、共享 Task DTO、Graph PUT、Authoring Apply 或 Scheduler 状态机。

production Material resolver 与 Reservation 属于 M1；MaterialSource 属于 M2。空
`list[ResourceSlot]` 和 nullable ResourceSlot 的 `None` 不需要查 Material，可在 02H
成功；任何实际 UUID 都必须经过 resolver。

## 3. 深 Module 与 Interface

新增 transport-independent `unilabos/workflow/task_input.py`，把合同解析、值规范化、
ResourceSlot、binding/provider 和 Job projection 收在一个深 Module。HTTP handler、
`WorkflowService` 和 `WorkflowStore` 不各自维护一套近似规则。

外部 Interface 只有一次 preflight 和一个注入 port，概念形状如下：

```python
class ResourceSlotResolver(Protocol):
    def resolve(
        self,
        *,
        material_uuid: str,
        allowed_resource_template_uuids: tuple[str, ...] | None,
    ) -> ResolvedResourceSlot: ...


def preflight_task_input(
    *,
    graph: Mapping[str, Any],
    raw_input: Mapping[str, Any],
    execution_plan: Mapping[str, Any],
    jobs: Sequence[Mapping[str, Any]],
    resource_resolver: ResourceSlotResolver | None,
) -> PreparedTaskInput: ...
```

`ResolvedResourceSlot` 是 immutable value，只有 canonical `uuid` 与
`resource_template_uuid`。`PreparedTaskInput` 返回独立 JSON 容器中的：

- `resolved_input`；
- 绑定完成的 `execution_plan`；
- 绑定完成的 `jobs`。

具体类名可在不改变该 Interface 语义的情况下调整。删除该 Module 会迫使 contract、slot、
provider 和 projection 规则重新散落到多个 caller，因此它不是 pass-through。

02H production adapter 是明确 fail-closed 的“未配置 Material Module”实现；独立测试提供
in-memory adapter。M1 用 durable local Material adapter 替换 production adapter，不改变
Task caller 或公共 HTTP Interface。resolver 不得创建 Task、Job、Reservation 或 Material，
也不得修改传入对象。

## 4. Input Contract 与 canonical value 规则

### 4.1 Contract 读取

- `Workflow.meta_data.unilab.input_contract` 缺失时按 legacy empty contract
  `{"version": 1, "parameters": []}` 处理，以保留当前无 input Workflow 的 Task 行为；
- 一旦字段存在，必须由 `schema.parse_input_contract()` 完整 closed 解析；malformed
  `meta_data.unilab`、contract、descriptor、schema、default 或 constraint 都是
  `400 invalid_input`；
- 参数顺序按 contract 保存顺序输出；不按 caller object 顺序、名称排序或 binding 使用顺序
  重排；
- raw input 的顶层 key 必须是字符串且只允许已声明参数。unknown key 即使值为 `null` 也
  仍是 unknown，不能用 null-as-omission 绕过 closed contract。

### 4.2 Omission、null 与 default

对每个已声明参数按以下顺序处理：

1. 先完成 unknown-key 检查；
2. 声明参数的显式顶层 `null` 只在 Task input seam 视为 omission；
3. omission 后，required 参数报 `400 invalid_input`；
4. 非 required 参数填入合同已验证的 default，包括 nullable default `None` 和 list default
   `[]`；
5. resolved input 必须包含 contract 中每个参数一次，不保留 caller 未声明字段。

该等价不传播到 opaque object 内部、list item、PATCH、Workflow output 或其他 JSON seam。

### 4.3 Strict value normalization

所有 non-ResourceSlot 值复用 `schema.normalize_value()`：

- string/boolean 不 coercion；boolean 不接受 `0/1`，integer/number 不接受 bool；
- mathematical integer `3.0` 规范化为 `3`；number 只接受 finite JSON number；
- enum 先做 strict base-type validation，再按已冻结顺序判断成员；
- `minimum/maximum`、`minLength/maxLength`、`minItems/maxItems` 都是 inclusive/finite v1
  vocabulary；
- opaque object 只验证 recursively valid JSON 和预算，整对象编辑，不推断字段 schema；
- list 必须是一维同质 list；顺序和重复项保持；nested/heterogeneous/null item 均按 schema
  拒绝；
- default、raw object/list、resolved input、plan param 与 Job param 之间不共享可变容器。

## 5. ResourceSlot port 与错误分类

外部 Task value 只能是 closed `{"uuid": "<material-uuid>"}`；不接受 bare UUID、
`resource_template_uuid`、flattened tree、Material body 或未知 sibling。

对每个 non-null ResourceSlot（collection 中每个元素独立）执行：

1. `schema.normalize_value()` 先关闭引用 shape 并规范化 UUID；
2. 把 schema 中可选、非空且去重的
   `allowed_resource_template_uuids` 原样作为 tuple 传给 resolver；省略表示 unconstrained；
3. resolver 必须返回同一个 `uuid` 和 authority-owned `resource_template_uuid`；caller 提供的
   template 永不受信；
4. preflight 再关闭返回 identity，验证 template allowlist，并输出
   `{"uuid", "resource_template_uuid"}`；
5. list 保持元素顺序与重复项，不把多个 root 合成 flattened tree。

稳定错误映射：

| 条件 | HTTP / machine code |
|---|---|
| caller shape/type/UUID 非法、resolver 返回非法或 template mismatch | `400 invalid_input` |
| Material 不存在或 soft-deleted | `404 not_found` |
| disposition 不可运行、authority fenced、02H production Material adapter 未配置 | `409 conflict` |

任一失败不得写 Task/Job。错误 body 不回显 Material body、合同、resolver 异常文本或输入值。

## 6. Handle binding 与 active plan

### 6.1 Binding identity

对 Graph 中每个 Node 的 `meta_data.unilab.input_bindings` 做 closed structural validation：

- 缺失等于 `{}`；present 必须是 object；
- key 必须是 Graph snapshot `handle_templates` 中真实 target Handle UUID，且该 Handle 必须
  属于本 Node 的 `workflow_node_template_uuid`；
- value 必须严格为 `{"parameter": "<contract-name>"}`；参数在 Input Contract 中恰好一次；
- 不接受 Handle display name、`data_key`、`handle_key`、source alias 或其他字段。

disabled/out-of-scope Node 不创建 Job，但其 persisted binding shape/identity 仍必须闭合；
provider readiness 只对 execution plan 中的 active Node 判断。

### 6.2 Provider 与 Job projection

对每个 active target Handle，provider 只可能是：

1. persisted Node `param` 中该 Handle 最终 `data_key` 的 non-null 静态值；
2. active plan 中指向该真实 target Handle 的一条非 dependency-only Edge；
3. 一个 Workflow input binding。

三个来源互斥；多个 provider 为 `400 invalid_input`。required Handle 必须有一个有效
provider；binding 的 resolved 值为 `None` 时不能满足 required Handle。debug/single-node
scope 剪掉的 Edge 不再是 provider，不能从旧 Task/Job、前端缓存或任意 input key补值。

binding 使用 Handle 的最终 `data_key` 写入 Task-scoped plan Node `param` 和对应 Job
`param`。两份内容相等但不共享容器。Graph snapshot 内 persisted Node `param`、metadata、
contract 和 bindings 保持字节语义不变。

## 7. Transaction 与零 partial write

`WorkflowStore.create_task_with_jobs()` 仍在一个 SQLite transaction 中读取 Graph snapshot，
但 builder 的职责调整为先构造内存 plan/jobs，再调用本 Module preflight，并返回
`resolved_input + plan + jobs`。Store 的顺序固定为：

```text
BEGIN
  -> read exact graph/templates/handles
  -> build active plan/jobs in memory
  -> contract/value/ResourceSlot/binding/provider preflight
  -> INSERT workflow_task(resolved_input, exact graph snapshot, plan)
  -> INSERT all workflow_node_job(bound param)
COMMIT
```

preflight 或任一 INSERT 失败都 rollback。特别要证明 resolver 在第二个 slot 失败、第二个
binding 失败、required Handle 缺 provider、single-node scope 切断 Edge 时，
`workflow_task` 与 `workflow_node_job` 均为零新增。

Task request 不携带 expected revision；transaction 内观察到的 Graph 就是 snapshot。后续
Workflow/contract/binding/default 修改不得改变既有 Task input、snapshot、plan 或 Job param。

## 8. 独立测试验收

独立 test-author 至少通过公共 `WorkflowService` 和一次真实 HTTP route 覆盖：

1. absent/empty contract 与 empty input 的 legacy success；
2. ordered required/default/nullable，declared null-as-omission，以及 unknown-null 仍拒绝；
3. strict string/bool/integer/number/object/list、fresh deep copies、finite/depth/constraint；
4. malformed persisted contract/schema/default 和 raw input 全部零 Task/Job write；
5. ResourceSlot closed external shape、allowlist、canonical result、list order/duplicates；
6. injected resolver 的 `invalid_input`/`not_found`/`conflict` 与恶意返回值；
7. production 未配置 Material Module 时 non-empty slot/list fail closed，nullable `None` 与
   empty slot list 可成功，且无 Inventory/remote/fallback call；
8. real Handle UUID binding、Job/plan param projection、persisted Node param 不变；
9. unknown/foreign/source Handle、unknown binding field/parameter、static/Edge/binding ambiguity、
   required Handle 与 dependency-only Edge；
10. disabled/single-node active scope、切断 Edge、snapshot immutability、restart readback；
11. HTTP `201` Backend envelope 与 `400/404/409` frozen error envelope；
12. failure injection 证明所有错误都发生在 Task/Job 写入前。

测试不得通过直接调用私有 helper 绕过本 Module Interface，也不得修改 Backend/Frontend、
skip/xfail 既有合同或引入 fake public route。

## 9. Round gate 与合并

本轮恰好一个 test-author、恰好一个未参与测试/实现的 reviewer，且任一时刻只运行一个
subagent。reviewer 固定精确 tested SHA，按 Standards/Spec 双轴读取 production 与 tests。

主代理必须记录并通过：

- `tests/workflow/test_workflow_task_input_v1.py`；
- Task/Graph/Authoring/schema、debug-scope、Store restart 相关回归；
- Phase 02 累积目标；
- `/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest tests -q`；
- configured Ruff/format/static checks；
- `git diff --check`。

所有 blocking 修复、受影响测试与完整 gate 重新通过、reviewer 确认后，新增中文
`02h-task-input-preflight-trend.md`，记录 tested SHA、命令结果、test-author、reviewer、
finding disposition 和规模趋势。只允许非 squash 本地合并到
`integration/workflow-task-runtime`；未经用户明确授权不 push。
