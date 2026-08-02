# M2A：MaterialSource graph schema 与物料链校验设计

日期：2026-08-01

实现分支：`migration/m2a-material-source-schema`

OS 基线：`integration/workflow-task-runtime@47990c98b836dfe229230e33895514fceec9a764`

交付票：[Uni-Lab-OS #8](https://github.com/Uni-Lab-OS/Uni-Lab-OS/issues/8)

跨仓合同：[Uni-Lab-Core #140](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/140)、
[Uni-Lab-Core #142](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/142)

状态：**PUBLIC SEAM CONFIRMED / AUTHORIZED FOR INDEPENDENT RED。公共 seam、静态范围和
停止线已经确认；按独立 test-author → vertical slices → 完整门禁 → 独立 reviewer 推进。**

## 1. 本轮结果

M2A 只建立可被 Python Authoring、DAG 保存、Task snapshot 和后续 M2B runtime 共用的
静态合同：

```text
resource template symbol              direct Backend-shaped Graph
          │                                      │
          ▼                                      ▼
 material_source(...)                  MaterialSource WorkflowNode
          └───────────────┬──────────────────────┘
                          ▼
             one canonical selector in node.param
                          │
                          ▼
             one ResourceSlot source Handle
                          │
                          ▼
             material edge out-degree <= 1
```

M2A 不选择或创建 Material，不创建 resolution Job，不写 Site occupancy/Reservation，也不
执行设备命令。上述能力属于 OS #9。

## 2. 已证实现状与前置条件

### 2.1 当前 integration 已具备

- `WorkflowAuthoringEngine.compile()/generate_python()/validate()` 是 AST-only、不会 import
  或执行用户源码的 Python↔Graph 唯一解释器。
- `WorkflowService.save_graph()` 与 `apply_authoring()` 是直接 DAG 和 Python Apply 的公开
  持久边界。
- `TemplateCatalog` 已提供 authority partition、稳定 Node/Handle UUID、immutable snapshot
  和 fingerprint。
- 外部 `ResourceSlot` 引用已经冻结为 closed `{"uuid": "<material_uuid>"}`；调用者不能
  注入 `resource_template_uuid`。
- M1A 已在当前 integration 提供 Material tracer，但尚未提供 Site authority。

### 2.2 前置依赖已进入 integration

1. A1 最终 accepted OS 候选 `93ec0f3b515ef00e8ee1aefe3e0e5d68706b5860`
   已通过 merge commit `47990c98b836dfe229230e33895514fceec9a764` 进入 integration；
2. A1 已提供 authority-scoped `LocalResourceTemplateIdentityIndex`，同时持有
   source identity → UUID 与 UUID → source identity 的完成态只读视图；M2A 在 public port
   层收口命名，不再维护第二份 resolver；
3. M1B 已交付 Backend-shaped `SiteRecord`、`MaterialModule.get_site()` 与 owner/template
   allowlist 事实；M2A 只新增所需的窄静态查询 port，不引入 occupancy/Reservation；
4. M1C 已将 production `ResourceSlotResolver` 接到同一 Material authority，M2A 不复制该
   runtime resolver，只冻结后续 Task snapshot 可消费的 selector。

测试可以对 ResourceTemplate identity index 与 Material/Site static-authority port 使用窄
fake，因为它们是系统边界；不得 mock Authoring Engine、Graph validator、WorkflowService、
TemplateCatalog 或 Store 内部。

## 3. 唯一持久 selector

`WorkflowNode.param` 保存以下 closed object；未知字段一律拒绝：

```json
{
  "mode": "existing",
  "resource_template_uuid": "<uuid>",
  "mount": {"uuid": "<material-uuid>"},
  "material_uuid": "<optional-material-uuid>",
  "site": "<optional-site-uuid>",
  "slot_range": ["<optional-site-uuid>", "..."],
  "flow_role": "primary_sample"
}
```

### 3.1 字段规则

| 字段 | 规则 |
|---|---|
| `mode` | 必填，closed enum：`existing` / `create_new` |
| `resource_template_uuid` | 必填、canonical non-nil UUID；是待绑定业务 Material 的模板 |
| `mount` | 必填 closed ResourceSlot reference，只允许 `{"uuid": canonical UUID}` |
| `material_uuid` | `existing` 可选；`create_new` 禁止 |
| `site` | 可选 canonical Site UUID；必须由 `mount` 直接拥有 |
| `slot_range` | 可选非空、无重复、按 UUID 规范排序的 Site UUID 数组；每项必须由 `mount` 直接拥有 |
| `flow_role` | 必填、无默认值，使用共享闭合目录 |

`site` 与 `slot_range` 互斥；都不填表示 `mount` 的全部兼容直接 Site。空数组不是“全部”，
而是非法输入。

`WorkflowNode.material_uuid` 对 MaterialSource 必须为 `null`。固定 Material 选择只允许保存在
`param.material_uuid`，避免 selector 与 Backend 旧顶层字段形成双权威。

### 3.2 flow role 共享目录

| wire value | 权威中文名称 |
|---|---|
| `primary_sample` | 主样品 |
| `aliquot_sample` | 分装样品 |
| `reagent` | 试剂 |
| `consumable` | 耗材 |

OS 公开一个共享 enum/目录对象；Graph 只保存 wire value，不保存可被节点覆盖的
`flow_role_zh`。FE 与后续 Backend adapter 消费同一目录，不各自硬编码翻译。

## 4. MaterialSource NodeTemplate 与 Handle

MaterialSource 是 Catalog 中一个真实、authority-owned framework template：

```text
class       = unilabos.workflow.authoring:material_source
name        = material_source
node_type   = material_source
type        = material_source
executor    = none
action_name = null
handles     = exactly one source Handle
```

唯一 Handle：

```json
{
  "handle_key": "material",
  "io_type": "source",
  "type": "ResourceSlot",
  "required": false,
  "data_source": "executor",
  "data_key": "material"
}
```

这里的 `data_source="executor"` 只复用现有 Graph validator 对“数据边而非 dependency-only
边”的判定，不表示 MaterialSource 有设备 executor。后续应把该内部命名歧义收口为显式
data/dependency classifier，但 M2A 不扩大该迁移。

`WorkflowNodeTemplate.resource_template_uuid` 在现有 Backend-shaped schema 中不可为空。
MaterialSource template 由 OS HostNode ResourceTemplate 作为 framework owner 发布，沿用
authority 提供的真实 UUID；它不代表被选物料模板，也不产生 executor binding。业务模板
只能读取 `node.param.resource_template_uuid`。

Catalog 不得为每个业务 ResourceTemplate 复制一个 MaterialSource template，也不得由
compiler 临时生成 NodeTemplate/Handle UUID。

## 5. Python authoring 合同

规范写法：

```python
from lab.resources import corning_96_well_plate
from unilabos.workflow.authoring import (
    MaterialFlowRole,
    material_source,
    resource_ref,
    workflow_definition,
)

@workflow_definition(
    workflow_uuid="10000000-0000-4000-8000-000000000001",
    displayname="Assay",
)
def assay_workflow():
    assay_plate = material_source(
        resource_template=corning_96_well_plate,
        mode="existing",
        mount=resource_ref("50000000-0000-4000-8000-000000000001"),
        material_uuid=None,
        site=None,
        slot_range=None,
        flow_role=MaterialFlowRole.PRIMARY_SAMPLE,
    )
    prepared = reactor.prepare(sample=assay_plate)
```

规则：

- `material_source()` 与 `resource_ref()` 都是 compile-only marker；只通过 Engine AST 解释，
  不能直接执行。
- assignment 名 `assay_plate` 写入 `WorkflowNode.name`；不是 selector 内另一份 `name`。
- `resource_template` 只接受绝对 import 的静态 symbol。Engine 通过同一 authority identity
  index 解析 UUID；不得 import 作者模块或读取运行时对象。
- `resource_ref()` 只接受一个 UUID string literal，并规范化为 closed `mount` ResourceSlot。
- `mode`、`material_uuid`、`site`、`slot_range` 是 literal；owner-relative
  `warehouse["A1:C3"]` deferred 到 Core #148。
- `flow_role` 只接受 `MaterialFlowRole.<member>`；不接受同值自由字符串，避免 Python 侧绕过
  共享目录。
- MaterialSource 变量本身就是唯一 `ResourceSlot` output，所以下游写
  `sample=assay_plate`，不写 `assay_plate.material`。

Graph→Python 时，Engine 通过 identity index 将 `resource_template_uuid` 反查为
`module:symbol` 并生成稳定 import。无法唯一反查时返回 `template_catalog_mismatch`，不得回退
到 UUID literal 或动态 import。

## 6. 双向 ResourceTemplate identity index

在 A1 单向 resolver 基础上收口一个 authority-scoped public port：

```python
class ResourceTemplateIdentityIndex(Protocol):
    def resolve_symbol(self, qualified_name: str) -> str: ...
    def identify_uuid(self, resource_template_uuid: str) -> str: ...
```

合同：

- `qualified_name` 只能是静态 `module:symbol`；
- 两个方向都返回 canonical identity，且必须互为逆映射；
- unknown、ambiguous、non-canonical 或跨 authority identity fail closed；
- index 是完成 Registry/ResourceTemplate 发布后的 detached read view；Engine 调用期间不做
  Catalog sync、网络请求、module import 或 fallback UUID 生成；
- A1 Registry→Catalog adapter 与 M2A Engine 使用同一个 index，不保留两个 resolver。

该 port 是 authoring 系统边界，不是第二个 ResourceTemplate database。真实数据仍归
ResourceTemplate Authority。

## 7. 共享静态验证

### 7.1 纯 selector 验证

Python compile、Graph Preview、direct save 和 Apply 共用一个 selector normalizer：

1. closed keys；
2. mode/`material_uuid` 组合；
3. canonical UUID；
4. closed mount ResourceSlot；
5. `site`/`slot_range` 互斥；
6. non-empty、unique、canonical candidate set；
7. required flow role；
8. MaterialSource 顶层 `material_uuid is None`；
9. template/handle identity 精确匹配 framework Catalog aggregate。

任何入口得到的 canonical param 必须 byte-for-byte 等价。

### 7.2 Material/Site authority 静态证明

WorkflowService 在 Candidate Preview、direct `save_graph()` 和 Apply 通过同一个只读 port
验证：

- mount Material 存在、未删除，且是可拥有直接 Site 的 Resource；
- exact Site 由 mount 直接拥有；
- CandidateSiteSet 每个成员都由 mount 直接拥有；
- exact/candidate/all-compatible 三种范围至少静态存在一个允许
  `resource_template_uuid` 的直接 Site；
- `create_new` 不要求当前 Site 空闲；occupancy/Reservation 属于 M2B；
- fixed `existing.material_uuid` 的存在、模板、当前位置与可用性在 M2B Task admission
  必须重验；M2A 可做只读提前诊断，但不能把 Preview 结果当作运行时事实。

2D/2.5D placement、PLR 字符串、传感器状态、occupied/unknown/reconciling 都不进入这个
静态 selector。这里使用稳定 Material/Site UUID 和 authority-owned allowlist。

## 8. 物料 edge 唯一链

普通 Material output Handle 的 graph out-degree 必须 `<= 1`：

- source Handle 的 canonical type 为 `ResourceSlot`；
- `ready`、dependency/control 和普通 scalar/object data Handle 不受此限制；
- MaterialSource 的 `material` Handle同样受限；
- 一个 ResourceSlot output 连到两个 target 时 Preview/Apply/direct save 拒绝；
- workflow output binding 只是结果投影，不是物理 consumer，不计作 graph fan-out；
- 真正分装必须由执行节点创建新的 child Material identity，并用不同 source Handle 输出；
  每个 child 后续仍各自形成唯一有序链。

M2A 只验证静态 Handle 拓扑，不靠 Reservation/Claim 把错误并行临时串行化。

MaterialSource producer 的模板保证由 `node.param.resource_template_uuid` 得出；这是
generic template 的 per-node guarantee。普通 Action producer 继续读取 Handle template 的
`meta_data.unilab.allowed_resource_template_uuids`。连接到受限 ResourceSlot target 时，现有
D-067 兼容性规则必须使用这两个来源进行静态证明。

## 9. Task snapshot 边界

M2A 不创建 Task，但必须保证 `WorkflowTask.workflow_snapshot` 日后可原样冻结：

- MaterialSource Node UUID；
- framework NodeTemplate UUID；
- `material` source Handle UUID；
- canonical selector param；
- `flow_role`；
- material edges。

不在 WorkflowNode `meta_data`、Task `meta_data` 或另一个 assignment map 复制 selector。
M2B resolution Job 的 `param` 从 snapshot selector 派生，其 `return_info` 才保存实际
ResourceSlot binding。

## 10. 稳定诊断

| 条件 | code |
|---|---|
| selector unknown key、shape、mode 组合、UUID、空/重复 range、role 非法 | `invalid_material_source` |
| framework NodeTemplate/Handle 缺失或身份不一致 | `template_catalog_mismatch` |
| ResourceTemplate symbol/UUID 不能双向唯一解析 | `template_catalog_mismatch` |
| mount/Site 不存在或已删除 | `not_found` |
| Site 非 direct child、模板不兼容、固定 Material 静态不匹配 | `material_source_conflict` |
| material output fan-out | `material_flow_fan_out` |
| Python 与 Graph 不能规范往返 | `round_trip_mismatch` |

HTTP adapter 将静态输入错误映射为既有 400/404/409 分类。diagnostic 不包含任意 selector
值、SQL、绝对路径、Registry body 或 driver exception。

## 11. 已确认 public test seam

测试只观察以下公共接口：

1. 纯 Authoring：
   - `WorkflowAuthoringEngine.compile()`；
   - `WorkflowAuthoringEngine.generate_python()`；
   - `WorkflowAuthoringEngine.validate()`。
2. 持久 Graph：
   - `WorkflowService.save_graph()` / `get_graph()`；
   - 既有 Draft→Candidate→`WorkflowService.apply_authoring()`。
3. 真实 collaborators：
   - `WorkflowStore`；
   - `TemplateCatalog`。
4. 只允许 fake 的系统边界：
   - `ResourceTemplateIdentityIndex`；
   - M1B 提供的只读 Material/Site static-authority port。

不直接测试 `material_source()`/`resource_ref()` marker 函数，不测试私有 parser/helper，不
mock Store SQL，也不通过 `_validate_*` 内部方法建立合同。

## 12. 第一条 vertical slice

独立 test author 先在 exact baseline 上只写一个端到端 RED：

```text
canonical Python MaterialSource
  -> WorkflowAuthoringEngine.compile
  -> one MaterialSource node + one ResourceSlot Handle edge
  -> generate_python
  -> compile again
  -> semantic graph equality
  -> WorkflowService.save_graph/get_graph keeps the same canonical selector
```

该 slice 只覆盖 `mode=existing`、无固定 Material、无 Site/range、一个合法 flow role 和一个
下游 Material input。GREEN 后再逐项增加 closed schema、mode matrix、Site 范围、template
compatibility、fan-out 和 persisted Apply failure 的测试。

## 13. 停止线

M2A 明确不实现：

- Material 选择/创建、Inventory 扣减、Site occupancy、Reservation；
- MaterialSourceResolutionJob 或 Task 状态机；
- sensor occupancy 与 reconciliation；
- `warehouse["A1:C3"]`、`SiteSelector` owner-relative Python 默认表达式；
- list/quantity/cardinality/group/multi-source route；
- `apply_deduct_resource`、Pick/Place/人工搬运或自动构图；
- Backend→OS authority transfer；
- FE 表单、画布和读模型；
- 设备包规范。

这些能力分别留在 OS #9–#12、FE #13–#17 和 SZLab #3–#5。
