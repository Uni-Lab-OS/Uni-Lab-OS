# 02D 工程 Round：production Authoring engine 设计基线

## 1. 目的与边界

本 Round 把旧 OS 中仍有价值的 AST-only 编译、确定性 Python 投影、source map
和诊断顺序迁移为一个新的深模块：`unilabos.workflow.authoring_engine`。模块直接产生
Backend-shaped Workflow/Node/Edge/Catalog aggregate，不发布或接受旧 Canonical DTO。

本 Round 只实现纯转换，不读写 Draft/Applied Authoring，不修改 Workflow 数据库，不创建
Task/Job，不调度设备，不访问网络，也不修改 Backend 或前端。02E 只需在该模块外增加
HTTP DTO 和 envelope，不再实现第二套 Python/Graph 解释器。

P1-2 的停止线继续有效：支持冻结的顺序、`group` 和 `parallel`；不接受 `if`、`elif`、
`else`、循环、异常处理、异步、动态调用或隐式/显式 Conditional Join。

## 2. 唯一公开入口

公开类为 `WorkflowAuthoringEngine`，构造参数只有 02C 的 `TemplateCatalog` 和
`CatalogAuthority`。它实现现有 `WorkflowService` 所需的 `AuthoringCompiler` 与
`CatalogSnapshotProvider` seam：

```python
WorkflowAuthoringEngine(
    catalog: TemplateCatalog,
    authority: CatalogAuthority,
)

engine.compiler_version: str
engine.template_catalog_fingerprint: str
engine.catalog_snapshot() -> AbstractContextManager[str]

engine.compile(
    *,
    workflow_uuid: str,
    workflow_revision: int,
    python_source: str,
    source_uri: str,
    applied_graph: dict[str, object],
) -> CandidateCompilation
```

为 02E 的另外两条纯路由提供同一语义内核，不增加 wire DTO：

```python
engine.generate_python(
    *,
    workflow_uuid: str,
    workflow_revision: int,
    graph: dict[str, object],
    source_uri: str,
) -> CandidateCompilation

engine.validate(
    *,
    workflow_uuid: str,
    workflow_revision: int,
    graph: dict[str, object],
    python_source: str,
    source_uri: str,
) -> CandidateCompilation
```

三条方法都返回现有 `CandidateCompilation`。`compile` 以 `applied_graph` 计算完整
changeset；`generate_python` 把传入 graph 视为待投影的完整 Candidate，返回
`source_only` changeset；`validate` 要求 graph 与 source 双向证明等价。02E 再把这些
领域结果投影为各自 HTTP response。

每次调用只使用一个不可变、authority-scoped 02C snapshot。若调用方已经进入
`catalog_snapshot()`，转换复用该 snapshot；独立调用则自行持有 snapshot 到结果完成。
编译期间不触发 Catalog import/refresh。公开结果只含可 JSON 序列化的 detached 值。

## 3. 错误模型与确定性

合法方法调用中的 Python syntax、authoring subset、annotation/schema、UUID anchor、
Catalog、Handle、Binding、graph 和 round-trip 失败全部返回
`CandidateCompilation.diagnostics`，不以这些用户输入错误抛异常。每条 diagnostic 只有：

```text
severity, code, message, source_range（能够定位源码时）
```

错误结果不携带可应用的 graph、normalized source 或 changeset；Catalog fingerprint 和
compiler version 仍存在。编程错误（错误的 Python 参数类型、非 UUID workflow identity）
可以抛 `TypeError`/`ValueError`，Catalog 持久层损坏继续使用 02C 的稳定错误。

给定相同 workflow UUID/revision、source/graph 和 Catalog fingerprint，三条转换必须产生
字节等价的 normalized source、排序、source map、changeset 和 diagnostics。禁止时间戳、
随机数、进程 hash、文件系统顺序或 import 执行影响结果。

## 4. Python 模块合同

编译器只调用 `ast.parse`、token/comment 扫描和 02B parser；绝不 import、decorate 或执行
Authoring 模块。

一个合法模块具有：

1. 绝对 import；禁止相对 import、`import *` 和动态 import。
2. 零个或多个 module-scope typed selector：
   `reactor: Reactor = device()` 或 `reactor: Reactor = device("reactor-1")`。
3. 恰好一个被 `@workflow_definition(...)` 修饰的普通函数。
4. 函数参数全部位于 `*` 之后，并由 02B `parse_parameter_annotation` 解析。
5. 函数体由 action assignment、`with group(name=...)`、`with parallel()` 和至多一个最终
   `return workflow_output(...)` 构成；允许一个最前面的静态 docstring。

`workflow_definition` 只接受 keyword `workflow_uuid`、`displayname`、`description`；前两项
必填、description 可省略。decorator UUID 必须等于方法参数 `workflow_uuid`。源码不拥有
revision、hash、Candidate token、catalog fingerprint、tags 或任意未声明 metadata。

`device()` 不带参数表示每个 Node 独立调度，唯一一个非空字符串位置参数表示 fixed
device。禁止 keyword、模板参数、表达式和空字符串。selector annotation 必须通过当前
模块的绝对 import 解析为 `module:symbol`。

Catalog 的现有 `WorkflowNodeTemplate.class` 是本 Round 的静态 device-symbol identity；
必须与 selector 的 `module:symbol` 完全相等，且同一 symbol/action 名在 snapshot 中只能
匹配一个 active NodeTemplate。`WorkflowNodeTemplate.name` 是 action method identity。
这不允许按 display name、大小写、字符串后缀或 runtime example 猜模板。02F importer
负责把 Registry 的真实 module symbol 发布到这个既有字段。

## 5. Node、值和 Binding 降低规则

action 语句的唯一规范形状为：

```python
# unilab:node_uuid=<uuid>
result_name = selector.action_name(keyword=value, ...)
```

必须是单个新名字接收一个 result object；禁止 tuple unpack、无接收调用、嵌套 action、
位置参数和结果变量重绑定。每个 action keyword 必须精确匹配所选模板的一个 target
Handle `data_key`（缺失时用 `handle_key`）；未知、重复或多义名称均为诊断。

value 只有三类：

- 可由 `ast.literal_eval` 得到并通过 JSON 严格验证的静态值，写入 Node `param`；
- Workflow 参数名，写入 `WorkflowNode.meta_data.unilab.input_bindings`，key 为真实 target
  Handle UUID；
- 先前 action 的 `result_name.output_name`，由 source/target Handle UUID 生成真实 Edge。

一个 target Handle 只能有一个 provider。required/default/type 的最终证明复用 Catalog
合同和现有 graph validator；编译器不从 `goal`、`result` 样例生成 Handle。

每个 action Node 直接引用 Catalog 的真实 `workflow_node_template_uuid`。Node 的
`name/status/type/pose/execution_policy/disabled/minimized` 使用一个确定性规范默认形状；
fixed selector 只写
`meta_data.unilab.executor_binding={"mode":"fixed","device_id":"..."}`，不滥用
`material_uuid` 或增加 Backend 顶层字段。

已存在的 anchor 保持 UUID。无 anchor 的新语句以 workflow UUID、规范语句结构和同结构
出现序号生成稳定 UUIDv5，并在 normalized source 中补上 anchor。重复、nil、格式错误、
不紧邻 persisted construct 或同时锚定两个 construct 的 comment 均阻止 Candidate。

Edge UUID 由 workflow UUID、两端 Node UUID 和两端 Handle UUID 生成稳定 UUIDv5。数据
Edge 直接连接真实 source/target Handle。source-order dependency 只使用 Catalog 中双方
明确发布的 dependency-only/`ready` source/target Handle；缺少真实 Handle 时返回 Catalog
diagnostic，绝不合成模板或虚拟 Handle。

## 6. 顺序、group 与 parallel

同一 lexical block 中相邻 persisted action/control construct 建立 source-order dependency，
但已有数据 Edge 已证明同一先后关系时不重复建立另一条 Edge。

`with group(name="...")` 是一个带紧邻 UUID anchor 的真实 Backend `group` presentation
Node；其直接/间接 action 子项以 `parent_uuid` 指向该 group。它需要 Catalog 中唯一的
`unilabos.workflow.authoring:group` / `group` 模板，不由编译器创建模板。执行依赖连接
group 内的真实 executable Nodes，不把 group 当 barrier。

`with parallel()` 是无 anchor、无 Node、无 Job 的纯 source structure。它只能直接包含
两个或更多 `group`，每个 group 内部顺序执行，各 branch 首项共享前驱、末项直接连接
后继。后继未消费某 branch 结果时使用该 branch 末项的 dependency-only Edge；不创建
Fork/Join Node。嵌套 parallel、空 branch 和 parallel 外逃逸 branch-local result 均诊断。

## 7. Workflow Input/Output metadata

函数 keyword-only 参数逐项复用 02B annotation parser 和 D-088 doc metadata 规则，最终
再由 02A `parse_input_contract` 验证，按声明顺序写到：

```text
Workflow.meta_data.unilab.input_contract
```

02B 返回的 `AllowedResourceTemplates` symbol 必须通过选定 authority 的显式 Catalog
identity 解析后才能替换为 UUID allowlist；当前 snapshot 无该 symbol identity 时返回
`template_catalog_mismatch`，不猜 UUID。无 allowlist 的 ResourceSlot 可正常编译。

最终 `return workflow_output(name=value, ...)` 只能出现一次且必须是函数最后一个
top-level statement。value 只能是 Workflow 参数或 action named output：

- Workflow 参数生成 `kind=workflow_input` Binding；
- `result_name.output_name` 生成 `kind=node_output` Binding，使用真实 Node/Handle UUID。

输出 schema 从 Binding 的权威生产者推导；按 keyword 顺序写
`unilab.output_contract.outputs`，Binding 写 `unilab.output_bindings`。输出名非空、唯一、
每个都必须解析，禁止 literal、表达式、whole result、tuple 和 branch-local value。
无 return 时输出合同为空、bindings 为空。

编译器只替换 Workflow/Node `meta_data.unilab` 中自己拥有的
`input_contract`、`output_contract`、`output_bindings`、`input_bindings` 和
`executor_binding`；保留其他 metadata。显示名/描述投影到现有 Workflow 字段，保留
UUID、revision、timestamps、tags 和其他非 Authoring 字段。

## 8. normalized source 与 source map

规范源码由结构化 AST/固定 formatter 生成，不保留无语义空白、别名、相对顺序差异或
`Optional` 拼法。它使用：

- 排序且分组稳定的绝对 imports；
- built-in `list[...]` / `dict[...]`、`T | None`；
- 02B renderer 产生的 `Literal`、`AllowedResourceTemplates`、`Field` 顺序；
- module-scope selector、唯一 decorator、keyword-only function；
- 一个 action 一个 result object、keyword-only call、立即相邻 UUID anchor；
- 唯一最终 `workflow_output(...)`。

source map 只包含真实 persisted Node（action 与 group），按 normalized source 起始位置及
Node UUID 排序；parallel 不产生 entry。每个范围覆盖 anchor 后对应 construct，不交叉，
并可由现有 `CandidateSourceMapEntry` 验证。

## 9. graph projection、changeset 与 round-trip 证明

结果 graph 始终具有完整五集合：`workflow/nodes/edges/node_templates/handle_templates`。
Catalog 集合是本次 snapshot 中被 Node 引用的模板及其 Handle 的稳定 UUID 排序投影；
编译器不修改 Catalog entity。

changeset 只比较 Backend write 语义字段和 server-owned reserved metadata，所有 UUID 数组
排序。没有图语义变化时为 `source_only`，否则为 `graph`。source-only 仍可返回不同但
证明等价的 normalized source。

`generate_python` 先验证 graph 只引用当前 snapshot 的真实 identity，再确定性生成源码；
`validate` 必须完成 graph 校验、source 编译，并比较规范 write entities、reserved metadata
与 Catalog identity。`compile(generate_python(graph))` 必须得到语义等价 graph；
`generate_python(compile(source).graph)` 必须得到相同 normalized source。任何一侧无法
证明时只返回 diagnostic，不能“尽量”生成可 Apply Candidate。

## 10. 明确延期

- Conditional/Join 和完整条件 AST：等待 P1-2。
- Registry/Backend Catalog 的发现、同步和 `class=module:symbol` 发布：02F。
- authority-scoped typing projection、Monaco/LSP：后续前端切片。
- implicit same-name ResourceSlot Action output 和尚未关闭的 P0-4 Catalog projection：不在
  02D 合成；只消费 Catalog 已存在的显式 Handle。
- HTTP request/response/envelope/status：02E。
- Draft/Apply 生命周期、三 token 和 SSE：已由 Phase 01 持久服务负责，不在本引擎重复。

## 11. 02D 验收门

独立测试至少覆盖：三条公开方法、一次 snapshot、无执行/无 I/O、workflow/decorator
identity、selector、anchor 稳定性、keyword-only 规则、02A/02B schema、真实 Catalog
identity、input/output Bindings、顺序/group/parallel、source map、changeset、诊断稳定性、
双向 round-trip、Catalog stale/unavailable/mismatch，以及对条件/动态 Python 的停止线。

Round 只在目标测试、Workflow/Registry 相关回归、全量测试和新增/修改文件 Ruff 均通过，
且唯一 review subagent 的 Standards/Spec 阻塞项归零后，才允许非 squash 合并。
