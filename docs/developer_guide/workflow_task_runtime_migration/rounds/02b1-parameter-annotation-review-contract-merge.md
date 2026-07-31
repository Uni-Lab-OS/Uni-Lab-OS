# Round 02B1：Parameter Annotation 最终合并合同复审

日期：2026-07-31

评审分支：`review/02b1-contract-merge`

基线：`ca6083badf9ac7db299b30c4f2999f1f32f6a445`

固定 production/test 候选：
`4469953f1c5d47405b0e46adf7af07f4c971f1f6`

含最终趋势文档的评审快照：
`072a8984d54ff82fd365eea17705bb4ff42cf0a2`

本次固定 delta：

- 独立 RED：`ce4e27d2fefb746543197a77c728804b19b211d1`
- production 修复：`4469953f1c5d47405b0e46adf7af07f4c971f1f6`

评审角色：最终合并顺序复审 1/3，合同 / Spec reviewer。Reviewer 未参与本轮
production 或测试编写；本报告不修改 production、测试、前端或 Backend，也没有
启动其他 subagent。

## 1. 结论

**Blocking 数为 0；Non-blocking 数为 1。固定候选允许进入最终合并顺序复审
2/3。**

| 评审轴 | Blocking | Non-blocking | 结论 |
|---|---:|---:|---|
| OverflowError delta / Spec | 0 | 0 | 通过 |
| D-082～D-092 与既有风险闭包 | 0 | 1 | 通过 |
| Repository Standards / scope | 0 | 0 | 通过 |

唯一 non-blocking 仍是既有 NB-01：未来 Registry/Compiler 生产 caller 必须从
真实 module AST 构造只含模块作用域、能识别名称遮蔽的 import map。02B1 没有接入
生产 caller，该风险当前不可达；后续接线 round 必须将它测试化并关闭。

`4469953..072a898` 在 `unilabos/` 与 `tests/` 下没有差异，后续提交只更新趋势
文档，因此 production/test 固定点没有漂移。

## 2. 独立 RED 是否准确

**Disposition：`accepted-valid-red`**

`ce4e27d` 新增 8 个 cases，以四个 `_literal_value()` 公共可达位置乘以 complex
加减两种标准库路径：

| literal seam | 预期稳定 path | cases |
|---|---|---:|
| `Literal[...]` member | `/annotation` | 2 |
| 参数 default | `/default` | 2 |
| `Field(ge=...)` bound | `/annotation/metadata/0/ge` | 2 |
| 嵌套 JSON default | `/default` | 2 |

测试使用 1,024 位十进制 integer 与 `1j` 做加减。该 integer 明确低于 4,096 位
Authoring 预算，因此失败不能被误归因于预算超限；它准确到达 CPython
`ast.literal_eval()` 的 integer→complex conversion seam。每个 case 先独立断言
对应 expression 的 `ast.literal_eval()` 确实抛 `OverflowError`，再从公开
`parse_parameter_annotation()` 观察稳定诊断。

Reviewer 在独立 detached checkout 的 `ce4e27d` 上实际复跑：

```text
8 failed, 0 passed
```

八项首因均为：

```text
OverflowError: int too large to convert to float
```

异常分别从上述四个公共 seam 泄漏，测试对
`AnnotationSchemaError.code/path/message` 的断言尚未到达。它证明的是同一个
标准库失败面在四个合同位置的完整投影缺口，不是把一个实现细节重复算成八个产品
问题。

修复后测试连续调用两次 parser，并冻结：

- `code == "invalid_annotation"`；
- path 与所在 seam 完全一致；
- message 非空、为简体中文；
- `str(error) == error.message`；
- 两次 code/path/message 完全相同。

这些断言均来自公开错误 Interface，没有断言 production 捕获 tuple、局部变量或
具体修复写法。测试同时用真实 `ast.parse` 构造 AST，不依赖伪造节点。RED 准确且
没有弱化既有合同。

## 3. OverflowError 修复的合同边界

**Disposition：`accepted-fixed`**

`4469953` 的全部 production delta 是：

```python
except (OverflowError, RecursionError, TypeError, ValueError):
    _fail(path)
```

新增捕获只位于纯 `ast.literal_eval(node)` seam。该标准库函数不会 import、执行
作者代码或调用作者自定义对象；输入只是已经解析的有限 literal AST。因此这里的
`OverflowError` 表示不支持的 literal 数值转换，按当前位置投影为稳定
`AnnotationSchemaError` 符合 AST-only、失败关闭合同。

实现没有使用 `except Exception` 或裸 `except`。独立异常注入确认：

```text
MemoryError       -> 原样传播
SystemExit        -> 原样传播
KeyboardInterrupt -> 原样传播
RuntimeError      -> 原样传播
```

因此修复没有掩盖资源耗尽、进程控制或实现错误。既有
`RecursionError/TypeError/ValueError` 映射未改变。

捕获 `OverflowError` 不会接受 complex。若普通小型 complex expression 能由
`literal_eval()` 成功返回，后续严格 scalar/JSON validator 仍会拒绝。独立复核：

```text
Literal[1 + 1j] -> invalid_annotation /annotation
Literal[1 - 1j] -> invalid_annotation /annotation
```

D-082 的有限 scalar 集、D-083 严格类型与 D-091 `Literal` scalar family 均没有
新增 complex；default、Field bound 与 opaque JSON 也仍由同一个严格 canonical
Authority 拒绝 complex。

## 4. 4096 位预算、可信整数和全局状态

**Disposition：`accepted-unchanged`**

本次 production 只向异常 tuple 增加一个类型，没有修改：

- `_AUTHORING_INTEGER_DIGITS = 4096`；
- `_AUTHORING_INTEGER_LIMIT = 10**4096`；
- `literal_eval()` 成功后的迭代 integer 工作预算遍历；
- `WorkflowInputContract` 的可信 canonical integer 语义；
- deterministic render 或 `ast.unparse()`；
- 任何 `sys` 设置。

四组最终风险回归继续覆盖 4,096 位接受并 round-trip、4,097 位稳定拒绝、大型
非十进制写法不能绕过，以及所有 literal 位置的预算。Reviewer 另从直接可信
`parse_input_contract()` 输入约 5,000 位 integer enum/default，canonical dump
保持原值。

操作前后：

```text
sys.get_int_max_str_digits() == 4300
```

production 没有调用 `sys.set_int_max_str_digits()`。因此新增异常隔离没有把
Authoring 工作预算变成 Workflow 类型或持久值上限，也没有修改进程全局转换语义。

## 5. B01/B02 与旧 findings

`c591f94..4469953` 除独立 overflow 测试外只有上述一行 production 变化，原风险
关闭机制没有被改写。167 个累计目标用例全部通过。

| Finding | 最终 disposition | 本次复核 |
|---|---|---|
| B01 可预测 integer hash collision 导致 O(n²) | `accepted-fixed` | 两层 collision-safe O(n log n) 排序相邻比较保持不变，原序与最早重复位置测试继续通过 |
| B02 integer render/unparse 闭包 | `accepted-fixed` | 4096 位预算保持；本次补齐预算检查前的 `OverflowError` 稳定投影 |
| M-01 `ParsedParameter` 可伪造 | `accepted-fixed` | parser-only token factory 与普通构造拒绝未变 |
| M-02 深 literal 泄漏 `RecursionError` | `accepted-fixed` | 窄异常映射保持，并新增同层 `OverflowError` |
| M-03 enum 判重 O(n²) | `accepted-fixed` | 同 B01，未恢复 hash 或平方扫描 |
| S-01 构造器缺 `-> None` | `accepted-fixed` | 两个公开构造器签名回归继续通过 |
| NB-01 import map 作用域/遮蔽 | `non-blocking-follow-up` | 当前仍无 production caller，合理后移到接线 round |

B02 此前已关闭的大整数位数边界没有被重新打开；本次修复关闭的是同一
`_literal_value()` seam 在预算检查之前的标准库异常投影缺口。修复后不支持的
complex 仍失败关闭，故没有以“避免异常”为由扩大 v1 类型集合。

## 6. D-082～D-092 与 scope

累计四组测试共 167 个 cases，继续覆盖有限类型、nullable、Literal、Field、
presentation、ResourceTemplate identity、default、canonical immutability、
确定性 render、复杂度和资源预算。

本次 delta 没有新增类型、语法、默认值形态、metadata、import identity 或
normalized Python 表示。异常映射发生在所有 literal 的唯一深模块 seam，因此
Workflow 与后续 Action caller 不会各自形成不同的异常行为，符合 D-088 的共享
parser 和 D-092 的 AST-only authoring 方向。

候选没有接 HTTP、Catalog、SQLite、旧 Registry scanner、Compiler、FE 或 Backend；
没有改变 Draft/Candidate/Apply 或 D-117 单编辑权交互。Action result record 仍
留在后续 round，scope 没有扩张。

## 7. Standards

production 只增加一个精确异常类型，没有新公共 Interface、分支层级或重复逻辑。
错误继续在原深模块边界内稳定投影，caller 不需要了解 CPython
`literal_eval()` 的失败集合。

注释、docstring 与错误信息仍使用简体中文；类型标注未改变。没有发现仓库标准
违反，也没有发现需要升级为 finding 的 Speculative Generality、Duplicated
Code、Shotgun Surgery 或宽泛异常捕获 smell。

## 8. 实际门禁

全部使用：

```text
/home/changjunhan/.micromamba/envs/unilab/bin/python
```

结果：

```text
ce4e27d 独立 RED：
  8 failed, 0 passed

Parameter Annotation 四组目标：
  167 passed in 1.23s

02A Schema/route 累计：
  212 passed, 2 warnings in 2.33s

Registry：
  193 passed in 3.12s

complex 拒绝、异常透传、可信大整数与 sys 设置只读脚本：
  passed

Ruff E/F/I：
  All checks passed

Ruff format --check：
  7 files already formatted

git diff --check ca6083b...4469953：
  passed

git diff --check 4469953...072a898：
  passed
```

主执行者已在同一固定 production/test SHA 运行正式全量：

```text
1223 passed, 3 skipped
```

该完整结果仅作为同 SHA 门禁证据引用，本 Reviewer 没有冒充重复执行。

## 9. 顺序复审门禁

固定 production/test 候选
`4469953f1c5d47405b0e46adf7af07f4c971f1f6` 的最终合并合同复审结论为：

```text
blocking:     0
non-blocking: 1（NB-01，后续生产 caller 接线前必须关闭）
```

允许进入最终合并顺序复审 2/3。若后续修改任何 production 或测试，必须固定新的
候选 SHA，并使本报告失效后重新开始三名 reviewer 的顺序复核。
