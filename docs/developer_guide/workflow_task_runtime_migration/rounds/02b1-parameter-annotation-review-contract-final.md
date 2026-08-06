# Round 02B1：Parameter Annotation 最终合同复审

日期：2026-07-31

评审分支：`review/02b1-contract-final`

基线：`ca6083badf9ac7db299b30c4f2999f1f32f6a445`

固定 production/test 候选：
`c591f94d2a84486e730ad3ccd0e26d6be2376179`

含最终决策与趋势文档的评审快照：
`b44278012e50eeaa79ee24fe966c9f698fe11975`

评审角色：最终顺序独立复审 1/3，合同 / Spec reviewer。Reviewer 未参与本轮
production 或测试编写；本报告不修改 production、测试、前端或 Backend，也没有
启动其他 subagent。

## 1. 结论

**Blocking 数为 0；Non-blocking 数为 1。固定候选允许进入最终顺序复审 2/3。**

| 评审轴 | Blocking | Non-blocking | 结论 |
|---|---:|---:|---|
| D-082～D-091 / 02B1 Spec 合同 | 0 | 1 | 通过 |
| D-117 / round scope | 0 | 0 | 通过 |
| Repository Standards | 0 | 0 | 通过 |

唯一 non-blocking 仍是既有 NB-01：未来 Registry/Compiler 生产 caller 必须从真实
module AST 构造只含模块作用域、能识别名称遮蔽的 import map。02B1 按停止线没有
接入任何 caller，因此该风险当前不可达；它必须在后续接线 round 测试化并关闭。

`c591f94..b442780` 在 `unilabos/` 与 `tests/` 下没有差异，后续两个提交只更新
决策/趋势文档，故本报告的 production/test 固定点没有漂移。

## 2. 最终 B01：collision-safe enum 判重

**Disposition：`accepted-fixed`**

Annotation 层
`unilabos/registry/annotation_schema.py:146-184` 在完成严格 scalar family
验证后，对临时副本排序并只比较相邻值。它返回的 `enum` 仍是原始 `values` list，
因此声明顺序不会被排序改写。算法不依赖 Python integer hash 均匀性，没有新增
未经决策的 enum 数量上限，最坏比较次数为 O(n log n)。

Canonical Workflow Schema 层
`unilabos/workflow/schema.py:161-179,262-297` 先严格规范化每个成员，再排序
`(原索引, 值)`，通过相邻比较发现数值等价项，最后返回未重排的 `normalized`
list。若存在多组重复，函数返回所有“第二次出现”索引中的最小值，保持既有稳定
JSON Pointer：

```text
[2, 1, 2, 1] -> /parameters/0/schema/enum/2
[1, 1, 1]     -> /parameters/0/schema/enum/1
```

严格语义没有被排序放宽：

- Annotation 在判重前拒绝 `Literal[True, 1]` 的混族；
- canonical integer/number 在判重前拒绝 boolean 和非有限 float；
- number 的 `1`/`1.0` 与 `-0.0`/`0` 仍按冻结的数值等价语义判重；
- string、boolean、integer 和 number 各自保持 D-083 类型边界；
- enum dump 与确定性 render 均保留声明顺序。

最终风险测试分别从 Parameter parser 和直接 `WorkflowInputContract` parser 输入
2,048 与 8,192 个可预测 integer hash collision，并要求四倍输入增长低于
`8x + 0.02s`，同时断言 8,192 个值完整保序。两层测试均通过。普通逆序
1,000 → 4,000 成员、宽重复和原 D-091 矩阵也继续通过。因此旧 M-03 及风险
B-01 均已关闭，而不是只优化平均 hash 分布。

## 3. 最终 B02：Authoring integer 工作预算与 render 闭包

**Disposition：`accepted-fixed`**

`annotation_schema.py:31-32,127-143` 把 Authoring AST 中 integer 的 canonical
十进制绝对值预算冻结为最多 4,096 位。`ast.literal_eval()` 后使用迭代工作栈检查
完整 literal，包括：

- `Literal` member；
- 参数 default；
- `Field` bound；
- 嵌套 JSON default 的 object key/value 与 list/tuple/set member。

判断依据是解析后的数学 integer 绝对值，而不是源码 token 长度或进制，所以大型
十六进制、八进制和二进制写法不能绕过预算。超预算统一投影为稳定、简体中文的
`AnnotationSchemaError`；不会把后续 `ast.unparse()` 的 4,300 位转换
`ValueError` 泄漏给 caller。

边界复核结果：

```text
10**4096 - 1：
  4,096 位，parse -> render -> ast.unparse -> reparse descriptor 相同

10**4096：
  4,097 位，在 Literal/default/Field/nested JSON 四类位置稳定拒绝

0xffff...（4,000 个十六进制位）：
  按 canonical 十进制位数稳定拒绝
```

测试同时在每次操作前后断言 `sys.get_int_max_str_digits()` 不变。production 没有
调用 `sys.set_int_max_str_digits`，没有改变进程全局行为。

此预算只位于 AST Authoring adapter。直接、可信的
`parse_input_contract()` 继续接受约 5,000 位 integer enum/default，且
`to_dict()` 保持原值；`unilabos/workflow/schema.py` 没有加入 integer 位数上限。
因此它符合设计与 D-101 的“工作预算而非 Workflow 类型/持久值上限”，没有缩窄
D-083 的内部数学整数语义。风险 B-02 已关闭。

## 4. 旧 findings 最终复核

| Finding | 最终 disposition | 证据 |
|---|---|---|
| M-01 `ParsedParameter` 可由 caller 伪造 | `accepted-fixed` | `init=False`、拒绝普通 `__new__`、模块 token factory；6 组普通伪造均在使用前抛 `TypeError` |
| M-02 深 literal 泄漏 `RecursionError` | `accepted-fixed` | 只在 `ast.literal_eval` seam 捕获 `RecursionError/TypeError/ValueError` 并稳定投影；没有宽泛 `except` |
| M-03 enum 判重 O(n²) | `accepted-fixed` | 最终采用两层 collision-safe O(n log n) 相邻比较；参见 B01 |
| S-01 新构造器缺少 `-> None` | `accepted-fixed` | `AnnotationSchemaError` 与 `AllowedResourceTemplates` 构造器均有显式返回标注 |
| NB-01 import map 作用域/遮蔽 | `non-blocking-follow-up` | 当前没有生产 caller；后续接线 round 必须测试化并关闭 |

M-01 的 parser-only 值仍能正常比较、哈希、独立 `to_dict()` 与确定性 render；
关闭伪造入口没有改变合法 Interface。M-02 的捕获范围没有吞掉 `RuntimeError`、
`MemoryError` 或其他实现错误。S-01 同时有 public signature 回归测试。

## 5. D-082～D-091 合同与 D-117 scope

三组独立测试共 159 个 cases，继续覆盖：

- 完整 v1 scalar、opaque JSON object、ResourceSlot 与一维同质 list 类型矩阵；
- `Optional[T]` / `T | None` 归一、非法 Union、嵌套 nullable 和 nullable item；
- 四类严格 `Literal`、number widening、保序、重复、混族与非有限拒绝；
- `Field` 的有限 keyword、类型匹配、上下界及 presentation precedence；
- `AllowedResourceTemplates` 的静态 import identity、顺序、重复与动态表达式拒绝；
- required、optional non-null、optional nullable 三种 default 形态；
- canonical 深不可变、稳定错误以及 parser 不 import/eval/exec 作者代码；
- M-01～M-03、S-01 与最终 B01/B02 的对抗回归。

production 仍只提供共享 Parameter Annotation 深模块和源码可 import 的有限 helper；
它把完整 descriptor 交给唯一 `WorkflowInputContract` Authority 校验，没有复制
第二套 default/value validator，也没有对未知类型回退。

本轮没有接 HTTP、Catalog、SQLite、旧 Registry scanner、完整 compiler、
transform/generate-python 或 Action result record；没有修改 Draft、Candidate、
Apply、前端编辑模式或 Backend。因而没有提前实现或改变 D-117 的单编辑权交互，
也没有把 FE/Backend 状态塞进 Annotation Interface。D-117 只作为后续生产
Authoring Interface 完成后触发独立 FE 分支和 FE-OS 联调的边界，本轮 scope
正确。

## 6. Standards 与模块边界

新增 production 的公开入口仍只有 parser、renderer、sentinel 和两个冻结值类型。
约束解析、canonical 校验与渲染细节均封装在模块内，没有把复杂度推给 caller。

两层 enum 判重分别守卫 AST→canonical 与可被直接调用的 canonical Schema
Authority；任意删除一层都会让另一种合法 caller 失去严格唯一性，因此不是可删除
的重复代码。两层在错误类型、路径和 normalization 前置条件上也不同。

注释、docstring 与运行时错误信息使用简体中文；新增/修改 Python 的类型标注完整。
未发现 documented-standard 违反，也未发现需要升级为 finding 的 Mysterious
Name、Duplicated Code、Speculative Generality、Shotgun Surgery 或其他 Fowler
baseline smell。

## 7. 实际门禁

全部使用：

```text
/home/changjunhan/.micromamba/envs/unilab/bin/python
```

结果：

```text
Parameter Annotation 三组目标：
  159 passed in 1.36s

02A Schema/route 累计：
  212 passed, 2 warnings in 2.32s

Registry：
  185 passed in 3.09s

Workflow：
  644 passed, 3 warnings in 27.29s

正式 python -m pytest tests -q：
  1215 passed, 3 skipped, 19 warnings in 68.37s

Ruff E/F/I：
  All checks passed

Ruff format --check：
  6 files already formatted

git diff --check ca6083b...c591f94：
  passed

git diff --check c591f94...b442780：
  passed
```

warnings 来自既有 FastAPI/TestClient、ROS test class、SOCKS 可选依赖与 lifespan
deprecated 提示；测试全部正常退出。评审 worktree 在写报告前保持 clean。

## 8. 顺序复审门禁

固定 production/test 候选
`c591f94d2a84486e730ad3ccd0e26d6be2376179` 的合同复审结论为：

```text
blocking:     0
non-blocking: 1（NB-01，后续 caller 接线前必须关闭）
```

允许进入最终顺序复审 2/3。若后续修改任何 production 或测试，必须固定新的候选
SHA，并使本报告失效后重新开始三名 reviewer 的顺序复核。
