# Round 02B2：Action named result record 深模块设计

日期：2026-07-31

分支：`migration/02b2-action-results`

基线：`5b7534d`

## 1. 决策来源与目标

本轮只实现 D-100 已冻结的 Action 显式 named result record：

1. 标准 `TypedDict` class；
2. 标准库 `@dataclass(frozen=True)` class；
3. Uni-Lab 兼容 dict return annotation；
4. `-> None` 表示没有显式输出。

三种有字段的声明必须归一化为同一个有序
`WorkflowOutputContract`。只改变声明形式不得改变字段顺序、Schema、
ResourceTemplate symbol identity 或后续 Catalog fingerprint 输入。

本轮复用 02B1 的有限 Parameter Annotation parser，不复制 scalar、nullable、
`Literal`、`Field`、`ResourceSlot` 或 integer budget 规则。

## 2. 停止线

本轮不做：

- 不接 `ast_registry_scanner.py`、运行时 import scanner 或 Registry 发布；
- 不解析“return annotation 中的 Name 到哪个 class”的模块查找；
- 不投影 WorkflowHandleTemplate UUID 或 Catalog fingerprint；
- 不合并旧 `@action(handles=...)`，也不决定它与新 result record 的优先级；
- 不合成 D-068 隐式同名 ResourceSlot output；
- 不生成 authority-scoped `.pyi` result view；
- 不实现 Workflow compiler、transform、generate-python、HTTP、SQLite 或 SSE；
- 不修改前端或 Backend。

未来 production caller 必须先用真实 defining module AST 构造 module-scope、
shadow-aware 的 import/definition map，再把已经解析到的 `ast.ClassDef` 或兼容
`ast.Dict` 交给本模块。这一条关闭 02B1 的 NB-01；本轮不能把旧 scanner 的
`ast.walk()` import map 当作新 Authority。

## 3. 模块边界

### 3.1 `unilabos.registry.annotation_schema`

在既有深模块增加：

```python
parse_result_annotation(
    name: str,
    annotation: ast.expr,
    *,
    imports: Mapping[str, str],
) -> ParsedResult
```

它与 `parse_parameter_annotation` 共用同一个内部 annotation parser，并通过
`parse_output_contract` 建立 canonical 单字段 Output Contract。

`ParsedResult`：

- 只能由 parser 构造；
- 持有一个 canonical 单字段 `WorkflowOutputContract`；
- 持有该字段的静态 `ResourceTemplateSymbol` tuple；
- `to_dict()` 只返回单个 output descriptor 的深拷贝；
- 不保存原 AST、声明形式、Catalog UUID 或运行时 Python type。

Output 没有 `required` 或 `default`。field annotation 的类型、约束、title、
description 和 template allowlist 与 02B1 完全同源。

### 3.2 `unilabos.registry.action_result_schema`

新增纯 AST 深模块：

```python
parse_action_result_declaration(
    declaration: ast.expr | ast.ClassDef,
    *,
    imports: Mapping[str, str],
) -> ParsedActionResults
```

caller 必须传入已经解析到 defining declaration 的节点：

- `ast.Constant(None)`：对应源码 `-> None`；
- `ast.Dict`：兼容 dict form；
- `ast.ClassDef`：TypedDict 或 frozen dataclass form；
- 缺失 return annotation、未解析 `ast.Name`、其他 expression 一律失败关闭。

`ParsedActionResults`：

- 只能由 parser 构造；
- 持有 canonical `WorkflowOutputContract`；
- 按 output 顺序持有 `(name, ResourceTemplateSymbol tuple)`；
- `to_dict()` 返回不共享容器的完整 Output Contract；
- 不暴露可被 caller 伪造为 canonical 的公开构造器；
- 不保存 TypedDict/dataclass/dict 的来源标签。

## 4. 三种声明形状

### 4.1 TypedDict

接受：

```python
class TransferResult(TypedDict):
    sample: Annotated[
        ResourceSlot,
        AllowedResourceTemplates(plate),
        Field(title="转移后样品"),
    ]
    transferred_volume: Annotated[float, Field(ge=0)]
```

约束：

- 恰好一个 base，静态 identity 必须是 `typing:TypedDict`；
- 不接受 class keywords，包括 `total=False`；
- 不接受 decorator；
- body 除一个可选 class docstring外，只能包含无 default 的简单
  `name: annotation`；
- 不接受 `Required`、`NotRequired`、`ClassVar`、方法、赋值、解包或嵌套 class；
- 至少一个字段；无输出必须写 `-> None`；
- 所有字段在成功结果中都存在。

不接受 `typing_extensions.TypedDict`。D-100 冻结的是标准 TypedDict；旧设备的
兼容迁移留给后续 Registry 接线决策，不在纯合同模块静默扩面。

### 4.2 Frozen dataclass

接受：

```python
@dataclass(frozen=True)
class TransferResult:
    sample: Annotated[ResourceSlot, AllowedResourceTemplates(plate)]
    transferred_volume: Annotated[float, Field(ge=0)]
```

约束：

- 无 base、无 class keywords；
- 恰好一个 decorator，静态 identity 必须是 `dataclasses:dataclass`；
- decorator 必须是 call，必须显式 `frozen=True`；
- 除 `frozen=True` 外，只允许可选 `slots=True` 和 `kw_only=True`；
- 不接受 positional argument、重复 keyword、`False`、计算表达式或其他 option；
- body 字段规则与 TypedDict 相同，不接受 default、`field(...)`、方法或
  `__post_init__`。

`slots`/`kw_only` 不进入 canonical contract；它们只影响设备代码中的 Python
record 实现，不改变 Workflow output 业务身份。

### 4.3 兼容 dict annotation

接受：

```python
def transfer(...) -> {
    "sample": Annotated[ResourceSlot, AllowedResourceTemplates(plate)],
    "transferred_volume": Annotated[float, Field(ge=0)],
}:
    ...
```

约束：

- 至少一个 entry；
- key 必须是非空 literal string，并且满足 canonical output name 规则；
- key 必须唯一且保留声明顺序；
- value 必须是受支持 annotation expression；
- 拒绝 computed key、非 string key、mapping unpack、重复 key 和缺失 value。

## 5. 统一 canonical 结果

每个字段先经 `parse_result_annotation` 得到 canonical descriptor，再将全部字段
一次性交给 `parse_output_contract`。最终：

```json
{
  "version": 1,
  "outputs": [
    {
      "name": "sample",
      "schema": {"$slot": "ResourceSlot"},
      "title": "转移后样品",
      "implicit": false
    },
    {
      "name": "transferred_volume",
      "schema": {"type": "number", "minimum": 0},
      "implicit": false
    }
  ]
}
```

显式 result record 的输入 descriptor 不主动写 `implicit`；既有
`parse_output_contract` canonical Authority 会为它物化 `implicit: false`。
D-068 的隐式同名 ResourceSlot output 由未来 Registry/Catalog 投影在本 parser
之后合成。

`AllowedResourceTemplates` 仍只保存 defining module 中的静态
`module:symbol` identity。本轮不解析 UUID。

## 6. 错误合同

新增稳定错误：

```text
code = "invalid_action_result"
message = "Action 结果声明不符合 Workflow 版本 1 合同"
```

path 使用声明结构，而不是 Python 行号：

- `/return`：根声明形状或 `-> None` 以外的空/错误表达式；
- `/return/bases/{index}`：class base；
- `/return/decorators/{index}`：dataclass decorator；
- `/return/fields/{index}/name`：field/key；
- `/return/fields/{index}/annotation...`：共享 annotation parser 失败；
- `/return/body/{index}`：class body 中的非字段 statement。

内部 `AnnotationSchemaError` 与 `WorkflowSchemaError` 必须被重新定位到上述 path，
不得泄漏裸 `ValueError`、`TypeError`、`KeyError`、`IndexError` 或 AST 异常。
资源耗尽与进程控制类 `BaseException` 不应被广泛捕获。

## 7. 确定性与安全不变量

- parser 不 import、eval、exec、compile 或执行 decorator/class body；
- 不调用 `typing.get_type_hints`、`inspect.signature` 或 dataclass runtime API；
- 三种 source form 产生同一 canonical bytes；
- 字段和 template symbol 顺序稳定；
- 返回对象及其 `to_dict()` dump 之间不共享可变容器；
- parser-only nominal 对象不能由 caller 伪造；
- 02B1 的 4096 位 Authoring integer budget、collision-safe enum、异常隔离与
  strict numeric 语义全部继承；
- class/docstring 大小与 AST 深度风险不通过执行 Python 放大；本轮测试必须覆盖
  深 annotation 和宽字段表的失败/增长行为。

## 8. RED 测试要求

独立 test author 必须在没有 02B2 production module 的基线上冻结：

1. 三种声明形式与 `-> None` 的 accepted matrix；
2. 三种形式 canonical equality、字段顺序和 symbol identity；
3. TypedDict base/keyword/decorator/body 的闭合拒绝矩阵；
4. frozen dataclass decorator option 与 field body 的闭合拒绝矩阵；
5. compatibility dict key/value/unpack/duplicate 拒绝矩阵；
6. output 无 default、nullable 表示 present-but-null；
7. scalar/list/nullable/Literal/Field/ResourceSlot 的完整复用；
8. parser-only、immutability、dump isolation；
9. no import/eval/exec/compile/runtime reflection；
10. 稳定 code/path/message 与深/宽对抗输入；
11. 02B1 全部 167 个测试无回归。

## 9. 完成门禁

1. 独立 RED 可计数且首因属于 02B2 缺失功能；
2. 新测试、02B1 Annotation、02A Schema、Registry、Workflow 全绿；
3. 正式 `python -m pytest tests -q` 全绿；
4. Ruff、format、`git diff --check` 全绿；
5. 中文趋势报告记录生产/测试文件数、增删行和问题趋势；
6. 合同、模块安全、最终风险三名 reviewer 顺序 0 blocking；
7. 本轮分支以非 squash merge 合入 integration 后再次全量通过。
