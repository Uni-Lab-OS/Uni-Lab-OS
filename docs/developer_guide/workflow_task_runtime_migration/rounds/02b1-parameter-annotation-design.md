# Phase 02B1：共享 Parameter Annotation 深模块设计

日期：2026-07-31

状态：**实现合同冻结，等待独立测试先行。**

分支：`migration/02b-annotation-schema`

基线：`ca6083b`

上游决策：

- D-082～D-091：v1 类型、默认值、约束、nullable、`Literal` 和 presentation；
- D-092：规范化 Workflow Python；
- D-100：Action 参数与 Workflow 参数使用同一个 parser；
- D-117：OS 完成生产 Authoring Interface 后，前端才进入单编辑权联调。

## 1. 本轮目标

新增一个纯 AST、无 import/执行副作用的共享深模块，使 Workflow compiler 和
Registry 后续只学习同一个 Parameter Annotation Interface：

```text
Python ast.arg + default + import map + doc metadata
                         │
                         ▼
          unilabos.registry.annotation_schema
                         │
                         ▼
      immutable ParsedParameter + deterministic AST render
```

本轮不接 HTTP、不访问 Catalog/SQLite、不修改现有 Registry 投影，不实现完整
Workflow compiler，也不实现 Action `TypedDict`/frozen dataclass result record。
Action result record 作为 02B2 独立 round，在本模块的相同有限类型 parser 上继续。

## 2. 外部 Interface

新增：

```text
unilabos/registry/annotation_schema.py
unilabos/registry/annotations.py
```

公开 Interface 保持很小：

```python
parse_parameter_annotation(
    name,
    annotation,
    *,
    default,
    imports,
    doc_title=None,
    doc_description=None,
) -> ParsedParameter

render_parameter_annotation(parameter) -> ast.expr
```

`default` 使用一个公开 sentinel 区分“未声明默认值”和显式 `None`。
`imports` 是 AST scanner 已经拥有的只读 `local_name -> module:symbol` 映射；
parser 不调用 `importlib`，不执行 `Field`、annotation、decorator 或作者模块。

`ParsedParameter` 是冻结值对象：

- 内部持有一个由 `WorkflowInputContract` parser 构造的单参数 canonical contract；
- `to_dict()` 每次返回不共享容器的 descriptor；
- `resource_templates` 使用冻结、按声明顺序保存的
  `ResourceTemplateSymbol(local_name, qualified_name)`；
- caller 不能修改 canonical schema、default 或 presentation；
- Catalog round 只读取 `qualified_name` 并解析真实 UUID，不重新解释 AST。

稳定失败统一为：

```python
AnnotationSchemaError(code, path, message)
```

不支持或不确定的 AST 必须失败关闭，不能回退成 string/object。

## 3. 有限类型语法

接受并规范化 D-082 的非空类型：

```python
str
int
float
bool
dict[str, JSONValue]
ResourceSlot
list[str]
list[int]
list[float]
list[bool]
list[dict[str, JSONValue]]
list[ResourceSlot]
```

输入兼容 `typing.List`/`typing.Dict`，render 只输出内建 `list`/`dict`。
`JSONValue` 和 `ResourceSlot` 必须能由 import map 证明来自冻结模块；仅凭同名局部
symbol 不接受。

nullable 只接受：

```python
Optional[T]
T | None
```

render 统一为 `T | None`。nullable 只能包完整顶层类型；拒绝 nullable list item、
多分支 Union、嵌套 nullable 和 `None | T | None`。

标量 enum 只接受 `Literal[...]`：

- 非空、无重复且成员只来自一个严格 scalar family；
- string、boolean、integer 分别同族；
- integer/fractional finite number 可统一为 number；
- boolean 不与 integer 混同；
- 保留声明顺序；
- nullable 必须包在 Literal 外；
- list item 可以是 Literal，list item 不可 nullable。

拒绝 `Any`、裸 `object`、bare `dict/list`、tuple、set、nested list、custom model、
bytes、datetime、Decimal、Enum class 和任意其他 Union。

## 4. `Annotated` metadata

一个参数最多有一个外层 `Annotated`。metadata 只允许：

1. 至多一个来自 `pydantic.Field` 的 `Field(...)`；
2. 仅 ResourceSlot 或 `list[ResourceSlot]` 可有一个来自
   `unilabos.registry.annotations.AllowedResourceTemplates` 的
   `AllowedResourceTemplates(...)`。

`Field`：

- 不接受 positional argument、`**kwargs` 或动态 expression；
- 只接受 `title`、`description`、`ge`、`le`、`min_length`、`max_length`；
- `title`/`description` 必须是 trim 后非空字符串；
- integer bound 必须是数学 integer；
- number bound 必须是 finite number；
- length bound 必须是非负 integer；
- lower 不得大于 upper；
- type 与 keyword 必须匹配；
- `default`、`default_factory`、alias、pattern、multiple_of、strict、
  json_schema_extra 和所有未知 keyword 都失败关闭。

`AllowedResourceTemplates`：

- 至少一个参数，全部必须是唯一的 imported Name；
- import map 必须把它解析为一个非空 `module:symbol`；
- 不接受字符串 UUID、Attribute/Call、`*args`、`**kwargs` 或局部未导入 symbol；
- parser 只保存 symbol identity，不在本轮伪造 ResourceTemplate UUID；
- render 顺序固定为 `AllowedResourceTemplates` 在前、`Field` 在后。

`annotations.py` 提供可真实 import 的 `JSONValue` 和
`AllowedResourceTemplates` metadata 类型，供 Action 源码和 normalized Workflow
源码使用；它不承担 Catalog resolution。

## 5. default 与 presentation

default 必须是 `ast.literal_eval` 可求值且属于 JSON/ResourceSlot 允许的声明默认：

- required `T`：没有默认；
- optional `T`：必须有一个满足 Schema 的非 null literal default；
- optional nullable：必须且只能 `= None`；
- required nullable、non-null `= None`、nullable non-null default 均拒绝；
- ResourceSlot 不允许非 null default；
- `list[ResourceSlot]` default 只能是 `[]`；
- opaque object/list/scalar default 继续由 `WorkflowInputContract` 的唯一严格
  validator 判断；
- tuple/set、计算表达式、Name、Call、comprehension 等不执行并拒绝。

presentation 独立解析：

- Field 和 doc metadata 各自 trim；
- 只有一方提供非空值时使用该值；
- 两方相同只保留一次；
- 冲突时 Field 胜出；
- 两方都缺失时 descriptor 不添加 title/description；
- render 把最终有效 presentation 物化到一个结构化 `Field`，不再生成 doc metadata。

## 6. deterministic render

render 从 canonical descriptor 生成一个 `ast.expr`，不得读取原 AST：

- `float` 对应 Schema `number`；
- enum 生成 `Literal[...]`；
- collections 使用 `list[...]`/`dict[...]`；
- nullable 使用 `T | None`；
- metadata 需要时生成唯一 `Annotated[...]`；
- Field keyword 顺序固定：
  `title, description, ge, le, min_length, max_length`；
- `AllowedResourceTemplates` 保留声明顺序并排在 Field 前；
- `ast.unparse(render(...))` 再 parse 必须得到同一 canonical descriptor 和
  resource symbol identities。

import 排序、alias 分配与完整函数/module render 属于 Authoring engine round；
本轮只 render annotation expression。

## 7. 测试门

独立测试作者先从公开 Interface 编写 RED，至少覆盖：

1. 全部 D-082 accepted type matrix 与 deterministic render；
2. `Optional[T]`/`T | None` 等价和非法 nullable/Union matrix；
3. `Literal` 四族、number widening、顺序和严格重复/混族/非有限拒绝；
4. `Field` 每个允许 keyword、presentation precedence 和全部禁止 keyword；
5. `AllowedResourceTemplates` 静态 import identity、顺序、重复和错误 expression；
6. required/default/null 三种合法形态及无副作用 literal 拒绝；
7. caller mutation 不改变 `ParsedParameter` 或后续 dump；
8. unsupported AST 只产生稳定 `AnnotationSchemaError`；
9. parser/render 不调用 import 或执行作者代码；
10. 原 02A Schema、Registry scanner 和正式测试不回归。

实现后运行：

```text
新增 annotation 目标测试
02A Schema/资源累计测试
tests/registry
tests/workflow
python -m pytest tests -q
Ruff E/F/I
Ruff format --check
git diff --check
```

全绿后固定候选，依次由合同、模块设计和风险三名独立 reviewer 评审。每轮仍只有
一名 subagent 运行。

## 8. 停止线

- 不修改 Backend；
- 不修改前端；
- 不接真实 Material/Catalog；
- 不让 parser import 或执行作者源码；
- 不从 runtime value、示例、字符串包含关系或旧 Registry fallback 猜类型；
- 不在本轮发布 Action result Handle、Template UUID 或 HTTP transform route；
- 任何现有 Registry caller 接线必须等 02B1 Module 单独通过门禁后另开 round。
