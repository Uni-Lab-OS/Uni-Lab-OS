# Round 02B1：Parameter Annotation 独立合同评审

日期：2026-07-31

评审分支：`review/02b1-contract`

基线：`ca6083badf9ac7db299b30c4f2999f1f32f6a445`

固定候选：`097d0df26d4555be42bde0889153e5596d83f2dd`

评审角色：顺序独立评审 1/3。Reviewer 未参与本轮设计实现或测试编写；本报告
不修改 production、测试、前端或 Backend。

## 1. 结论

**Blocking 数为 0；固定候选可以进入顺序评审 2/3。**

| 评审轴 | Blocking | Non-blocking | 结论 |
|---|---:|---:|---|
| Spec / contract | 0 | 1 | 通过 |
| Repository Standards | 0 | 0 | 通过 |

唯一 non-blocking 是未来 Registry/Compiler 接线时必须提供模块作用域且能识别名称
遮蔽的 import map；02B1 按冻结停止线尚未接任何生产 caller，因此该风险当前不可达。

## 2. Spec 来源与范围

本次逐项核对：

- `AGENTS.md:344-406` 的 Workflow Frontend Interface Authority 注解合同；
- `decisions.md:2253-2814` 的 D-082～D-092；
- `decisions.md:3140-3205` 的 D-100；
- Core `Uni-Lab-Core#139` 的 D-117；
- `02b1-parameter-annotation-design.md:18-243`；
- `ca6083b...097d0df` 的完整 production/test diff。

本轮设计在
`02b1-parameter-annotation-design.md:33-35,235-243` 明确排除 HTTP、Catalog、
SQLite、现有 Registry 投影、完整 Compiler、Action result record、前端和 Backend。
候选遵守该停止线，没有把后续接口或持久状态提前塞入本模块。

## 3. Spec findings

### C-01 有限类型、nullable、Literal 与 Field：accepted-fixed

`annotation_schema.py:186-254` 只接受冻结的 scalar、opaque object、
`ResourceSlot` 和一维同质 list；`JSONValue`、`ResourceSlot`、`Optional`、
`Literal`、`Annotated` 与 `Field` 都要求 import map 中的精确 identity。未知类型
在 `annotation_schema.py:216-217,254` 失败关闭，没有继承旧 Registry 的
string/object fallback。

`annotation_schema.py:153-183` 将 `Optional[T]` 和 PEP 604 nullable 归一为同一个
canonical `anyOf`，并阻止 list item nullable、嵌套 nullable和任意多分支 Union。
渲染在 `annotation_schema.py:510-559` 统一输出 `T | None`、内建
`list`/`dict` 与 `Literal`。

`annotation_schema.py:107-150` 严格区分 boolean、integer 与 number，拒绝空、
重复、混族、null、非有限或非 scalar Literal，并保留顺序。
`annotation_schema.py:287-355` 只接受冻结 Field keyword，验证类型匹配、有限数值、
非负长度和上下界。

对测试未直接组合的边界做了只读复现：

```text
Annotated[Literal[1, 2], Field(ge=1, le=2)]
=> 接受并确定性 round-trip

Annotated[Literal[1, 2], Field(ge=2)]
=> invalid_schema /parameters/0/schema/enum/0

Literal[1, 2] = 3
=> invalid_contract /parameters/0/default
```

这证明 enum member、Field constraint 与 default 最终仍由同一个 canonical 合同
交叉校验，不是三个互不一致的局部判断。

### C-02 默认值与 presentation：accepted-fixed

`annotation_schema.py:452-503` 只用 `ast.literal_eval` 读取默认值，然后把完整
descriptor 交给 `parse_input_contract`。因此 required、非 null default、
nullable `= None`、ResourceSlot 禁止非 null default、`list[ResourceSlot]`
只允许 `[]`，以及 JSON 严格值验证均复用 `WorkflowInputContract`。

`annotation_schema.py:270-276,312-320,443-483` 独立 trim Field/doc 的有效内容并
执行 Field 优先；`annotation_schema.py:572-599` 以冻结顺序渲染有效
presentation 和约束。没有使用 `Field(default=...)` 替代实际 `=` 默认值。

### C-03 ResourceTemplate identity：accepted-fixed

`annotation_schema.py:358-387` 只接受 `ResourceSlot` 或
`list[ResourceSlot]` 上非空、唯一的 imported Name，并只保存有且仅有一个冒号的
`module:symbol` identity；字符串 UUID、Attribute、Call、`*args`、`**kwargs`、
局部未导入 symbol 和重复 identity 均失败关闭。

`annotation_schema.py:562-569,602-630` 保留 symbol 声明顺序，并在确定性输出中把
`AllowedResourceTemplates` 放在 `Field` 前。本轮没有伪造
ResourceTemplate UUID，也没有访问 Catalog；真实 UUID resolution 正确留在后续
Catalog round。

额外只读复现：

```text
Annotated[list[ResourceSlot] | None,
          AllowedResourceTemplates(plate)] = None
=> canonical nullable list + 静态 lab.resources:plate identity；
   render 后 descriptor 与 identity 均不变
```

### C-04 parser 静态性和 canonical 单一真值：accepted-fixed

`annotation_schema.py` 没有 `importlib`、`eval`、`exec`、reflection 或作者模块
加载。`annotation_schema.py:100-104` 只做 literal AST 读取；
`annotation_schema.py:498-503` 委托 `WorkflowInputContract` 构造 canonical
contract；`annotation_schema.py:48-58` 用不可变 contract 持有参数，并在每次
`to_dict()` 时返回独立容器。

渲染只读取 canonical descriptor 和冻结的
`ResourceTemplateSymbol`，不读取或执行原 AST。测试
`test_annotation_schema_v1.py:1018-1030,1081-1126` 分别验证原 AST 修改不能改变
结果，以及 import/eval/exec/作者 expression 均不被执行。

因此“实现又复制一套 Workflow default/value validator”以及“未知类型回退后让错误
实现通过”两个假设均为 `rejected-with-evidence`。

### C-05 D-100、D-117 与 scope creep：rejected-with-evidence

本轮只提供后续 Workflow compiler 与 Action Registry 可共同调用的 Parameter
Annotation Interface；没有修改旧 Action parser、发布 result Handle 或提前决定
Action output。该边界符合 D-100 仍把 result record 和 catalog projection 留给后续
决策的要求。

候选没有 HTTP、Draft、Candidate、Apply、FE、Backend 或编辑模式变化，因此没有
改变 D-117 的单编辑权、Draft 双 CAS、server-owned Candidate、单
`candidate_hash` Apply 或独立 FE 分支约束。

## 4. Non-blocking finding

### NB-01 后续 import map 必须是模块作用域且 shadow-aware

**Disposition：`non-blocking-follow-up`**

02B1 parser 正确把 `imports` 当作可信、只读的
`local_name -> module:symbol` 输入（设计 `:62-64`）。但独立测试 helper 在
`tests/registry/test_annotation_schema_v1.py:58-70` 使用 `ast.walk(tree)` 收集
所有层级的 import；尚未接线的旧 Registry scanner 在
`unilabos/registry/ast_registry_scanner.py:533-555` 也会遍历嵌套 import，并且其
本地名称盘点没有覆盖 `AnnAssign`。

最小只读复现表明：若 caller 把函数体内的
`from pydantic import Field` 错装进扁平 import map，parser 会把该 identity 当作
已证明；parser 无法从已丢失作用域的信息中恢复真实 lexical scope。

这不是当前候选的 blocking，原因是设计
`02b1-parameter-annotation-design.md:243` 明确要求现有 Registry caller 接线另开
round，当前 diff 也没有任何生产 caller。后续接线 round 必须：

1. 只从合法模块作用域构造 import map；
2. 识别 `Assign`、`AnnAssign`、class/function 等名称遮蔽；
3. 增加“嵌套 import 不证明注解 identity”和“遮蔽 builtin/helper 后失败关闭”的
   集成测试。

在该接线完成前，不得用 02B1 的纯 parser 测试宣称完整 Workflow/Action module
静态名称解析已经交付。

## 5. 测试审计

逐行检查了新增的 1126 行合同测试。测试覆盖：

- 完整 accepted type matrix、`typing.List`/`typing.Dict` 输入与 canonical render；
- 两种 nullable、非法 Union、nullable list item；
- Literal 四族、number widening、重复/混族/非有限与严格 bool/int；
- Field 全部允许和禁止 keyword、顺序与 presentation precedence；
- ResourceTemplate symbol identity、顺序、重复及动态 expression 拒绝；
- 三种默认形态、可执行或非 JSON default 拒绝；
- canonical/dump 深不可变；
- 稳定错误以及 import/eval/exec 零调用。

现有 02A `WorkflowInputContract` 测试继续守护默认值、enum membership、约束和
canonical JSON 的单一真值。除 NB-01 的未来 caller lexical scope 外，没有发现会
让当前错误实现通过的合同缺口。

## 6. Standards

production diff 只新增两个职责集中的模块：

- `annotation_schema.py` 聚合 syntax-to-canonical 与 deterministic render；
- `annotations.py` 只提供源码可 import 的有限辅助类型。

注释和 docstring 使用简体中文，新增 Python 均有类型标注；没有修改 Backend/FE，
没有复制 HTTP/persistence/Catalog 逻辑，也没有发现本轮 diff 的硬性 Standards
违反或需要升级为 blocking 的代码气味。

由于本轮要求顺序独立 reviewer 且禁止 subagent，本次在一个 reviewer 内分开执行
Spec 与 Standards 两轴，没有启动并行 reviewer。

## 7. 门禁

使用固定解释器
`/home/changjunhan/.micromamba/envs/unilab/bin/python`：

```text
python -m pytest -q tests/registry/test_annotation_schema_v1.py
=> 127 passed in 0.74s

python -m pytest -q \
  tests/workflow/test_route_body_budget.py \
  tests/workflow/test_json_resource_budget.py \
  tests/workflow/test_schema_codec_hardening.py \
  tests/workflow/test_value_schema_hardening.py \
  tests/workflow/test_value_schema_v1.py
=> 212 passed, 2 warnings in 2.36s

python -m pytest -q tests/registry
=> 153 passed in 2.54s

python -m ruff check --select E,F,I --ignore E501 \
  unilabos/registry/annotation_schema.py \
  unilabos/registry/annotations.py \
  tests/registry/test_annotation_schema_v1.py
=> All checks passed

python -m ruff format --check \
  unilabos/registry/annotation_schema.py \
  unilabos/registry/annotations.py \
  tests/registry/test_annotation_schema_v1.py
=> 3 files already formatted

git diff --check ca6083b...097d0df
=> passed
```

## 8. 下一步

固定候选 `097d0df26d4555be42bde0889153e5596d83f2dd` 为 **0 blocking、
1 non-blocking**，可以进入顺序独立评审 2/3。任何 production 或测试修改都会产生
新候选 SHA，并使本评审失效。
