# Round 02B3：静态模块作用域修复确认评审

日期：2026-07-31

评审分支：`review/02b3-module-scope-confirm`

固定点：`044a740c68ab1de52da29daf31022118a3e18916`

初评候选：`3cbe97afe582a70bb330738bf8de5b8c2c89db5a`

修复候选：`80cad0c183477e3a0dd066f8635e3510aea0c284`

评审角色：Round 02B3 同一且唯一独立 reviewer。没有启动其他 subagent。本报告只
新增中文确认文档，不修改 production、测试、前端或 Backend，不执行合并或推送。

## 1. 结论

**Blocking 数仍为 2；Non-blocking 数为 0。修复候选暂不允许本地合并。**

| 评审轴 | Blocking | Non-blocking | 结论 |
|---|---:|---:|---|
| Repository Standards | 0 | 0 | 通过 |
| NB-01 / D-092 / 02B3 Spec | 2 | 0 | 不通过 |

原报告 S-NB01 已关闭。S-B01 的普通 opaque/compound/builtin 路径和 S-B02 的直接
class body 路径已修，但两个真实、可由 `ast.parse` 产生的相邻 Python binding 形态
仍会输出错误的可信环境：Delete target 求值阶段的 NamedExpr，以及执行期 nested
class body 的 `global`。因此 S-B01、S-B02 均只能记为 `partial`，还不能关闭。

## 2. 固定 diff 与范围

本次同时检查：

```text
git diff 3cbe97afe582a70bb330738bf8de5b8c2c89db5a...80cad0c183477e3a0dd066f8635e3510aea0c284
git diff 044a740c68ab1de52da29daf31022118a3e18916...80cad0c183477e3a0dd066f8635e3510aea0c284
```

修复 diff 只涉及 02B3 设计/初评报告、两份测试和
`unilabos/registry/module_scope.py`。完整候选仍未接旧 scanner、Catalog、Compiler、
HTTP、SQLite、SSE、FE 或 Backend，停止线未漂移。

## 3. Standards 轴

**Standards blocking 0，non-blocking 0。**

`annotation_bindings` 是 S-B01 所需的一个 parser-consumption seam，并没有把内部
Catalog、caller 或未来 adapter 提前公开。真正 import 事实继续只存在于
`import_identities`；额外 mapping 使用独立只读副本。class-global 分析仍留在同一
深模块。没有形成 Mysterious Name、Duplicated Code、Feature Envy、Data Clumps、
Primitive Obsession、Repeated Switches、Shotgun Surgery、Divergent Change、
Speculative Generality、Message Chains、Middle Man 或 Refused Bequest finding。

Ruff 已覆盖的格式问题不重复计入 Standards。

## 4. 原 findings 逐项确认

### 4.1 S-B01：`partial`，普通遮蔽已修，但 Delete target 求值重新打开 builtin 回退

已确认修复的部分：

- `module_scope.py:32-35,64-72` 保持 `import_identities` 纯净，并单独建立只读
  `annotation_bindings`；
- Assign、AnnAssign、NamedExpr、class/function definition 与 compound ambiguity
  会写入不可匹配 sentinel，后续无条件 import/definition 能重建相应状态；
- 直接 `list = attacker; del list` 会清除模块全局名并恢复 builtin lookup；
- Parameter 与 Action Result 的端到端测试均使用 `annotation_bindings`，不再只断言
  import entry 消失；
- sentinel 是 `"<shadowed>"`，不含 `:`。`AllowedResourceTemplates(plate)` 在 plate
  被遮蔽后会由既有 identity 形状检查稳定拒绝，不能产生
  `ResourceTemplateSymbol("plate", "<shadowed>")`；Field、ResourceSlot、TypedDict
  等 helper 也只做 exact identity 匹配，sentinel 不会伪装为真 import。

仍存在的 blocking：`module_scope.py:439-448` 把 Delete target 中“实际被删除的 Name”
和“为求值 Attribute/Subscript target 而执行的 NamedExpr 绑定”合成一个 set；
`module_scope.py:646-654` 又因外层 statement 是 Delete 而把 set 内所有名称执行
`_clear_binding()`。

以下均是 Python 3.11 `ast.parse` 接受的真实源码：

```python
del obj[(list := key)]
del (list := obj).attr
```

两者都给模块 `list` 赋值，删除的是 subscript/attribute，并没有删除 `list`。独立
端到端 probe 却得到：

```text
scope.annotation_bindings == {'workflow': '<shadowed>'}
list 不在 binding view
02B1 Parameter parser 接受 list[int] 为 array[integer]
```

这重新产生了 S-B01 的同一安全结果：本地 opaque 名被误当 builtin。

**最小修复：**把 Delete 的实际删除 effect 与 target 求值期间的 NamedExpr binding
分开。直接 Name/解构 Name 可清除；Attribute/Subscript 的 value/slice 中 NamedExpr
必须保留 shadow。可以保守规定同一 Delete 同名同时出现时 shadow 优先，避免为了
精确顺序扩大 surface。补 subscript、attribute 和 mixed-target 端到端 parser tests。

### 4.2 S-B02：`partial`，直接 class global 已修，但 nested class body 仍被错误跳过

已确认修复的部分：

- 直接 class code block 的 global assign/import/del 会撤销旧 import proof；
- 普通 class-local import 不影响模块；
- 未调用 function/async function body 的 global 仍被正确忽略。

仍存在的 blocking：`module_scope.py:377-384` 同时跳过 nested function 与 nested
ClassDef；`_class_global_bindings()` 因而看不到 nested class body 的 global 写入。
设计 `02b3-module-scope-design.md:78-83` 也错误地把 nested class 与未调用 function
并列为“不参与盘点”。

Python 的差异是：function body 在定义时不执行；nested class body 会在 outer class
body 执行其 class statement 时立即执行。受控 probe：

```python
from math import pi as Token

class Outer:
    class Inner:
        global Token
        Token = "evil"
```

Python 完成模块加载后 `Token == "evil"`，但 resolver 仍返回：

```text
import_identities  == {'Token': 'math:pi'}
annotation_bindings == {'Token': 'math:pi', 'Outer': '<shadowed>'}
```

这仍把已被 class body 覆盖的 identity 错认证为可信 Schema/Catalog Authority。

**最小修复：**class-global 分析需递归盘点执行期可达的 nested class body，同时继续
跳过 function/async function/lambda body。对每个 class code block 独立应用其
`global` 声明，保守合并可能的模块写入。补一层/多层 nested class、compound 内
nested class，以及 function body 内未调用 nested class 的区分测试。

### 4.3 S-NB01：`accepted-fixed`

`module_scope.py:292-294` 在记录任何 ClassDef/FunctionDef/AsyncFunctionDef 前用
`_statement_list` 验证 body；新增测试覆盖 `None`、tuple 和非 statement element。
独立 probe 也确认 forged ClassDef/FunctionDef 得到稳定
`invalid_module_scope`，不再进入 definitions。原 non-blocking 已关闭。

## 5. 其他安全与回归确认

- `import_identities`、`definitions`、`annotation_bindings` 三个 mapping 均不可修改；
  两次解析不共享 mapping，definition node 保持原 AST identity；
- ordinary/from/future import、last-binding、compound ambiguity、wildcard/relative
  失败关闭及旧 `ast.walk` 删除测试保持通过；
- production resolver 仍不 import 作者模块/symbol，不调用 eval/exec/compile 或
  reflection，不执行 decorator/default/annotation；
- 新 sentinel 不进入 true import map，也不能通过 ResourceTemplate identity 的单冒号
  检查；
- Parameter 与 Action Result 主路径的 builtin shadow barrier 已有端到端覆盖；
- 修复没有增加 Catalog、Compiler、Registry 发布、HTTP、持久化、FE 或 Backend
  surface。

## 6. 实际门禁与 probes

使用仓库规定解释器：

```text
/home/changjunhan/.micromamba/envs/unilab/bin/python
```

本 reviewer 独立运行：

```text
02B3 目标：
  69 passed in 0.76s

完整 Registry：
  368 passed in 3.39s

Ruff E/F/I：
  All checks passed

Ruff format --check：
  3 files already formatted

git diff --check 044a740...80cad0c：
  passed

binding 顺序、Delete NamedExpr、AllowedResourceTemplates sentinel、
nested class global、forged body probes：
  completed；结果见 §4、§5
```

主执行者在同一固定候选登记的完整门禁为：

```text
目标 69 passed
相关 269 passed
完整仓库 1398 passed, 3 skipped, 19 warnings
Ruff / format / diff-check passed
```

本 reviewer 没有冒充重复执行完整仓库结果。语义 probe 中只执行了报告可见的受控
字面量 class 代码，用来确认 Python nested-class global 行为；production resolver
自身没有执行作者源码。

## 7. 最终合并门禁

修复候选 `80cad0c183477e3a0dd066f8635e3510aea0c284` 的确认结论：

```text
Standards blocking:      0
Standards non-blocking:  0
Spec blocking:           2
Spec non-blocking:       0
```

**当前不允许本地合并。** Delete target effect 与 nested-class global 必须补测试并
修复；任何 production/test 修改都会产生新的候选 SHA，需要重跑受影响测试与完整
门禁，并由本 reviewer 对新的精确 SHA 再确认。
