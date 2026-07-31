# Round 02B2：Action result record 合并合同复审

日期：2026-07-31

评审分支：`review/02b2-action-result-contract-merge`

基线：`5b7534d69522f302eaefc4a26681f0eda6eb708f`

固定 production/test 候选：
`5327323c9de8487e1ca47597074999fda8aa790e`

含最终趋势文档的评审快照：
`ef7189bf1f3aa099c1ea0fac849daf5f5d4b2b79`

独立 forged AST 测试提交：
`cc53cd2d15be11d2569fe754fcde5e63a078d839`

评审角色：重新开始的顺序独立复审 1/3，合同 / Spec reviewer。本报告不修改
production、测试、前端或 Backend，也没有启动其他 subagent。

## 1. 结论

**Blocking 数为 1；Non-blocking 数为 1。固定候选当前不允许进入顺序复审
2/3，也不可合并。**

| 评审轴 | Blocking | Non-blocking | 结论 |
|---|---:|---:|---|
| 原 AR-C01 修复 | 0 | 0 | 通过 |
| D-100 / 02B2 完整合同 | 1 | 1 | 不通过 |
| Repository Standards / scope | 0 | 0 | 通过 |

原 5 个 forged AST seam 已正确关闭；新 blocking 是相邻 malformed dataclass
decorator keyword shape 仍会泄漏裸 `TypeError`。唯一 non-blocking 仍是 NB-01：
未来 caller 的 module-scope、shadow-aware import/definition map。

`5327323..ef7189b` 在 `unilabos/` 与 `tests/` 下没有差异，后续提交只更新趋势
文档，因此受审 production/test 固定点没有漂移。

## 2. 原 forged AST RED 与修复

**AR-C01 disposition：`accepted-fixed`**

Reviewer 在仅含首版实现与独立测试的 `cc53cd2` detached checkout 上实际复跑：

```text
5 failed, 0 passed
3 个首因：裸 TypeError
2 个首因：裸 AttributeError
```

五个 cases 准确冻结：

- `ast.Dict.keys=None`；
- `ast.Dict.values=None`；
- `ClassDef.body=None`；
- `AnnAssign` 缺 `target`；
- `AnnAssign` 缺 `annotation`。

每项都要求两次调用得到稳定的 `invalid_action_result`、`/return...` path 和冻结
中文 message。RED 首因与原报告一致，没有用较宽断言掩盖错误。

`5327323` 在读取前显式验证：

- `ClassDef.bases/keywords/body/decorator_list` 必须是 list；
- compat dict 的 `keys/values` 必须是 list；
- `AnnAssign.target` 必须是带 string id 的 `ast.Name`；
- `annotation` 必须是 `ast.expr`，`simple/value` 必须完整合法；
- dataclass `Call.args/keywords` 及 keyword/value 必须具备预期 AST shape。

原五项在最终候选全部稳定 GREEN，目标累计为 104 passed。

共享 annotation 委托只在 `_parse_field()` 调用周围重定位
`AttributeError/IndexError/TypeError` 到字段 `/annotation` path。独立异常注入确认：

```text
AttributeError / IndexError / TypeError -> /return/fields/0/annotation
MemoryError / SystemExit / KeyboardInterrupt / RuntimeError -> 原样传播
```

没有顶层 `except Exception`，资源耗尽、进程控制和无关运行时错误未被吞掉。

相邻只读复核也确认 ClassDef 四个 list container 缺失、缺形状 decorator call 和
缺属性 annotation subscript 均稳定投影。原 AR-C01 已关闭。

## 3. Blocking finding

### AR-C02：malformed decorator keyword name 可泄漏裸 TypeError

**Disposition：`blocking-open`**

`action_result_schema.py:232-245` 已确认每个元素是 `ast.keyword`，但随后直接执行：

```python
name = getattr(keyword, "arg", None)
if name not in {"frozen", "kw_only", "slots"} or name in seen:
    ...
```

它没有先确认 `name` 是 `str | None`。手工 forged AST 可以让 `arg` 成为不可哈希
对象，使允许集检查本身抛裸异常。

固定候选最小复现：

```python
declaration = ast.ClassDef(
    name="Result",
    bases=[],
    keywords=[],
    body=[
        ast.AnnAssign(
            target=ast.Name(id="value"),
            annotation=ast.Name(id="str"),
            value=None,
            simple=1,
        )
    ],
    decorator_list=[
        ast.Call(
            func=ast.Name(id="dataclass"),
            args=[],
            keywords=[
                ast.keyword(
                    arg=[],
                    value=ast.Constant(value=True),
                )
            ],
        )
    ],
)
parse_action_result_declaration(
    declaration,
    imports=MappingProxyType({"dataclass": "dataclasses:dataclass"}),
)
```

实际结果：

```text
TypeError: unhashable type: 'list'
```

按 02B2 design §6，该输入必须稳定返回：

```text
code = invalid_action_result
path = /return/decorators/0
message = Action 结果声明不符合 Workflow 版本 1 合同
```

该问题属于当前 `parse_action_result_declaration(ast.ClassDef, ...)` 的 decorator
shape 合同，不是未来 caller 接线范围。新增 5 个测试没有覆盖相邻的 keyword 属性
类型，所以 104 全绿不能关闭 AR-C02。

关闭条件是由独立测试作者先冻结 malformed `ast.keyword.arg` 的稳定错误，再显式
验证 keyword name shape；不能通过扩大为顶层宽泛异常捕获修复。本 Reviewer 不改
production 或测试。

## 4. D-100 与 canonical 合同复核

除 AR-C02 外，D-100 行为保持正确：

- `ParsedResult` 仍直接复用 02B1 `_parse_annotation()` 与唯一
  `parse_output_contract()`；没有复制 D-082～D-091 类型系统；
- 标准 `TypedDict`、标准库 frozen dataclass、兼容 dict 和 `-> None` 的接受及闭合
  拒绝矩阵未改变；
- 三种有字段形式继续得到同一个有序 contract 与同序 ResourceTemplate symbols；
- canonical 值不保存声明形式、class name、`slots` 或 `kw_only`；
- 显式 output 仍物化 `implicit: false`，没有 `default`/`required`；nullable 只表示
  必然存在字段的值可为 null；
- `-> None` 仍得到空显式 outputs；
- parser-only、dump isolation、纯 AST/no import/eval/exec/reflection 和宽字段增长
  守护继续通过。

02B1 167 个测试全绿，说明 integer budget、Literal collision safety、异常隔离和
共享 annotation 语义没有回归。

## 5. 停止线与 NB-01

候选没有接旧 Registry scanner、Catalog、Handle UUID/fingerprint、旧
`@action(handles=...)`、D-068 implicit outputs、`.pyi`、Compiler、HTTP、SQLite、
SSE、前端或 Backend，也没有解析 return Name 到 class。

NB-01 仍为 `non-blocking-follow-up`：下一 caller round 必须实现真实 module AST、
module-scope、shadow-aware 的 import/definition resolver；当前 02B2 不应提前接线。

## 6. 实际门禁

全部使用：

```text
/home/changjunhan/.micromamba/envs/unilab/bin/python
```

结果：

```text
cc53cd2 forged AST RED：
  5 failed, 0 passed

02B2 Action result 目标：
  104 passed in 1.00s

02B1 Parameter Annotation：
  167 passed in 1.24s

Registry：
  297 passed in 3.69s

相邻 AST shape / 委托异常透传脚本：
  AR-C01 paths passed；AR-C02 reproduced

Ruff E/F/I：
  All checks passed

Ruff format --check：
  6 files already formatted

git diff --check 5b7534d...5327323：
  passed

git diff --check 5327323...ef7189b：
  passed
```

主执行者已在同一 production/test SHA 运行正式全量：

```text
1327 passed, 3 skipped
```

该结果仅作为同 SHA 门禁证据引用，本 Reviewer 没有冒充重复执行。自动门禁全绿
不能关闭 AR-C02，因为当前测试没有覆盖该 decorator attribute shape。

## 7. 顺序复审门禁

固定 production/test 候选
`5327323c9de8487e1ca47597074999fda8aa790e` 当前为：

```text
blocking:     1（AR-C02，malformed decorator keyword 泄漏 TypeError）
non-blocking: 1（NB-01，未来 production caller 接线前关闭）
```

**不允许进入顺序复审 2/3，也不允许合并。**

关闭 AR-C02 后必须固定新的 production/test SHA，重跑目标及正式门禁，并由三名
reviewer 对新 SHA 重新开始顺序复审。任何 production/test 变化都会使本报告失效。
