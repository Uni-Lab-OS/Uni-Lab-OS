# A1：现有 `@action` typed contract 与 Catalog 投影实现设计

日期：2026-08-01

实现分支：`migration/a1-action-catalog-e2e`

OS 基线：`integration/workflow-task-runtime@21e42beee58062abcf3417841e2db4c44a154dc9`

跨仓协议权威：[Uni-Lab-Core #153](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/153)

跨仓验收门：[Uni-Lab-Core #159](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/159)

状态：**HUMAN APPROVED / IMPLEMENTATION AUTHORIZED — 2026-08-01。用户已完成本 spec
评审，并明确允许在经过 FE-OS migration、无 local bridge 的 F006 integration 基线上继续。**

## 1. 结果与唯一扩展方向

A1 只深化既有 Registry Action module：

```text
显式 @action method
  -> 既有静态 AST scanner
  -> action_value_mappings[action_name].schema
  -> local Registry-to-Catalog adapter
  -> 既有 TemplateCatalog.replace()/snapshot()
  -> Authoring compiler 与 Backend-shaped read projection
```

`@action` 是唯一 Workflow-capable Action marker。方法参数 annotation/default 与具名
return annotation 是业务 typed contract 的唯一来源；A1 不增加 decorator、DSL、contract
registry、Catalog 或第二套 schema。

现有 `action_value_mappings[action_name].schema` 继续是唯一 wire schema envelope。typed
schema 只能向后兼容地增强它；禁止增加 sibling `action_contract`，也禁止把既有 parser 的
`input_contract`/`output_contract` dump 复制进 action record 成为第二权威。

## 2. 当前基线证据

- `unilabos.registry.action_contract_schema.parse_action_contract()` 已以只读 AST 组合参数、
  TypedDict/frozen dataclass/inline dict result、default/null、Literal、Field、ResourceSlot 和
  `AllowedResourceTemplates`；当前只有测试调用，尚未接入 production scanner。
- `ast_registry_scanner.py` 仍分别收集 decorator args、松散 params 和 return type；
  `registry.py` 仍用旧 generator、ROS schema、`goal_default` override 与
  `@action(handles=...)` 构造 action record。
- 未装饰 public method 仍会生成 `auto-*` action。该兼容能力不等于 typed Workflow
  Action。
- `TemplateCatalog` 已拥有 authority partition、事务性完整 `replace()`、稳定 local UUID、
  `(workflow_node_template_uuid, handle_key, io_type)` Handle business identity、immutable
  `snapshot()` 与 fingerprint；它明确不负责发现 Registry 或重新解析 Action。
- compiler 已只读持久 Catalog snapshot，并把 fingerprint 绑定 Candidate。缺口是一个
  Registry schema 到既有 Catalog importer 的 production adapter，而不是另一个 Catalog。

## 3. 范围与停止线

### 3.1 本轮范围

1. 只对 scanner 已证明存在显式 Uni-Lab `@action` 的方法调用既有 strict parser。
2. 把 parser 结果确定性投影进现有 action record 的 `schema`、兼容
   `goal_default` 和兼容 `handles`。
3. 建立一个 local Registry-to-Catalog adapter，把显式 typed Action 完整投影为既有
   `NodeTemplateImport` aggregate。
4. 在投影时解析 ResourceTemplate symbol、生成真实 typed Handle，并合成 ResourceSlot
   隐式同名 output。
5. 用既有 `TemplateCatalog.replace()` 原子发布 local authority snapshot；compiler 与
   frontend read adapter 读取同一个 fingerprint/snapshot。
6. 增加稳定 Registry/Catalog diagnostics 和 schema/legacy-handle conformance inventory。

### 3.2 明确不做

- 不修改 device dispatch、Scheduler、ExecutionPlan、WorkflowTask/Job、Action result commit、
  Reservation、Claim、ChangeSet、Task output 或 Debugger。
- 不实现 M2 MaterialSource、自动资源选择或创建。
- 不自动把未装饰 public method 升级为 typed Action。
- 不从字段名、Action 类型、runtime example、端口 ordinal、ROS message 顺序或旧 YAML
  猜测 typed contract。
- 不让 compiler、HTTP handler 或 frontend 临时同步、修补或生成 Handle。
- 不修改 Backend，不增加 `/action-catalog` 或其他平行 public resource。
- 本轮在独立 RED 与 exact-SHA review 门内修改 production/test source。

## 4. 现有 decorator schema 的兼容增强

### 4.1 唯一 schema envelope

既有外形保持：

```json
{
  "title": "heat参数",
  "description": "...",
  "type": "object",
  "properties": {
    "goal": {
      "type": "object",
      "properties": {},
      "required": [],
      "additionalProperties": false
    },
    "feedback": {},
    "result": {
      "type": "object",
      "properties": {},
      "required": [],
      "additionalProperties": false
    }
  },
  "required": ["goal"],
  "x-unilabos-action-contract": {
    "version": 1,
    "input_order": [],
    "output_order": [],
    "resource_template_symbols": {
      "goal": {},
      "result": {}
    }
  }
}
```

规则：

- `goal.properties` 直接承载参数 value schema；`goal.required` 与 property `default` 来自
  Python declaration，不从 decorator `goal_default` 反推。
- `result.properties` 直接承载显式 named result value schema；每个声明字段都进入
  `result.required`。nullable 表示 key 必须存在而值可为 `null`。
- `-> None` 产生 closed empty result；opaque bare `dict` 不产生猜测字段。
- `feedback` 继续是 transport schema，不参与 Workflow typed input/output。
- 唯一保留扩展区名为 `x-unilabos-action-contract`，`version` 固定为整数 `1`。
  `input_order`/`output_order` 只补充 JSON object 不表达的声明顺序，不复制字段 schema。
- `resource_template_symbols` 只保存 strict parser 已静态证明的 defining-module symbol
  identity，供 local Catalog adapter 解析 UUID；不得执行作者模块或 decorator。authority
  projection 后，Handle metadata 使用已解析的 `allowed_resource_template_uuids`。
- `unilabos_device_id`、busy state、timestamps、placeholder presentation 注入不属于 typed
  contract，不得进入上述扩展区或 Catalog fingerprint。
- 未识别的扩展版本 fail closed；旧消费者忽略新增 `x-*` 字段后仍可读取原 envelope。

### 4.2 `goal_default` 是兼容投影

既有 sibling `goal_default` 暂时保留给当前 transport/UI 调用方，但只能由同一 canonical
goal schema 确定性生成。`@action(goal_default=...)` 不再覆盖 typed declaration；inventory
阶段若发现其值与 Python default 不同，阻止该 Action 的 typed Catalog 发布。

### 4.3 ROS mapping 是 transport adapter

既有 `goal`、`feedback`、`result` mapping 与 ROS action type 可继续负责 Python 参数/结果
到 ROS message 的传输适配。它们不能添加、删除、重命名或改变 typed business fields。
ROS message schema 与 Python declaration 不一致时 fail closed，不能让 ROS reflection
反向覆盖 `@action` schema。

## 5. `handles` 兼容断言与 auto-action

### 5.1 `@action(handles=...)`

非空 legacy `handles` 只作为迁移 assertion：

- 先从 schema 投影完整 input、explicit output 与 implicit ResourceSlot output Handle；
- 再规范化 legacy declaration，并比较 `handle_key`、`io_type`、typed value、required、
  `data_source` 与 `data_key` 等业务字段；label/description 等展示字段不取得权威；
- 完全等价时允许发布，并由 schema 生成兼容 `.handles`；
- 缺失、额外或冲突 Handle 时返回 `action_handle_contract_conflict`，整个新 Catalog
  replace 不发生；不得 union、按 ordinal 对齐或选择任一来源。

空或未提供 legacy `handles` 不构成声明；完整 Handle 集合仍由 schema 生成。A1 不删除
decorator 的 `handles` 参数，以便存量迁移，但 Workflow 路径不得直接消费它。

### 5.2 auto-action

未装饰 public method 生成的 `auto-*` action 保持当前直接设备调用兼容：

- scanner 不对它调用 `parse_action_contract()`；
- 不写 `x-unilabos-action-contract`；
- Registry-to-Catalog adapter 必须排除它；
- frontend typed Catalog、compiler 与 Workflow Apply 不得看到它。

需要进入 Workflow 的 method 必须显式增加 `@action` 并通过相同严格门，不能由配置或
frontend 临时 opt-in。

## 6. Registry-to-Catalog 深模块 seam

新增的 adapter 只有一个外部 Interface：输入一个完成 Registry build 的只读 snapshot、
local `CatalogAuthority` 与已有 ResourceTemplate identity resolver，返回完整
`Sequence[NodeTemplateImport]`；调用方随后只调用一次既有 `TemplateCatalog.replace()`。

复杂性全部留在 adapter implementation 内：

1. 只选择带 version-1 extension 的显式 `@action` record；
2. 解析 Action 所属 ResourceTemplate UUID，Action template identity 不包含在线 device
   instance；
3. 将 goal/result schema 投影为真实 target/source Handles；
4. 按 schema order 生成确定性 aggregate；
5. 为每个 ResourceSlot input 在没有兼容同名显式 output 时合成同名 source Handle；
6. 解析 `AllowedResourceTemplates` symbol 并写入 Handle
   `meta_data.unilab.allowed_resource_template_uuids`；
7. 为 frontend 写入确定性 `editor_control`：ResourceSlot 为 `material_port`，Site selector
   为 `site_selector`，其他 typed value 为 `variable_selector`；
8. 将完整 aggregate 交给既有 Catalog 负责 UUID、soft-delete、事务与 fingerprint。

Handle 继续使用既有 business identity。删除后重新引入是否获得新 UUID，完全沿用
`TemplateCatalog` lifecycle，不在 adapter 中发明 UUIDv5 或缓存。

编译器只跨 `TemplateCatalog.snapshot()` Interface。它不认识 Registry、decorator 或 legacy
handles；删除 adapter 时复杂度会重新散落到 scanner/compiler/HTTP，因此该 seam 具有实际
depth 与 locality。

## 7. Catalog read seam

- frontend 继续使用已约定的 Backend-shaped `GET /api/v1/workflow-node-templates` 资源；
  不增加第二个 Action Catalog path。
- HTTP adapter 必须从当前 selected authority 的 persisted `TemplateCatalog.snapshot()` 读取
  Node/Handle DTO 与 fingerprint，不能读取 HostNode live mapping、online device 或另一个
  Registry projection。
- public DTO 保留真实 NodeTemplate UUID、Handle UUID、parent UUID、`handle_key`、
  `io_type`、type、required、data source/key、schema/metadata 和 catalog fingerprint。
- exact envelope/list-detail 形状必须在 Core #153 的跨仓 integration spec 中与 FE 固定；
  OS implementation 不得为了适配旧 FE fixture 发明 `id` 字符串并要求客户端拆分。
- fingerprint 与 compiler Candidate 使用同一 snapshot 值；schema/default/Handle 合同改变
  必须改变 fingerprint，online/busy/timestamp 改变不得改变它。

## 8. 诊断与原子失败

| 条件 | 稳定 code / 既有 code | 失败位置 |
|---|---|---|
| Action AST/annotation/result 非法 | parser 原 `invalid_action_contract`、`invalid_annotation`、`invalid_action_result`、`invalid_schema` | `/actions/{action_name}/...` |
| legacy defaults 与 declaration 冲突 | `action_default_contract_conflict` | `/actions/{action_name}/goal_default/{field}` |
| legacy handles 与 schema 冲突 | `action_handle_contract_conflict` | `/actions/{action_name}/handles/...` |
| ResourceTemplate symbol 无法解析 | `template_catalog_mismatch` | `/actions/{action_name}/schema/...` |
| import aggregate 非法 | 既有 `template_catalog_mismatch` | Catalog identity path |
| fingerprint 已变化 | 既有 `template_catalog_conflict` | `/authority/fingerprint` |
| authority 尚无成功 snapshot | 既有 `template_catalog_unavailable` | `/authority/catalog` |

错误不得包含作者源码、任意默认值、SQL、绝对路径或 driver exception。一次 local Catalog
projection 任一 Action 失败时，不发布部分 snapshot；production composition 也不得把旧
snapshot 冒充当前 Registry 的成功同步结果。

## 9. 建议实现切片

1. **A1-OS-0 inventory**：对全部显式 `@action` 产出 parser/default/handles/ROS conformance
   报告；auto-action 只计数，不升级。
2. **A1-OS-1 schema wiring**：scanner 调用既有 strict parser并原位增强 `.schema`；保持
   decorator marker、AST-only 和旧直接设备调用。
3. **A1-OS-2 compatibility projection**：单一 schema 生成 `goal_default`/`.handles`，存量
   declaration 只作 assertion。
4. **A1-OS-3 Catalog adapter**：Registry snapshot 到完整 `NodeTemplateImport`，解析资源
   symbol、隐式 pass-through、稳定 order，并一次 replace。
5. **A1-OS-4 read adapter**：从同一 Catalog snapshot 提供 Backend-shaped read DTO 与
   fingerprint；补齐跨 FE integration fixture。

这些切片属于同一个 A1 owning round；不得把未合入的切片继续堆成新 round。整轮使用恰好
一名独立 test-author 与恰好一名独立 reviewer，任何切片都不能跳过 RED 或最终门禁。

## 10. 测试与接受门

独立 RED 至少覆盖：

- 只有显式 `@action` 进入 typed Catalog，auto-action 继续旧 runtime 可见但 Catalog 不可见；
- sync/async method、positional-only/keyword-only、required/static default/nullable、
  Literal/Field/object/list/ResourceSlot；
- TypedDict、frozen dataclass、inline dict 与 `-> None` 的同一 normalized result；
- schema 只新增 frozen `x-unilabos-action-contract`，无 sibling contract；
- `goal_default` 和 legacy handles 等价/冲突，冲突无部分 Catalog replace；
- ROS mapping 不反向改写 typed schema；
- ResourceTemplate symbol resolved/unresolved/stale、implicit pass-through 与 Site metadata；
- 重复 replace、重启、声明形式等价时 UUID/fingerprint 稳定；default/schema/Handle 改变时
  fingerprint 改变；
- compiler 与 HTTP read 使用同一个 snapshot/fingerprint；
- malformed/unknown extension version、错误 Handle parent、缺失 template fail closed；
- tests/registry、tests/workflow、完整 tests、Ruff、format 与 `git diff --check` 全绿。

Core gate 还必须固定 OS/FE exact SHA，证明 Python/JSON/DAG 端口一致、保存/刷新 identity
不漂移、旧 fingerprint 产生 409、非法类型/缺参/错误 Handle 明确拒绝，以及 production
source 中字段名/Action 类型/ordinal 猜测为零。

## 11. 本轮授权与门禁

用户在本会话中先要求 spec 经人工评审，随后逐项确认无异议，并明确授权开始实现；同时
指定必须以 F006 已完成 FE-OS migration、无 local bridge 的 integration 为基线。当前仓库
`AGENTS.md` 的 owning-round 规则因此作为本地执行门：

- 从已合入 F006 的 integration 创建新 A1 分支，不堆叠未合入 implementation round；
- 恰好一名独立 test-author 先提交 OS/FE RED；
- production 完成并通过 OS/FE/SZLab 门禁后，恰好一名独立 reviewer 审 exact SHA；
- spec、RED、实现与 review evidence 全部留在本地分支；除非用户另行要求，不修改远端
  GitHub issue 或 stage。
