# Round 02B1：Parameter Annotation 最终风险确认复审

日期：2026-07-31

评审分支：`review/02b1-final-risk-confirm`

基线：`ca6083badf9ac7db299b30c4f2999f1f32f6a445`

固定 production/test 候选：
`c591f94d2a84486e730ad3ccd0e26d6be2376179`

含最终合同、模块安全与趋势文档的评审快照：
`5c81a80942b353b67cea3ee17cc56bacd1f78485`

评审角色：最终顺序独立复审 3/3，最终风险 reviewer。Reviewer 未参与本轮
production 或测试编写；本报告只新增评审文档，不修改 production、测试、前端或
Backend，也没有启动其他 subagent。

## 1. 结论

**Blocking 数为 1；Non-blocking 数为 1。固定候选当前不可合并。**

| 评审轴 | Blocking | Non-blocking | 结论 |
|---|---:|---:|---|
| 最终安全 / 回归风险 | 1 | 1 | 不通过 |
| Repository Standards / scope | 0 | 0 | 通过 |

旧风险状态：

| Finding | 最终 disposition | 说明 |
|---|---|---|
| B01 可预测 integer hash collision 恢复 O(n²) | `accepted-fixed` | 两层排序相邻比较不依赖 hash，保序且为 O(n log n) |
| B02 大 integer 破坏 render/unparse 闭包 | `reopened-partial` | 4096 位预算与普通边界已修复，但预算检查前仍可泄漏 `OverflowError` |
| M-01 parser-only 对象可伪造 | `accepted-fixed` | 普通构造路径继续关闭 |
| M-02 深 literal 泄漏 `RecursionError` | `accepted-fixed` | 递归异常继续稳定投影 |
| S-01 构造器返回标注 | `accepted-fixed` | 两处 `-> None` 继续存在 |
| NB-01 import map 作用域/遮蔽 | `non-blocking-follow-up` | 当前没有生产 caller，留待接线 round |

新的 blocking 仍位于 B02 的同一个不可信 literal seam：合法 Python AST 中的
“较大 integer 与 complex literal 运算”会让 `ast.literal_eval()` 在 integer 工作
预算检查之前抛裸 `OverflowError`，没有稳定的 code/path/中文 message。

## 2. B01：排序型 enum 判重

**Disposition：`accepted-fixed`**

### 2.1 最坏复杂度与内存

Annotation 层在
`unilabos/registry/annotation_schema.py:146-148,182-184`
对已经通过严格 family 验证的临时副本排序，只比较相邻项，成功结果仍返回原
`values` list。

Workflow Schema 层在
`unilabos/workflow/schema.py:161-179,262-297`
先规范化并验证每个成员，再排序 `(原索引, 值)`，从相邻等价项计算最早第二次出现
索引，成功结果仍返回原顺序的 `normalized` list。

两层都不调用 numeric hash，最坏比较次数为 O(n log n)，临时内存为 O(n)。排序输入
只含 exact `str`、`bool`、`int` 或 finite `float`，不会调用作者对象自定义的
`__hash__`、`__eq__` 或比较方法。

独立对抗脚本把相同 integer hash 的值以固定随机顺序打乱，避免只测 TimSort 已排序
快路径：

```text
2,048 项：0.024235 s
8,192 项：0.096187 s
四倍输入增长：3.97x
8,192 项 dump 与原随机声明顺序完全相同
```

没有恢复 B01 的 O(n²)，也没有新增未经决策的 enum 成员数上限。

### 2.2 严格数值、NaN 与重复位置

Annotation 在排序前按 exact type 划分 family，Workflow Schema 在排序前调用严格
scalar normalization，因此：

- boolean 不会混入 integer/number；
- NaN 与 infinity 在排序前拒绝，不会破坏 total ordering；
- number 的 `1`/`1.0`、`-0.0`/`0` 继续按冻结数值等价判重；
- integer 的 integral float 先规范化为 integer；
- string、boolean、integer、number 的声明顺序均不依赖排序结果；
- `[2, 1, 2, 1]` 仍报告最早第二次出现的 index `2`；
- `[1, 1, 1]` 仍报告 index `1`。

可信 Workflow Schema 的约 10,000 位 integer 与 finite float 混合 enum 可以排序、
canonical dump 保持原值，且 `sys.get_int_max_str_digits()` 不变。

候选把 duplicate 检查放在所有 member constraint 检查之后，因此一个同时包含
“较早 duplicate”和“较晚 constraint violation”的多重非法 Schema 可能先报告
constraint path；例如 `[1, 1, 100]` 配合 `maximum=10` 报 index `2`。D-082～D-091、
设计与现有测试没有冻结多个同时错误之间的优先级，所有单一错误和 duplicate-only
路径仍稳定，故不把这一观察升级为 finding。

## 3. B02：Authoring integer 工作预算

### 3.1 已正确关闭的部分

`annotation_schema.py:31-32,127-143` 将 Authoring literal 中 exact integer 的
canonical 十进制绝对值限制为最多 4096 位，并用显式工作栈遍历
list/tuple/set、dict key 与 dict value。

独立复核确认：

```text
10**4096 - 1：
  接受；parse -> canonical -> render -> ast.unparse -> reparse 完全闭包

10**4096：
  在 Literal/default/Field/nested JSON 位置稳定拒绝

正负大型十六进制、八进制、二进制：
  均按数学值的 canonical 十进制位数拒绝，源码进制不能绕过

直接可信 WorkflowInputContract：
  10**9999 enum 保持原值，不受 Authoring 预算限制

sys.get_int_max_str_digits()：
  所有操作前后不变
```

因此工作预算没有变成新的持久 Workflow integer 类型上限，也没有通过修改
`sys.set_int_max_str_digits()` 改变进程全局语义。

### 3.2 Blocking：`literal_eval` 可在预算检查前泄漏 `OverflowError`

**Disposition：`blocking-open`**

`annotation_schema.py:127-131` 先调用 `ast.literal_eval()`，但只捕获：

```python
(RecursionError, TypeError, ValueError)
```

integer 位数检查在 `literal_eval()` 成功返回后的
`annotation_schema.py:132-143` 才执行。CPython 的 `literal_eval` 接受
“signed number ± complex number”这一有限语法；当 integer 转成 complex float
超过浮点范围时，它抛 `OverflowError`。

最小复现使用正常 `ast.parse`，不是伪造 AST：

```python
hex_value = "f" * 300
annotation = ast.parse(
    f"Literal[0x{hex_value} + 0j]",
    mode="eval",
).body
parse_parameter_annotation(
    "value",
    annotation,
    default=NO_DEFAULT,
    imports=MappingProxyType({"Literal": "typing:Literal"}),
)
```

结果：

```text
OverflowError: int too large to convert to float
code:    absent
path:    absent
message: 非冻结中文诊断
```

300 个十六进制位对应约 362 个十进制位，明显低于 4096 位 Authoring integer
预算。它本应因 complex 不是 D-082/D-091 scalar family 而稳定返回
`AnnotationSchemaError("/annotation")`，而不是把标准库异常泄漏给 caller。

同一普通源码形状在以下四个 `_literal_value()` 位置均独立复现裸
`OverflowError`：

| 位置 | 预期稳定 path |
|---|---|
| `Literal[large_int + 0j]` | `/annotation` |
| `float = large_int + 0j` default | `/default` |
| `Field(ge=large_int + 0j)` | `/annotation/metadata/0/ge` |
| 嵌套 JSON default 中的 `large_int + 0j` | `/default` |

正/负 integer 以及 `+ 0j`/`- 0j` 都会进入同一转换边界。超过 4096 位的
non-decimal integer 也会在应由工作预算拒绝之前泄漏同一异常。

关闭条件应保持当前窄异常边界：在 `ast.literal_eval` seam 将该输入稳定映射为
`AnnotationSchemaError`，或在求值前以有界 AST 检查阻止转换；不得使用宽泛
`except Exception`，不得修改全局 integer 转换限制，也不得执行作者表达式。
独立回归至少覆盖四个位置、正负号、预算内但超过 float 范围及超预算两类输入。

## 4. 其他安全与 closure 复核

### 4.1 parser-only Authority

`annotation_schema.py:53-80` 继续使用 `init=False`、拒绝普通 `__new__` 与模块 token
factory。空/多参数 contract、非法 metadata 和看似合法状态都不能通过普通
`ParsedParameter(...)` 构造；合法 parser 结果仍可比较、哈希、独立 dump 和 render。
M-01 保持关闭。

### 4.2 无 import/eval/exec 与 runtime helper

新增/修改 production 没有 `importlib`、`eval`、`exec`、`__import__`、文件、网络、
进程或全局状态写入。`annotations.py` 仍只提供标准库 typing alias 和冻结 metadata
carrier，不加载 Pydantic、驱动、作者模块或 Catalog。

深 literal 的 `RecursionError` 仍在 `_literal_value` seam 稳定投影，后续 integer
容器遍历使用迭代工作栈，不新增 Python 递归。除本报告明确的 `OverflowError` 外，
没有发现新的普通源码异常逃逸或 parse/render closure 破坏。

### 4.3 NB-01

**Disposition：`non-blocking-follow-up`**

当前 `unilabos/` 中除定义外没有 `parse_parameter_annotation` production caller，
本轮停止线也明确不接旧 Registry scanner/Compiler。未来接线必须从真实 module AST
构造只含模块作用域、能识别名称遮蔽的 import map，并增加嵌套 import、`Assign`/
`AnnAssign` 遮蔽与 render closure 集成测试。

因此旧 NB-01 在 02B1 仍合理后移，不要求本轮猜测或扩大未来 caller Interface。

## 5. D-082～D-092/D-100 与 scope

除 B02 异常边界外，候选继续维持有限 v1 类型、严格 default/null、nullable、
`Literal`、Field、presentation 和 ResourceTemplate symbol 合同；未知类型不回退，
descriptor 仍交给唯一 `WorkflowInputContract` Authority。

本轮没有接 HTTP、Catalog、SQLite、旧 Registry scanner、完整 Compiler、
transform/generate-python、Action result record、FE 或 Backend；没有改变
Draft/Candidate/Apply 或 D-117 单编辑权。D-100 的 result record 与 NB-01 caller
接线仍属于后续独立 round，scope 正确。

## 6. 实际门禁

全部本 reviewer 实际运行的命令使用：

```text
/home/changjunhan/.micromamba/envs/unilab/bin/python
```

结果：

```text
Parameter Annotation 三组目标：
  159 passed in 1.41s

02A Schema/route 累计：
  212 passed, 2 warnings in 2.34s

Registry：
  185 passed in 3.14s

Workflow：
  644 passed, 3 warnings in 27.49s

Ruff E/F/I：
  All checks passed

Ruff format --check：
  6 files already formatted

git diff --check ca6083b...c591f94：
  passed

git diff --check c591f94...5c81a80：
  passed
```

主执行者已在同一固定 production/test SHA 运行正式全量：

```text
1215 passed, 3 skipped
```

该完整结果仅作为同 SHA 门禁证据引用，本 reviewer 没有冒充重复执行。现有自动测试
全部通过，但没有覆盖 B02 的 complex conversion `OverflowError`。

## 7. 合并门禁

固定 production/test 候选
`c591f94d2a84486e730ad3ccd0e26d6be2376179`
当前为：

```text
blocking:     1（B02 literal_eval OverflowError）
non-blocking: 1（NB-01，后续 caller 接线前关闭）
```

**不允许合并到 `integration/workflow-task-runtime`。**

关闭 B02 后必须：

1. 由独立测试作者先冻结上述 literal 位置与符号/预算矩阵；
2. production 修复后固定新的 production/test SHA；
3. 重跑三组目标、02A、Registry、Workflow、正式全量、Ruff、format 和 diff；
4. 三名独立 reviewer 对新 SHA 依次复核；
5. 更新趋势报告，任何 production/test 变化使当前三份最终复审失效。

该修复只需要闭合现有 literal seam，不要求接未来 caller，不要求修改 FE、Backend、
HTTP、Catalog、SQLite 或 Action result record。
