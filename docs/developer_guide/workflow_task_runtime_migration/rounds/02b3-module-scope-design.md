# Round 02B3：静态模块作用域解析深模块设计

日期：2026-07-31

分支：`migration/02b3-module-scope`

基线：`044a740`

## 1. 目标与决策来源

本轮关闭 02B1、02B2 最终评审共同保留的 NB-01：未来 Registry/Compiler
caller 不能把旧 `ast_registry_scanner._collect_imports()` 的 `ast.walk()` 结果传给
Parameter Annotation 或 Action Result parser。

旧实现有两个根本问题：

1. 它会把函数体、类体和条件分支里的 import 错当成确定的模块顶层 identity；
2. 它用 `setdefault()` 补本地定义，不能反映 Python 顶层名称的顺序和遮蔽。

D-092 要求 Workflow authoring 始终是纯 AST、静态、失败关闭的有限语言。因此本轮
提供一个 Registry 与未来 Compiler 可共同复用的模块作用域入口，不执行作者源码，
也不根据运行时 import 猜测 identity。

## 2. 公共接口

新增：

```python
resolve_module_scope(
    module: ast.Module,
    *,
    module_name: str,
) -> ResolvedModuleScope
```

`ResolvedModuleScope` 是 resolver 唯一可创建的不可变快照，公开：

- `module_name`：定义该 AST 的稳定 dotted module name；
- `import_identities`：只读的 `local_name -> static identity`；
- `definitions`：只读的 `local_name -> 顶层定义 AST`。
- `annotation_bindings`：可直接交给 02B1/02B2 parser 的只读绑定视图；它保留
  真正 import identity，并用不可匹配的内部标记阻断本地或不确定绑定回退为 builtin。

`definitions` 的 key 保持本地名，使 caller 可以直接以 return annotation 的
`ast.Name.id` 查找 `ClassDef`。同模块完整 identity 唯一派生为
`f"{scope.module_name}:{local_name}"`，但不会伪装成一个真正 import 放进
`import_identities`。

一次调用会复制三个结果映射；不同调用不共享可变容器。定义 AST 保留输入节点
identity，resolver 不复制、不修改源 AST。

`import_identities` 始终只包含真正 import，不能用伪 identity 污染审计结果；
`annotation_bindings` 才是 parser-consumption seam。这个区分保留了 import 事实与
失败关闭解析环境两个不同语义。

## 3. 绑定证明规则

resolver 顺序读取且只直接读取 `ast.Module.body`。任一本地名在最终结果中至多有
一种证明：import、definition，或没有静态证明。

| 顶层形态 | 处理 |
|---|---|
| `import package.submodule` | 实际绑定名是 `package`，identity 是 `package` |
| `import package.submodule as alias` | `alias -> package.submodule` |
| `from package import Symbol as local` | `local -> package:Symbol` |
| `from __future__ import annotations` | 不产生普通模块绑定 |
| 顶层 class/function/async function | 用本地名建立 definition 证明 |
| Assign/AnnAssign/AugAssign/NamedExpr | 撤销旧证明，并保留 builtin 遮蔽状态 |
| 无条件顶层 `del` | 真正删除的 Name 清除旧状态；Attribute/Subscript target 求值中的 NamedExpr 保留遮蔽 |
| 解构目标 | 递归撤销所有实际绑定名 |
| `if/while/for/with/try/match` | 其中任何可能绑定的名称都按不确定处理并撤销旧证明 |
| 后续无条件 import/definition | 可重新建立被撤销名称的证明 |

条件分支不会建立 import 或 definition 证明，因为 resolver 不执行条件，也不把
`if True`、循环次数或异常路径当作可求值常量折叠。这个限制会拒绝一部分理论上可
运行的 Python，但不会把不确定 identity 错认证为 Catalog/Schema Authority。

函数体在定义时不执行，其中的 import、赋值和 `global` 不表示“模块加载后已经
发生”的静态顶层证明。class body 则会在 class statement 求值时立即执行：普通
class-local 绑定仍不影响模块，但 class code block 直接声明为 `global` 且可能被
assign/import/del 的名称会保守撤销模块证明。nested class statement 的 body 也会在
外层 class body 执行期立即执行，因此递归盘点；嵌套 function/async function/lambda
body 未被调用时不执行，继续跳过。定义表达式阶段会真正求值的 decorator、base、
default、annotation 等位置中的 NamedExpr 也按可能绑定处理。

## 4. 失败关闭合同

稳定错误为：

```text
code = "invalid_module_scope"
message = "模块作用域不符合 Workflow 静态解析合同"
```

path 从 `/module` 开始，并细化到 name、body、statement 或 alias。resolver 对根
AST、容器、alias、target 和 compound body 做结构守卫，畸形 AST 不泄漏裸
`TypeError`、`AttributeError` 或容器异常。

以下输入直接失败关闭：

- 非 `ast.Module` 根或非 list 的 module body；
- 空或带空 segment 的 `module_name`；
- 相对 import；
- wildcard import；
- 畸形 import alias、definition name、assignment target 或 compound body。

相对和 wildcard import 不进行“尽力解析”：前者依赖 package context，后者可能
覆盖任意名称，两者都不能产生有限、可审计的 identity map。规范化 Workflow
Python 本来也只生成绝对、显式 import。

## 5. 纯 AST 与安全边界

本模块禁止：

- import 作者模块或 imported symbol；
- `eval`、`exec`、`compile`；
- `typing.get_type_hints`、dataclass/inspect reflection；
- 执行 decorator、default、annotation 或其他作者表达式。

模块只读 AST 节点类型和字段。结果可直接提供给 02B1/02B2 parser：

```python
scope = resolve_module_scope(tree, module_name=module_name)
declaration = scope.definitions[return_annotation.id]
results = parse_action_result_declaration(
    declaration,
    imports=scope.annotation_bindings,
)
```

上述 caller 接线仍属于后续独立 round；示例只说明 seam 的删除测试和复用方向。

## 6. 独立测试与删除测试

本轮唯一独立测试作者先提交 54 个 RED case，统一首因是 production seam 缺失。
测试覆盖：

- absolute/from/future import 的真实绑定名；
- 顶层定义和 last-binding 遮蔽；
- Assign、AnnAssign、AugAssign、del、NamedExpr、解构；
- if/while/for/with/try/match 的保守不确定绑定；
- nested function/class scope 隔离；
- wildcard/relative import 与 forged AST；
- 结果映射不可变、AST 不变、跨调用隔离；
- 禁止 import、执行、编译和反射。

关键删除测试把一个可信顶层 `Result` import 与函数内恶意同名 import 放在同一
模块。若删掉本模块并恢复旧 `ast.walk()` 方案，恶意 nested identity 会覆盖可信
identity，该测试立即失败。

最终 reviewer 发现初版只删除 proof、没有保留 opaque/ambiguous shadow 状态，并
发现 class body `global` 的加载期写入语义。修复阶段再增加 15 个回归 case，目标集
增至 69 个；同时验证 `ClassDef`/`FunctionDef` 的 forged body 容器会稳定失败关闭，
并让 Parameter 与 Action Result parser 共用同一 builtin shadow barrier。

第一次修复确认又发现 Delete target 求值中的 NamedExpr 与 nested class body 两个
相邻真实语义；第二次修复再增加 7 个 case，最终目标集增至 76 个，并对同一 Delete
中“删除 Name”和“求值重新绑定 Name”采用保守的 shadow-wins 规则。

## 7. 停止线

本轮不做：

- 不修改或接线旧 `ast_registry_scanner.py`、`ImportManager`；
- 不接 Action Registry 发布、Template Catalog 或 fingerprint；
- 不把 return `ast.Name` 解析接进 production caller；
- 不合成隐式 ResourceSlot output；
- 不实现 Compiler、transform、generate-python、HTTP、SQLite 或 SSE；
- 不修改前端或 Backend。

这样 02B3 只关闭名称证明的公共安全前置条件。旧 scanner 的生产迁移必须在后续
caller round 通过新的独立测试和评审完成，不能因本模块存在就宣称 Registry/
Compiler 已经接线。
