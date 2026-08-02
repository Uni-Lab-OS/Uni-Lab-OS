# C1 Composite Workflow Invocation authoring implementation

## 1. Outcome 与冻结控制面

本交付让一个由 PackageCatalog 发布、且已经在同一 Graph Authority Apply 的 Workflow
以一个 typed CompositeWorkflowInvocation 插入父 Workflow。OS 负责 Published Workflow
Contract、静态展开、稳定身份、Boundary Mapping、合同兼容性、Python/JSON fixed point 与
Apply 原子门；FE 只消费完整层级图和编辑父级 boundary。

跨仓协议以 [Core #178](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/178)
冻结的 C1-P1～P5 为唯一规范，跨仓测试与接受由
[Core #179](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/179) 跟踪。本文件只冻结
OS owning-repository 的 Module、Interface、round 和验证方式，不新建协议。

实现起点为：

- `integration/workflow-task-runtime@cf6f81da8bf41950c8779555c60a7b7349184fbe`；
- PackageCatalog candidate `ba5def38e5c4c50b0c73ed7499aa33109b6c9173` 已是该基线祖先；
- 第一轮分支 `migration/c1-r1-published-workflow-contract`。

C1 继承 A1、I1、D-064、D-108～D-110。`ResourceSlot`、Workflow I/O、Action
Handle、Template Catalog 和 Applied graph 都只有现有一套 authority。Template
Catalog→Registry、authority-local no-source child、DefinitionClosure 继续 deferred。

## 2. 公共 seam 与深 Module

新增 `unilabos.workflow.composite` 深 Module。调用方只学习两个 Interface；解析、合同
摘要、静态展开、UUID、mapping、兼容性和诊断全部留在 Module 内部。

```python
class PublishedWorkflowResolver(Protocol):
    def resolve(self, module: str, symbol: str) -> PublishedWorkflowSource: ...


class CompositeAuthoring:
    def compile_invocation(
        self,
        *,
        parent_workflow_uuid: str,
        invocation_uuid: str,
        module: str,
        symbol: str,
        keyword_arguments: Mapping[str, object],
    ) -> CompositeExpansion: ...
```

`PublishedWorkflowSource` 是冻结值，只包含：

- `workflow_uuid`、`definition_fqid`；
- absolute `module` 与静态 `symbol`；
- `package_catalog_digest`、`definition_content_hash`。

production Adapter `PackageCatalogPublishedWorkflowResolver` 位于
`unilabos.package_manager.consumers`。它只从启动时显式传入的完整 PackageCatalog 集合建立
唯一 `(module, symbol)` 索引；重复、缺失、动态 source 均拒绝。它不 import/exec，不扫描目录，
不读取任意路径。测试使用 in-memory resolver Adapter。

`CompositeExpansion` 返回一次调用的完整 server-owned 结果：invocation Node、递归展开后的
Nodes/Edges、business mappings、structural mappings、引用的 Node/Handle templates、
contract pin 与 diagnostics。`authoring_engine` 是该 Interface 的 Adapter，不复制算法。

## 3. Published Workflow Contract

只有同时满足以下条件的 child 才发布：

1. PackageCatalog 中存在一个唯一 Workflow Definition；
2. Definition 具有 module、symbol、workflow_uuid 与 package provenance；
3. 同一 WorkflowStore 的 Workflow 未 soft-delete；
4. `workflow_authoring.applied_source` 存在，且其 `workflow_revision` 等于当前 Workflow
   revision；
5. Applied graph 通过现有 `validate_workflow_graph_io()`。

每个 Published Workflow 拥有一个现有 `workflow_node_template` 行：

```text
resource_template_uuid = authority-scoped host_node ResourceTemplate UUID
name                   = workflow:<workflow_uuid>
type                   = workflow
node_type              = workflow
meta_data.unilab.framework_owner_only = true
```

host_node 只承担 framework renderer owner 和 Backend 既有 FK/lifecycle；Composite 不因此
成为 host_node Action、Job 或设备 dispatch。缺少唯一 host_node ResourceTemplate 时发布返回
`composite_catalog_mismatch`，不新建 ResourceTemplate。

Node template schema 复用 A1 闭合 `goal`/`result` envelope，并增加：

```json
{
  "x-unilabos-workflow-contract": {
    "version": 1,
    "compatibility_version": 1,
    "workflow_uuid": "<uuid>",
    "workflow_revision": 7,
    "applied_source_hash": "sha256:<hex>",
    "contract_digest": "sha256:<hex>",
    "composition_allow_transparent": false,
    "input_order": ["sample", "temperature"],
    "output_order": ["report", "final_sample"]
  }
}
```

Package provenance 只放在 `meta_data.unilab.workflow_source`，字段严格为
`kind=package`、definition_fqid、module、symbol、package_catalog_digest、
definition_content_hash。

Handle 集合按顺序为 I1 inputs、I1 outputs，加现有 A1 形状的 `ready` target/source。
输入/output descriptor 继续使用一个 I1 value vocabulary；Handle metadata 使用现有
`unilab.value_schema` 与 `allowed_resource_template_uuids`。NodeTemplate identity 由
`(authority_id, resource_template_uuid, workflow:<workflow_uuid>)` 保持；Handle identity 由
`(node_template_uuid, handle_key, io_type)` 保持。

`contract_digest` 是 RFC 8785 canonical JSON 的 SHA-256。输入只含 version=1、
`composition_allow_transparent`、按声明顺序且去掉 title/description 的 I1 input/output
semantic descriptors；不含 UUID、revision、source hash、package provenance 或布局/显示字段。

## 4. Catalog coherent publication

启动时组合根先冻结 PackageCatalog resolver 与 Registry template imports，再从同一 Store
读取所有 eligible Applied Workflow Contract，最后只调用一次完整
`TemplateCatalog.replace()`。该 aggregate 同时包含 Registry Actions、framework templates、
MaterialSource/group 与 Published Workflows；Workflow publication 不得 soft-delete其他模板。

child Apply 的 publication linearization：

```text
acquire Catalog guard
  -> revalidate Candidate against observed catalog fingerprint
  -> commit child graph + Applied source
  -> while guard is still held, rebuild and replace the complete catalog
  -> release guard
```

因此 Parent compiler 只能观察 old graph+old catalog 或 new graph+new catalog。图事务提交后若
完整 replace 失败，必须在释放 guard 前把 authority Catalog 标记为 unavailable；后续 compiler
返回 `template_catalog_unavailable/composite_catalog_mismatch`，不得暴露 new graph+old
contract。OS restart 从 PackageCatalog+WorkflowStore 重建。不要用 timer、轮询或第二个
publication store 修复窗口。

## 5. Composite metadata、identity 与 mapping

invocation 是真实 `WorkflowNode`，引用 child Published Workflow NodeTemplate。其
`meta_data.unilab.composite` 是 server-owned closed v1 object，至少冻结 child UUID/revision、
Applied source hash、contract digest、mode、business mappings 与 structural mappings。

direct child Node UUID：

```python
uuid5(
    UUID(invocation_uuid),
    "unilabos:c1:node:v1:" + canonical_child_node_uuid,
)
```

固定向量：

- invocation `11111111-1111-4111-8111-111111111111`；
- child `22222222-2222-4222-8222-222222222222`；
- expanded `b6b35f79-80d0-5b77-a0eb-9646bcb36808`；
- grandchild `33333333-3333-4333-8333-333333333333`；
- nested expanded `7b221513-105e-5c92-9859-1a3c2015fafb`。

如果 direct child 是 nested Composite，派生后的 invocation UUID 是下一层 namespace。内部 Edge
不复制 child Edge UUID，继续调用现有 authoring edge rule：根 parent Workflow UUID 为
namespace，name 为
`authoring-edge:<expanded-source>:<source-handle>:<expanded-target>:<target-handle>`。
Core #178 固定 edge vector 的结果是 `b3e67370-ee6e-54b5-9dd1-6d44c5a5854f`。

business `target_mappings/source_mappings` 只映射 I1 值；ready 单独存入：

```json
{
  "structural_mappings": {
    "entry_targets": [
      {"workflow_node_uuid": "<root>", "target_handle_uuid": "<ready-target>"}
    ],
    "completion_sources": [
      {"workflow_node_uuid": "<terminal>", "source_handle_uuid": "<ready-source>"}
    ]
  }
}
```

数组按 Node UUID、Handle UUID 排序，map key 按 canonical UUID 排序。source mapping 只接受
I1 的 `workflow_input` 与 `node_output` 变体。external Edge 永远连接 invocation boundary
Handle；连接 internal private Handle 返回 `composite_external_private_edge`。

## 6. Static expansion 与验证

Module 从同一 WorkflowStore 读取一个 coherent Applied child snapshot；PackageCatalog 不提供
child graph。它递归复制 child Nodes/Edges 为 parent Candidate 的层级投影，所有 direct internal
Node 的 `parent_uuid=invocation_uuid`。child 中原有 parent relationship 映射到对应派生 UUID。

boundary target mapping 来自 child `input_contract` 与每个内部 Node 的真实
`input_bindings`；boundary source mapping 来自 child `output_bindings`，并把 Node UUID 替换为
派生 UUID。entry/terminal 只按 child DAG 的真实 root/terminal 与每个模板的 ready Handle计算。
ready-only、无业务 output child 仍保留结构顺序。

以下情况 Compile Preview fail closed，且 Apply 零写入：

- missing、soft-deleted、unapplied 或 source identity 不唯一；
- self/nested/cross-composite recursive reference；
- 展开后 parent relation 或 DAG cycle；
- mapping 引用 foreign Node/Handle、错误 owner/direction 或缺少 coverage；
- external Edge 指向 private internal Handle；
- child template/provenance 与 pinned graph 不一致；
- ResourceSlot effective allowlist 交集为空。

D-064 在 parent Compile 时逐 boundary chain 求交：omission 是 universal，显式列表做集合交集，
空交集返回 `composite_resource_constraint_empty`。FE 不计算或补写 effective allowlist。

## 7. Contract evolution

compatibility_version=1：

- exact：contract digest 相同；
- additive：mode 不变、全部旧字段保持原顺序和 semantic descriptor，只允许末尾新增带显式
  default 的 optional input，或末尾新增 output；
- breaking：删除、重命名、重排，改变 schema/null/default/implicit/ResourceSlot constraint/mode，
  或任何不满足 additive 的变化。

exact/additive 保留旧 Handle UUID，additive 仅为新增字段分配 Handle UUID。父 Candidate 必须
显式 Compile/Apply；breaking 返回 `composite_contract_stale`。revision/source hash 只 pin
implementation update，不参与兼容分类。已 Applied parent 保留自己的展开图，Task snapshot
永不读取 live child。

## 8. Python/JSON/Canvas fixed point

Python canonical form只有 absolute import、direct keyword-only call 和 named result object：

```python
from szlab_poly_studio.workflows.prepare_sample import prepare_sample

# unilab:node_uuid=<invocation_uuid>
prepared = prepare_sample(sample=sample, temperature=80.0)
```

compiler 通过 PublishedWorkflowResolver 识别调用，不 import/exec child。参数与 result fields
只来自 Published Workflow Contract。normalized Python 保留 child call，不展开内部 Actions；
Canvas/JSON 的 invocation metadata 由 generator 反查同一 provenance 恢复 absolute import。

必须证明：

```text
Python -> Candidate -> generated Python -> Candidate
JSON Candidate -> generated Python -> Candidate
Canvas boundary edit -> Candidate -> Python -> Candidate
```

contract、UUID、parent_uuid、business/structural mappings、effective ResourceSlot constraints、
catalog fingerprint 与 source map 语义保持 fixed point。

## 9. 分轮 TDD 与交付

每轮从最新 `integration/workflow-task-runtime` 建 fresh branch，严格串行使用 1 test-author、
1 implementation owner、1 exact-SHA reviewer。独立 RED commit 原样进入实现分支，不 squash。

1. **R1 Published Contract**：resolver、contract/digest、host_node owner、完整 Catalog replace、
   Apply publication linearization、startup rebuild；
2. **R2 Static Expansion**：exact UUID/edge vectors、business/structural mappings、nested/cycle、
   private Edge 与 ready-only child；
3. **R3 Authoring fixed point**：absolute call AST、compatibility、D-064、Python/JSON/Canvas；
4. **R4 Lifecycle/recovery**：exact/additive/breaking child evolution、crash/race、restart、零 partial
   graph/template write 与旧路径静态门。

每轮先跑 targeted RED，随后最小 GREEN；review 前运行 targeted、Workflow 累积、完整仓库
pytest、配置的 Ruff/format/type/compile 门和 `git diff --check`。ledger 记录 test-author、
test commit、tested SHA、命令结果、reviewer、finding disposition 和 merge SHA。

## 10. 明确不做

- R2 ExecutionPlan lowering、transparent/completion-gated readiness；
- Composite Job、nested WorkflowTask、synthetic barrier；
- O1 root `WorkflowTask.output`；
- DBG Composite step/status；
- authority-local no-source child；
- Template Catalog→Registry、DefinitionClosure、PackageCatalog visibility 重构；
- FE expanded state 持久化；
- WorkflowSourceLibrary、目录扫描、import/exec、timer/polling fallback。
