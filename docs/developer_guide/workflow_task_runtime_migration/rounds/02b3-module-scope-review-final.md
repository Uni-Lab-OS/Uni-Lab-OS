# Round 02B3：静态模块作用域最终评审

日期：2026-07-31

评审分支：`review/02b3-module-scope-final`

固定点：`044a740c68ab1de52da29daf31022118a3e18916`

固定 production/test 候选：
`3cbe97afe582a70bb330738bf8de5b8c2c89db5a`

评审角色：Round 02B3 唯一独立 reviewer。本 reviewer 未编写本轮 production 或
测试，也没有启动其他 subagent；在一个 turn 内顺序完成 Standards 与 Spec 两轴。
本报告只新增中文评审文档，不修改 production、测试、前端或 Backend，不执行合并
或推送。

## 1. 结论

**Blocking 数为 2；Non-blocking 数为 1。固定候选暂不允许本地合并。**

| 评审轴 | Blocking | Non-blocking | 结论 |
|---|---:|---:|---|
| Repository Standards | 0 | 0 | 通过 |
| NB-01 / D-092 / 02B3 Spec | 2 | 1 | 不通过 |

目标测试、相关回归、完整仓库测试、Ruff 和 `git diff --check` 全绿，但两个独立探针
证明当前返回值仍会丢失名称遮蔽信息，并且会把 class body 的显式模块全局写入误认
为可信 import。因此测试全绿不足以证明 02B1 NB-01 已关闭。

## 2. 固定范围与证据来源

实际固定命令为：

```text
git diff 044a740c68ab1de52da29daf31022118a3e18916...3cbe97afe582a70bb330738bf8de5b8c2c89db5a
git log 044a740c68ab1de52da29daf31022118a3e18916..3cbe97afe582a70bb330738bf8de5b8c2c89db5a --oneline
```

提交顺序为：

```text
dae30d3 test: specify static module scope resolution
3cbe97a feat(registry): resolve static module scope
```

候选只新增四个文件：02B3 设计、两份 Registry 测试和
`unilabos/registry/module_scope.py`。没有修改旧 scanner、Catalog、Compiler、HTTP、
SQLite、SSE、FE 或 Backend，停止线本身遵守。

评审完整阅读了仓库 `AGENTS.md`、`CONTEXT.md`、D-092、02B3 设计、02B1 NB-01、
02B2 design/final review，以及本轮全部 production/tests diff。

## 3. Standards 轴

### 3.1 结论

**Standards blocking 0，non-blocking 0。**

模块维持一个主入口 `resolve_module_scope(...)`，把绑定遍历、结构守卫、错误投影和
只读快照藏在内部；命名可理解，类型标注完整，中文 docstring/错误符合仓库规则。
没有 FE/Backend 越界，也没有修改冻结接口。Ruff 已覆盖的格式问题不重复评审。

逐项检查 Fowler smell baseline：未发现需要形成 finding 的 Mysterious Name、
Duplicated Code、Feature Envy、Data Clumps、Primitive Obsession、Repeated
Switches、Shotgun Surgery、Divergent Change、Speculative Generality、Message
Chains、Middle Man 或 Refused Bequest。

558 行实现本身不构成 Speculative Generality：对外 surface 很小；02B3 设计明确要求
处理 Assign/AnnAssign/AugAssign/NamedExpr/del、解构和六类 compound ambiguity；
仓库只读 AST 盘点也确实存在顶层 `If`、`Try`、`For`、`While`、`With`。复杂度主要
来自失败关闭的 Python binding 枚举，而不是未来 hook、第二套 Catalog 或 caller
adapter。当前问题是绑定状态不完整，不是代码行数过多。

## 4. Spec 轴 findings

### S-B01：撤销 proof 后丢失 opaque/ambiguous 状态，builtin/helper 遮蔽仍可绕过

**级别：Blocking**

**证据：**

- `module_scope.py:31-33` 的公共快照只有 `import_identities` 与 `definitions`；
- `module_scope.py:505-511,550-551` 对 Assign、AnnAssign、NamedExpr 或 compound
  可能绑定只执行两个映射的 `pop`，没有保留“该名称已被本地值遮蔽或身份不确定”；
- `annotation_schema.py:139-145` 把 `name not in imports` 直接解释成 builtin；
- 02B1 NB-01 明确要求识别 Assign、AnnAssign、class/function 对 builtin/helper 的
  遮蔽，并增加“遮蔽后失败关闭”测试；02B3 设计 `:109-118` 又把
  `scope.import_identities` 作为可直接交给既有 parser 的结果。

只读组合探针得到：

```text
list = attacker                  -> scope imports={} definitions=[]
if flag: list = attacker         -> scope imports={} definitions=[]
class list: pass                 -> scope imports={} definitions=['list']

以上三种 scope.import_identities 交给 02B1 parser 后，
list[int] 均被接受为 {'type': 'array', 'items': {'type': 'integer'}}。
```

前两项尤其无法由未来 caller 从当前快照恢复：名称“从未绑定”和“有本地 opaque/条件
绑定”在两个公开映射里完全相同。caller 只能重新扫描 AST，等于深模块没有关闭
NB-01。第三项虽可由 caller 额外查 definitions 补救，但公共示例和 parser Interface
没有建立该约束。

**最小修复：**让 `ResolvedModuleScope` 保留完整的最终绑定格，而不只是正向 proof；
至少要有只读的 opaque/ambiguous shadow 状态，并提供一个不能把该状态误当 builtin
的 parser-consumption seam。后续无条件 import/definition 应清除该状态。新增端到端
模块名称测试，覆盖 Assign、AnnAssign、class/function、NamedExpr 和每类 compound
对 `list`、`dict`、`Field`、`ResourceSlot` 等 builtin/helper 的遮蔽；不能只断言旧
import entry 消失。

### S-B02：class body 的 `global` 会在模块加载时写全局名，resolver 却保留旧 identity

**级别：Blocking**

**证据：**

- `module_scope.py:280-304` 只检查 class decorator/base/keyword；
- `module_scope.py:542-549` 随后直接记录 class definition，不检查 class body 中的
  模块全局声明；
- `test_module_scope_v1.py:237-260` 只覆盖普通 class-local import，未覆盖
  `global`；
- 02B3 设计 `:71-73` 把 class body 的 `global` 与未执行的 function body 一并忽略，
  这不符合 Python：class body 在 class statement 求值时立即执行。

受控语义探针：

```python
from math import pi as Token

class Container:
    global Token
    Token = "evil"
```

resolver 返回 `{'Token': 'math:pi'}`，而 Python 完成该 class statement 后模块
`Token == 'evil'`。这不是保守 false negative，而是把已被覆盖的 identity 错认证为
Schema/Catalog Authority。类似的 class-body `global Token; from evil import Token`
或 `del Token` 也存在同一问题。

**最小修复：**纠正设计中的 class/function 等同假设。对 class code block 直接声明
为 `global` 的名称进行保守失效，或对含 `global` 的 class 失败关闭；遍历时跳过嵌套
function/class 的独立 lexical scope。补三类测试：普通 class-local import 不遮蔽；
class-body global assign/import/del 会失效；未调用 function body 的 global 仍不失效。

### S-NB01：forged definition 的 body 容器未验证，却被返回为已解析 definition

**级别：Non-blocking hardening**

`module_scope.py:280-347` 验证 definition header，但没有确认 ClassDef/FunctionDef 的
`body` 是 statement list；`module_scope.py:542-549` 仍将节点写入 definitions。
只读探针显示 `ClassDef(body=None)` 与 `FunctionDef(body=None)` 都成功返回，保留
`body=None`。这与设计 `:84-94` 的“真实 AST、容器守卫、forged AST 失败关闭”表述
不完全一致。

当前 production caller 将来自 `ast.parse`，而 02B2 class parser 还会再次守卫 body，
所以该项不单独升级为 blocking。最小修复是在记录 definition 前用现有
`_statement_list` 验证 body 容器但不递归采集普通嵌套绑定，并补 None/tuple/非 stmt
元素三项 forged tests。

## 5. 已通过的 Spec 与安全审计

除 findings 外，以下行为有代码和测试支持：

- 只直接顺序读取真实 `ast.Module.body`，普通 function/class-local import 不进入
  module import map；
- unconditional import/definition 之间采用 last-binding，compound 内可能绑定只做
  保守失效，不把条件分支当 proof；
- `import package.submodule`、带 alias import、absolute from import 与
  `from __future__ import annotations` 的绑定形态正确；
- wildcard 和 relative import 稳定失败关闭；已覆盖的 forged root/alias/target/
  compound container 不泄漏裸容器异常；
- 两个 mapping 为 `MappingProxyType`，不同调用不共享 mapping，输入 AST 不被修改，
  definition node identity 保留；
- production module 没有 import 作者模块或 symbol，也没有 eval/exec/compile、
  reflection、decorator/default/annotation 执行。

删除测试有效但范围有限：把可信顶层 import 与 function-local 恶意 import 交给旧
`_collect_imports(ast.walk(...))`，实际得到
`evil_nested_contract:Result`，因此 `test_nested_import_deletion_case...` 确实能杀死旧
scanner。它不能杀死“只收 module.body、但把 opaque shadow 删除成 absent”的实现，
这正是 S-B01 的测试缺口。

## 6. 实际测试与探针

全部命令使用仓库规定解释器：

```text
/home/changjunhan/.micromamba/envs/unilab/bin/python
```

实际结果：

```text
02B3 目标：
  54 passed in 0.56s

02B1/02B2 + 02B3 相关 Registry 回归：
  327 passed in 1.77s

完整仓库：
  1383 passed, 3 skipped, 20 warnings in 68.77s

Ruff E/F/I：
  All checks passed

Ruff format --check：
  3 files already formatted

git diff --check 044a740...3cbe97a：
  passed

opaque/builtin、class-global、forged definition body、旧 ast.walk 删除探针：
  completed；结果见 S-B01、S-B02、S-NB01 与 §5
```

完整测试 warnings 均为既有 escape、FastAPI/TestClient、pytest collection、SOCKS 和
lifespan deprecation 提示；没有测试失败。评审语义探针执行的仅是报告中可见的受控
字面量代码，用于确认 Python class `global` 语义；production resolver 自身没有执行
作者源码。

## 7. 合并门禁

固定候选 `3cbe97afe582a70bb330738bf8de5b8c2c89db5a` 的最终结论：

```text
Standards blocking:      0
Standards non-blocking:  0
Spec blocking:           2
Spec non-blocking:       1
```

**当前不允许本地合并。** S-B01 与 S-B02 必须修复并补测试；任何 production/test
修改都会产生新的候选 SHA，需要重新运行受影响测试与完整门禁，并由本 reviewer 对
新的精确 SHA 确认修复。S-NB01 建议同一修复轮收口，但它不单独阻塞。
