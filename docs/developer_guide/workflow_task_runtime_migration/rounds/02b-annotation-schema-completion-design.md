# Round 02B：Annotation Schema production caller 收尾设计

日期：2026-07-31

分支：`migration/02b-annotation-schema-completion`

基线：`5e49b5f`

状态：**Interface 冻结，等待唯一独立测试作者提交 RED。**

## 1. 为什么 02B 尚未结束

02B1 已提供 Parameter Annotation parser，02B2 已提供 Action named result
parser，02B3 已提供静态、module-scope、shadow-aware resolver；但三个 Module
尚未由一个 production caller 组合成 D-100 的完整 Action Contract。

因此 02B 的完成条件不是“又存在一个 parser”，而是一个真实 Action 定义可以通过
同一个小 Interface 得到 canonical input/output contracts。该 Interface 是 02C
Catalog projection 与 02D Workflow Compiler 的共同上游。

## 2. 确认的测试 seam

唯一对外 seam：

```python
parse_action_contract(
    module: ast.Module,
    action: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    module_name: str,
) -> ParsedActionContract
```

输入是已经由调用方读取并通过 `ast.parse()` 得到的真实 defining module AST，以及
其中一个 Action method/function 节点。Module 不读取文件、不 import 作者模块，也不
执行 decorator、default、annotation、class body 或 result type。

结果只暴露：

```python
contract.to_dict() -> {
    "input_contract": {"version": 1, "parameters": [...]},
    "output_contract": {"version": 1, "outputs": [...]},
}
contract.input_resource_templates
contract.output_resource_templates
```

两个 template 集合按参数/结果字段顺序保存静态 `module:symbol`，交给 02C 解析真实
authority-scoped ResourceTemplate UUID。返回对象与每次 dump 均不可被 caller 修改。

这就是 TDD 的已确认 seam：测试只调用 `parse_action_contract()` 并观察返回值或稳定
错误，不 mock `module_scope`、Parameter parser 或 Result parser 内部协作者。

## 3. Action 参数合同

caller 从 `ast.arguments` 按 Python 声明顺序收集 positional-only、普通 positional
和 keyword-only 参数：

- class method 的首个 `self`/`cls` 以及既有 framework-owned `sample_uuids` 不进入
  Action Contract；
- `*args`、`**kwargs`、无注解参数和 forged `ast.arguments` 失败关闭；
- positional defaults 按 Python 尾对齐规则匹配，keyword-only default 按索引匹配；
- 每个参数直接调用 `parse_parameter_annotation()`，不复制类型、default、nullable、
  Literal、Field 或 ResourceSlot 规则；
- `ast.get_docstring()` 只读取 literal docstring，再按既有 D-088 Google-style
  `Args:` 语义提供 `doc_title/doc_description`；Field 仍具有最终优先级；
- 最终一次性交给 `parse_input_contract()`，保持参数顺序和 closed contract。

## 4. Action 结果合同

- `-> None` 和兼容 inline dict 直接交给 `parse_action_result_declaration()`；
- `-> ResultName` 必须由同一个 `ResolvedModuleScope.definitions` 唯一证明为最终
  module-scope `ast.ClassDef`；被 import、赋值、删除、条件绑定或其他定义遮蔽时失败；
- Attribute、字符串 forward reference、调用或其他动态 expression 不猜测；
- 缺失 return annotation 不等同 `-> None`，必须失败关闭；
- Result parser 使用同一个 `scope.annotation_bindings`，因此 TypedDict/dataclass、
  字段 Annotation 与 ResourceTemplate symbol 均服从 02B3 的 shadow barrier。

## 5. 稳定错误与安全

对外统一：

```text
ActionContractError
code = invalid_action_contract | invalid_module_scope | invalid_annotation |
       invalid_action_result | invalid_schema
path = /module... | /parameters/{index}... | /return...
```

内部稳定错误保留原 code，并把路径定位到 Action Contract；不得泄漏裸
`AttributeError`、`IndexError`、`KeyError`、`TypeError` 或 AST 容器异常。

Module 全程只读 AST，禁止 `importlib`、`__import__`、`eval`、`exec`、`compile`、
`inspect`、`typing.get_type_hints` 和 dataclass runtime reflection。

## 6. 删除测试

测试必须证明删除这个 facade 后，caller 不得通过以下旧路径获得“看似成功”的合同：

1. `ast_registry_scanner._collect_imports()` 的 nested-scope `ast.walk()` map；
2. `_extract_method_params()` 的空类型 fallback；
3. `_get_annotation_str()` 的字符串类型猜测；
4. decorator `goal/result/handles` runtime example；
5. import 或执行作者模块后的 reflection。

## 7. 停止线

- 不修改 Backend；
- 不修改前端；
- 不发布或改变现有 Registry/YAML wire shape；
- 不解析 Template Catalog UUID 或 fingerprint；
- 不决定旧 `@action(handles=...)` 优先级；
- 不合成 D-068 implicit ResourceSlot output；
- 不实现 Workflow Compiler、HTTP、SQLite、SSE 或 typing projection。

02B 通过测试、完整质量门和唯一 reviewer 后合并；写完中文趋势/策略报告后直接进入
Round 02C，不再等待单独确认。
