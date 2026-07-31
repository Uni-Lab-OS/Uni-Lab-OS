# Round 02B2：Action result record 最终合同复审

日期：2026-07-31

评审分支：`review/02b2-action-result-contract-final`

基线：`5b7534d69522f302eaefc4a26681f0eda6eb708f`

固定 production/test 候选：
`9806d9a61699b2f17dcf4409353702f070df201a`

含最终趋势文档的评审快照：
`4f21857615950b657637a1a9cad86869e0de6bcf`

独立 forged decorator 测试提交：
`c1d05d9c1b322ec6fa2a8940e2a86bbbfad77f3b`

评审角色：重新开始的顺序独立复审 1/3，合同 / Spec reviewer。本报告不修改
production、测试、前端或 Backend，也没有启动其他 subagent。

## 1. 结论

**Blocking 数为 0；Non-blocking 数为 1。固定候选允许进入顺序复审 2/3。**

| 评审轴 | Blocking | Non-blocking | 结论 |
|---|---:|---:|---|
| forged AST shape / 稳定错误 | 0 | 0 | 通过 |
| D-100 / 02B2 Spec | 0 | 1 | 通过 |
| Repository Standards / scope | 0 | 0 | 通过 |

AR-C01 与 AR-C02 均为 `accepted-fixed`。唯一 non-blocking 仍是 NB-01：未来
production caller 必须提供 module-scope、shadow-aware 的 import/definition map；
本轮按停止线没有接 caller，该风险合理后移。

`9806d9a..4f21857` 在 `unilabos/` 与 `tests/` 下没有差异，后续提交只更新趋势
文档，因此受审 production/test 固定点没有漂移。

## 2. AR-C02 的 RED → GREEN

**Disposition：`accepted-fixed`**

Reviewer 在包含 `5327323` production 与独立测试、但尚无修复的 `c1d05d9`
detached checkout 上实际复跑：

```text
2 failed, 0 passed
```

两个 cases 分别把 list 与 dict 放入 forged `ast.keyword.arg`，均在 dataclass
option set membership 中得到：

```text
TypeError: unhashable type
```

测试要求两次调用稳定返回 `invalid_action_result`、精确 path
`/return/decorators/0` 和冻结中文 message。RED 首因与 AR-C02 完全一致，没有修改
原 104 个测试。

`9806d9a` 只在 membership 前增加：

```python
type(name) is not str
```

Python `or` 短路保证非 exact string 永远不会进入允许集或 `seen` lookup；合法
`frozen`、`slots`、`kw_only` 的行为和顺序不变。最终 106 个目标测试全部通过。

## 3. 系统 AST 属性审计

按本轮要求，不只复核已知 keyword 例子，而是逐项检查
`action_result_schema.py` 中所有来自 AST 的属性在 membership、lookup、iteration
或 indexing 前的守卫。

| AST 来源 | 使用前守卫 | 结论 |
|---|---|---|
| `ClassDef.bases/keywords/body/decorator_list` | `_parse_class` 要求 exact `list` | 通过 |
| TypedDict base `Name.id` | `_is_import` 要求 `ast.Name` 且 exact `str` | 通过 |
| `ClassDef.body` statement | 先区分 docstring/Pass/`ast.AnnAssign` | 通过 |
| `AnnAssign.target.id` | `target` 为 `ast.Name` 且 id exact `str` | 通过 |
| `AnnAssign.annotation/simple/value` | annotation 为 `ast.expr`；simple 等于 1；value 属性存在且为 None | 通过 |
| compat `Dict.keys/values` | 二者均要求 exact `list` 后才 `len`/index | 通过 |
| compat dict key | `ast.Constant.value` 要求 exact `str` 后才进入 `names` set | 通过 |
| compat dict value | bounds check 后取值，并要求 `ast.expr` 才委托 | 通过 |
| dataclass `Call.func` | `getattr` 后交给 guarded `_is_import` | 通过 |
| `Call.args/keywords` | 二者均要求 exact `list` 后才 truthiness/iteration | 通过 |
| decorator keyword element | 先要求 `ast.keyword` | 通过 |
| `keyword.arg` | exact `str` 后才允许集/seen lookup | 通过 |
| `keyword.value` | 先要求 `ast.Constant`，再读取 value 并要求 exact `True` identity | 通过 |
| root `Constant.value` | `getattr` 后只接受 identity `None` | 通过 |

除正式 7 个 forged AST/decorator cases 外，Reviewer 又对 25 个相邻 shape 连续调用
两次，覆盖：

- 四个 class container 的 `None` 与 tuple；
- base/target `Name.id` 为非 string；
- `AnnAssign` 缺 target/annotation；
- dict container、缺/非 string key、malformed annotation；
- `Call` 缺 func/args/keywords 或 keyword element 非 AST；
- keyword arg 为 list/dict、keyword value 缺失。

全部得到稳定 `invalid_action_result` 与对应 `/return...` path，没有裸
`TypeError`、`AttributeError`、`IndexError` 或 AST 异常。

原 AR-C01 的 5 个 tests 与 AR-C02 的 2 个 tests 均保持 GREEN，因此两个 finding
已关闭。

## 4. 委托 annotation seam 与异常边界

`_parse_field()` 的 try block 仅包围 `parse_result_annotation(...)` 调用：

- `AnnotationSchemaError` 按原错误位置重定位；
- malformed annotation AST 可能产生的 `AttributeError`、`IndexError`、
  `TypeError` 统一定位到该字段 `/annotation`；
- 没有 `except Exception` 或顶层宽泛捕获。

实际异常注入结果：

```text
AttributeError -> /return/fields/0/annotation
IndexError     -> /return/fields/0/annotation
TypeError      -> /return/fields/0/annotation

MemoryError       -> 原样传播
SystemExit        -> 原样传播
KeyboardInterrupt -> 原样传播
RuntimeError      -> 原样传播
```

因此 design §6 的输入结构错误稳定化与资源/进程/无关实现异常透传同时成立。

## 5. D-100 合同复核

最终候选没有改变业务合同：

- `ParsedResult` 继续直接复用 02B1 `_parse_annotation()` 与唯一
  `parse_output_contract()`，没有复制 D-082～D-091 类型系统；
- 标准 `TypedDict`、标准库 `@dataclass(frozen=True)`、兼容 dict 与 `-> None`
  的接受及闭合拒绝矩阵全部通过；
- 三种有字段形式继续得到同一个有序 `WorkflowOutputContract` 与同序
  ResourceTemplate symbols；
- canonical 值不保存声明形式、class name、`slots` 或 `kw_only`；
- 每个显式 output 物化 `implicit: false`，没有 `default` 或 `required`；
- nullable 表示字段必然存在但值可为 null，`-> None` 表示零显式 outputs；
- parser-only、dump isolation、纯 AST/no import/eval/exec/reflection 和宽字段增长
  守护继续通过。

02B1 的 167 个测试全绿，说明 finite type、Literal collision safety、4096 位 integer
预算、异常隔离和确定性 canonical 语义没有回归。

## 6. 停止线与 NB-01

候选没有接旧 Registry scanner、Catalog、Handle UUID/fingerprint、旧
`@action(handles=...)`、D-068 implicit outputs、`.pyi`、Compiler、HTTP、SQLite、
SSE、前端或 Backend，也没有解析 return Name 到 class。

NB-01 仍为 `non-blocking-follow-up`：下一 production caller round 必须实现真实
module AST、module-scope、shadow-aware 的 import/definition resolver；当前纯声明
parser 不应提前承担该职责。

## 7. 实际门禁

全部使用：

```text
/home/changjunhan/.micromamba/envs/unilab/bin/python
```

结果：

```text
c1d05d9 forged decorator RED：
  2 failed, 0 passed

02B2 Action result 目标：
  106 passed in 0.99s

02B1 Parameter Annotation：
  167 passed in 1.25s

Registry：
  299 passed in 3.50s

25 个相邻 AST shape 与委托异常边界脚本：
  passed

Ruff E/F/I：
  All checks passed

Ruff format --check：
  7 files already formatted

git diff --check 5b7534d...9806d9a：
  passed

git diff --check 9806d9a...4f21857：
  passed
```

主执行者已在同一固定 production/test SHA 运行正式全量：

```text
1329 passed, 3 skipped
```

该结果仅作为同 SHA 门禁证据引用，本 Reviewer 没有冒充重复执行。

## 8. 顺序复审门禁

固定 production/test 候选
`9806d9a61699b2f17dcf4409353702f070df201a` 当前为：

```text
blocking:     0
non-blocking: 1（NB-01，未来 production caller 接线前关闭）
```

**允许进入顺序复审 2/3。** 若后续修改任何 production 或测试，必须固定新的候选
SHA，并使本报告失效后重新开始三名 reviewer 的顺序复审。
