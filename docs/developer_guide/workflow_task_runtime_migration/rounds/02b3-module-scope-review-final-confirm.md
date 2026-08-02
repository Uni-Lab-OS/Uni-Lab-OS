# Round 02B3：静态模块作用域最终确认

日期：2026-07-31

评审分支：`review/02b3-module-scope-confirm2`

固定点：`044a740c68ab1de52da29daf31022118a3e18916`

前次修复候选：`80cad0c183477e3a0dd066f8635e3510aea0c284`

最终候选：`a80d471581949ac7311fd13356cc8dda09b57bf4`

评审角色：Round 02B3 同一且唯一独立 reviewer。没有启动其他 subagent。本报告只
新增中文确认文档，不修改 production、测试、前端或 Backend，不执行合并或推送。

## 1. 结论

**Blocking 数为 0；Non-blocking 数为 0。最终候选允许本地合并。**

| 评审轴 | Blocking | Non-blocking | 结论 |
|---|---:|---:|---|
| Repository Standards | 0 | 0 | 通过 |
| NB-01 / D-092 / 02B3 Spec | 0 | 0 | 通过 |

第二次修复把 Delete 的实际删除 effect 与 target 求值 binding 分离，并让 class-global
分析递归覆盖执行期 nested class body。前两次报告的全部历史 findings 均已关闭，
没有发现新的 blocking、non-blocking 或停止线漂移。

## 2. 固定 diff 与范围

定向检查：

```text
git diff 80cad0c183477e3a0dd066f8635e3510aea0c284...a80d471581949ac7311fd13356cc8dda09b57bf4
```

完整检查：

```text
git diff 044a740c68ab1de52da29daf31022118a3e18916...a80d471581949ac7311fd13356cc8dda09b57bf4
```

本次 production 变化仍只在 `unilabos/registry/module_scope.py`，另有目标测试和
02B3 设计更新。完整候选没有修改旧 scanner、Catalog、Compiler、Registry 发布、
HTTP、SQLite、SSE、FE 或 Backend。

## 3. Standards 轴

**Standards blocking 0，non-blocking 0。**

新增 `_delete_target_effects()` / `_delete_effects()` 把两种不同的 Python effect 明确
命名并封装；class-global 递归继续留在同一内部实现。没有新增 public type、参数、
mapping、hook 或 caller adapter。对外仍只有既定 resolver、错误和只读 scope
snapshot。

逐项检查 Fowler baseline，未形成 Mysterious Name、Duplicated Code、Feature Envy、
Data Clumps、Primitive Obsession、Repeated Switches、Shotgun Surgery、Divergent
Change、Speculative Generality、Message Chains、Middle Man 或 Refused Bequest
finding。Ruff 已覆盖的格式问题不重复计入。

746 行实现仍是必要的内部复杂度，而不是 speculative public surface：本轮明确承担
Python 3.11 顶层 import/definition/opaque/compound/delete proof、class 执行期 global、
forged shape 与稳定错误；第二次修复只增加这两个已复现语义的内部 effect 分离和递归
收集。删除任一部分都会重新打开已测试的错误 identity proof。

## 4. 最后两个 blocking 定向确认

### 4.1 Delete actual-name 与 target-evaluation NamedExpr：`accepted-fixed`

`_delete_target_effects()` 现在分别返回：

- 真正被 `del` 删除的 Name；
- 求值 Attribute/Subscript target 时执行的 NamedExpr binding。

顶层 resolver 先清除 deleted names，再对 evaluated names 建立 shadow；因此同一
Delete 中同名同时出现时稳定采用保守 `shadow-wins`。独立 parser probe 结果：

| 源码形态 | 最终静态处理 | Parameter `list[int]` |
|---|---|---|
| `list = attacker; del list` | direct Name clear | 接受 builtin list |
| `del box[(list := key)]` | evaluated shadow | 拒绝 |
| `del (list := box).member` | evaluated shadow | 拒绝 |
| `del list, box[(list := key)]` | same-name shadow-wins | 拒绝 |
| `del box[(list := key)], list` | 保守 shadow-wins | 拒绝 |
| `if flag: del list` | compound ambiguity | 拒绝 |
| `if flag: del box[(list := key)]` | compound ambiguity | 拒绝 |

反向 mixed target 在实际成功路径最后可能恢复 builtin，但保守拒绝不会把不确定 name
误认证为 Authority，符合 02B3 fail-closed 规则。Tuple/List/Starred target 递归仍有
container/type guard，Attribute/Subscript 只采集求值表达式中的 NamedExpr。

因此前次 Delete blocking 已关闭，原 S-B01 的 opaque/builtin shadow 闭包也保持关闭。

### 4.2 nested class body global：`accepted-fixed`

`_ClassGlobalDeclarations.visit_ClassDef()` 现在递归调用每个 nested class code block
的 class-global 分析；function、async function 和 lambda body 仍直接停止，不会把
未调用代码当成模块加载事实。

独立 probe 和目标测试确认：

- 一层 nested class 的 global assign 使旧 import proof 失效；
- 多层 nested class 同样递归失效；
- `if` 等 compound 内可能执行的 nested class 保守失效；
- 普通 class-local `Token = ...` 不影响模块 `Token` import；
- function 与 async function body 内的 nested class 未调用时不影响模块；
- lambda body 中的 NamedExpr 不执行、不影响模块；lambda default 中的 NamedExpr
  在定义时执行，仍被原 expression analyzer 正确记为 shadow；
- nested class 自己的 ordinary local binding 不会被误当 global，只有其 code block
  的 `global` 与可能写入交集进入结果。

递归只沿真实 AST tree 的 nested ClassDef child 前进，不从 child 回到 parent；实际
95 层 `ast.parse` nested-class probe 正常结束并得到 shadow proof。每个 class body 的
statement list 都经过现有结构守卫；forged nested `ClassDef(body=None)` 得到稳定
`invalid_module_scope`，未泄漏裸 container error。没有 import、eval、exec、compile
或 reflection 路径。

因此前次 nested-class blocking 已关闭，原 S-B02 class-global finding 完整关闭。

## 5. 全部历史 findings disposition

| 历史 finding | 最终 disposition | 证据摘要 |
|---|---|---|
| S-B01 opaque/ambiguous shadow 丢失 | `accepted-fixed` | `annotation_bindings` + sentinel + Parameter/Action Result E2E |
| S-B02 direct class-body global | `accepted-fixed` | direct assign/import/del 失效 proof |
| S-NB01 forged definition body | `accepted-fixed` | definition body list guard + forged tests |
| 第一次确认：Delete NamedExpr | `accepted-fixed` | delete effects 分离、mixed shadow-wins、compound tests |
| 第一次确认：nested class global | `accepted-fixed` | 一层/多层/compound 递归，function/async/lambda 边界 |

`annotation_bindings` 的 sentinel 仍不进入 `import_identities`，也不含
ResourceTemplate identity 必需的单冒号，不能被 `AllowedResourceTemplates` 当成合法
symbol。三个公开 mapping 仍不可变且跨调用隔离；输入 AST 不被修改，definition node
identity 保持。

## 6. 实际门禁与安全 probes

使用仓库规定解释器：

```text
/home/changjunhan/.micromamba/envs/unilab/bin/python
```

本 reviewer 独立运行：

```text
02B3 目标：
  76 passed in 0.78s

完整 Registry：
  375 passed in 3.41s

Ruff E/F/I：
  All checks passed

Ruff format --check：
  3 files already formatted

git diff --check 044a740...a80d471：
  passed

Delete 七种顺序/compound、class local/nested/function/async/lambda、
95 层递归与 forged nested body probes：
  passed
```

主执行者在相同最终候选登记的完整门禁：

```text
目标 76 passed
Registry 375 passed
完整仓库 1405 passed, 3 skipped, 18 warnings
Ruff / format / diff-check passed
```

本 reviewer 没有冒充重复执行完整仓库结果。production resolver 仍是纯 AST；评审
probe 不导入作者模块或 symbol，也没有修改生产/测试文件。

## 7. 最终合并门禁

最终候选 `a80d471581949ac7311fd13356cc8dda09b57bf4`：

```text
Standards blocking:      0
Standards non-blocking:  0
Spec blocking:           0
Spec non-blocking:       0
```

**允许本地合并到 `integration/workflow-task-runtime`。** 本结论只覆盖精确候选；若
合并前再修改任何 production 或测试，本报告失效并需重新固定 SHA 复审。合并后仍须
按 round gate 在 integration 上运行正式完整门禁。
