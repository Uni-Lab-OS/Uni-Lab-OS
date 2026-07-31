# Round 02B1：Parameter Annotation 最终合并模块安全复审

日期：2026-07-31

评审分支：`review/02b1-module-safety-merge`

基线：`ca6083badf9ac7db299b30c4f2999f1f32f6a445`

固定 production/test 候选：
`4469953f1c5d47405b0e46adf7af07f4c971f1f6`

含趋势与最终合并合同复审的评审快照：
`d702d538691c3086d7402df90fd74d035d8934a0`

本次固定 delta：

- 独立 RED：`ce4e27d2fefb746543197a77c728804b19b211d1`；
- production 修复：`4469953f1c5d47405b0e46adf7af07f4c971f1f6`。

评审角色：最终合并顺序复审 2/3，Module safety / Standards reviewer。
Reviewer 未参与本轮 production 或测试编写；本报告只新增中文评审文档，不修改
production、测试、前端或 Backend，没有执行合并或推送，也没有启动其他 subagent。

## 1. 结论

**Blocking 数为 0；Non-blocking 数为 1。固定候选允许进入最终合并顺序复审
3/3。**

| 评审轴 | Blocking | Non-blocking | 结论 |
|---|---:|---:|---|
| OverflowError delta / Spec | 0 | 0 | 通过 |
| Module / safety | 0 | 1 | 通过 |
| Repository Standards | 0 | 0 | 通过 |

唯一 non-blocking 仍是既有 NB-01：未来 Registry/Compiler 生产 caller 必须从
真实 module AST 构造只含模块作用域、能识别名称遮蔽的 import map。02B1 没有接入
生产 caller，该风险当前不可达；后续接线 round 必须将其测试化并关闭。

`4469953..d702d53` 在 `unilabos/` 与 `tests/` 下没有差异，后续提交只更新趋势和
评审文档，因此本报告的 production/test 固定点没有漂移。

## 2. Spec：独立 RED 质量

**Disposition：`accepted-valid-red`**

`ce4e27d` 新增 8 个 cases，矩阵是四个公开可达 literal 位置乘以 complex 加减两条
标准库路径：

| literal 位置 | 稳定 path | `+ 1j` | `- 1j` |
|---|---|---:|---:|
| `Literal[...]` member | `/annotation` | 1 | 1 |
| 参数 default | `/default` | 1 | 1 |
| `Field(ge=...)` bound | `/annotation/metadata/0/ge` | 1 | 1 |
| 嵌套 JSON default | `/default` | 1 | 1 |

用例通过真实 `ast.parse(..., mode="eval")` 构造 AST。输入整数为 1,024 位十进制，
测试显式断言 `1024 < 4096`，因此它低于 Authoring integer 工作预算，却已经足以让
CPython `ast.literal_eval()` 的 integer→complex 转换抛出 `OverflowError`。每个
case 先直接证明标准库 expression 到达该异常，再从公开
`parse_parameter_annotation()` Interface 观察结果。

Reviewer 从 `ce4e27d` 的只读归档快照实际复跑：

```text
8 failed, 0 passed
```

八项首因均为：

```text
OverflowError: int too large to convert to float
```

四个位置的正负表达式都在旧实现的同一 `_literal_value()` seam 泄漏异常，尚未到达
测试的稳定错误断言。这是一个产品异常隔离缺口的完整位置/符号矩阵，不是八个产品
问题。

GREEN 断言只冻结公开错误 Interface：

- `code == "invalid_annotation"`；
- path 与 literal 所在位置完全一致；
- message 非空且包含简体中文；
- `str(error) == error.message`；
- 同一输入连续两次的 code/path/message 完全相同。

测试没有断言 production 的异常 tuple、helper 名称或局部状态，修复改成等价的
有界 AST 预检时仍可通过。153 行主要来自四位置 AST 构造、稳定错误 helper 和参数
矩阵，没有 skip、xfail 或弱化既有测试；未发现过拟合或无效重复。

## 3. Spec：OverflowError 修复

**Disposition：`accepted-fixed`**

`4469953` 的全部 production delta 是在现有 `ast.literal_eval(node)` 窄 seam 增加
一个输入异常类型：

```python
except (OverflowError, RecursionError, TypeError, ValueError):
    _fail(path)
```

`try` 块只包围标准库 literal 求值；后续 integer 工作栈、canonical validation 和
render 均不在捕获范围。这里的 `OverflowError` 是有限 literal AST 数值转换产生的
输入错误，将其按当前位置投影为 `AnnotationSchemaError` 符合 AST-only、
fail-closed 合同。

修复不接受 complex：能够被 `literal_eval()` 正常返回的小型 complex 仍在后续
严格 scalar/JSON Authority 中拒绝。它也没有新增类型、默认值形态、metadata 或
render 规则。

所有四个合同位置共享唯一 `_literal_value(node, path=...)` 实现：

- `_parse_literal()` 的每个 member；
- `_parse_field()` 的允许 keyword；
- `_parse_default()`；
- 嵌套 JSON default 也由 `_parse_default()` 进入同一 seam。

因此异常映射与 path 由 caller 传入位置决定，不存在四处复制的捕获逻辑或行为漂移。
这是关闭既有 seam 的最小修改，不需要新增公共 Interface、异常 adapter 或 speculative
class hierarchy。

## 4. 异常、递归与资源隔离

**Disposition：`accepted-safe`**

实现没有使用 `except Exception` 或裸 `except`。Reviewer 在固定候选替换
`ast.literal_eval` 后独立确认：

```text
OverflowError  -> 稳定 AnnotationSchemaError
RecursionError -> 稳定 AnnotationSchemaError
TypeError      -> 稳定 AnnotationSchemaError
ValueError     -> 稳定 AnnotationSchemaError

MemoryError       -> 原样传播同一实例
RuntimeError      -> 原样传播同一实例
SystemExit        -> 原样传播同一实例
KeyboardInterrupt -> 原样传播同一实例
自定义 BaseException -> 原样传播同一实例
```

因此资源耗尽、进程控制和无关实现错误没有被伪装成作者输入诊断。`OverflowError`
只在 `literal_eval` seam 内映射，未来 Implementation 自己在预算遍历、canonical
validator 或 renderer 中意外抛出的同名异常不会被吞掉。

既有 M-02 深 AST 行为未改变：`literal_eval` 的 `RecursionError` 稳定映射，成功
返回后的容器/integer 检查使用显式工作栈，不增加 Python 递归。新增捕获本身为
O(1) 时间和零常驻状态，不增加输入相关内存、后台任务、I/O、cache 或全局配置。

## 5. B01/B02 与旧 findings

**Disposition：均保持关闭**

### B01 / M-03：collision-safe 复杂度

Annotation 与 Workflow Schema 两层继续使用排序后相邻比较，不依赖 Python integer
hash，输出保持声明顺序，Workflow Schema 继续报告原输入中最早的第二次重复位置。
本次一行异常 delta 没有触碰判重实现。

Reviewer 使用固定随机顺序打乱 2,048/8,192 个相异但 hash 完全相同的 integer，
避免只测 TimSort 已排序快路径：

| 层 | 2,048 项 | 8,192 项 | 增长 |
|---|---:|---:|---:|
| Parameter Annotation | 0.021956 s | 0.093319 s | 4.25x |
| Workflow Schema | 0.019295 s | 0.079307 s | 4.11x |

四倍输入仍保持约四倍增长，8,192 项 descriptor 与打乱后的声明顺序完全一致。
最坏比较次数保持 O(n log n)，临时内存为 O(n)，没有恢复 O(n²) 或新增未经决策的
enum cap。

### B02：4096 位预算与 render closure

既有 `_AUTHORING_INTEGER_LIMIT = 10**4096`、迭代 literal 容器遍历和
parse/render/unparse 回归均未改变。累计测试继续覆盖 4,096 位接受并 round-trip、
4,097 位拒绝以及大型非十进制写法不能绕过。

Reviewer 额外确认 1,024 位和 4,097 位 integer 的 complex 加减在全部四入口均稳定
映射，操作前后 `sys.get_int_max_str_digits()` 不变。直接可信
`parse_input_contract()` 的 10,000 位 integer enum/default 原值保留；Authoring
工作预算没有变成 Workflow integer 类型或持久值上限。

### 既有 findings 表

| Finding | 最终 disposition | 本次复核 |
|---|---|---|
| B01 可预测 integer hash collision 导致 O(n²) | `accepted-fixed` | 两层 O(n log n) 保序判重与最早重复位置保持 |
| B02 integer render/unparse 闭包 | `accepted-fixed` | 4096 位预算保持；预算检查前的 OverflowError 已稳定隔离 |
| M-01 `ParsedParameter` 可伪造 | `accepted-fixed` | parser-only factory 与普通构造拒绝未变 |
| M-02 深 literal 泄漏 `RecursionError` | `accepted-fixed` | 同一窄 seam 的递归错误继续稳定映射 |
| M-03 enum 判重 O(n²) | `accepted-fixed` | 同 B01，没有恢复 hash 判重或平方扫描 |
| S-01 新构造器缺少 `-> None` | `accepted-fixed` | 两个构造器签名回归继续通过 |
| NB-01 import map 作用域/遮蔽 | `non-blocking-follow-up` | 当前没有 production caller，后续接线前关闭 |

## 6. Module depth 与 Standards

外部 Interface 仍集中为：

```text
parse_parameter_annotation(...) -> ParsedParameter
render_parameter_annotation(ParsedParameter) -> ast.expr
```

`_literal_value()` 是 Implementation 内部的公共 literal seam，隐藏 CPython
literal 失败集合、4096 位工作预算、迭代容器遍历和稳定错误投影。删除它会使
Literal、Field 和 default caller 分别重建相同求值/预算/异常规则，复杂度会重新
散到三个调用点；deletion test 成立。本次修复一次覆盖四个语义入口，体现了该深
Module 的 locality。

两层 enum 判重继续分别守卫 AST adapter 与可直接调用的 canonical Schema
Authority；删除任一层都会使一种合法入口失去唯一性检查，因此不是无价值的
Duplicated Code。

新增 production 只有一个精确异常类型，没有新名称、分支、公开参数或间接层。
新增测试用 `_LiteralSeam` 收拢 annotation/default/imports/path 这一组位置数据，
避免四套夹具散写。按仓库 Standards 与 Fowler baseline 复核：

- 注释、docstring、错误信息保持简体中文；
- Python 类型标注完整，S-01 未回归；
- 没有宽泛异常捕获、Mysterious Name、Feature Envy 或 Message Chains；
- 没有 Duplicated Code、Shotgun Surgery、Divergent Change；
- 没有 Speculative Generality、Middle Man 或新的外部 seam。

本候选没有修改 FE、Backend、HTTP、Catalog、SQLite、旧 Registry scanner 或
Compiler，也没有改变 Draft/Candidate/Apply 与 D-117 单编辑权，scope 正确。

## 7. 实际门禁

本 reviewer 使用：

```text
/home/changjunhan/.micromamba/envs/unilab/bin/python
```

实际结果：

```text
ce4e27d 独立 RED：
  8 failed, 0 passed（均为裸 OverflowError）

Parameter Annotation 四组目标：
  167 passed in 1.38s

Registry：
  193 passed in 3.12s

Workflow：
  644 passed, 4 warnings in 28.17s

异常透传、四入口 1024/4097 位、可信大整数、sys 只读和打乱碰撞脚本：
  passed

Ruff E/F/I：
  All checks passed

Ruff format --check：
  7 files already formatted

git diff --check ca6083b...4469953：
  passed

git diff --check 4469953...d702d53：
  passed
```

Workflow warnings 来自既有 FastAPI/TestClient、`param_resolver.py` escape 与
lifespan deprecated 提示，所有测试正常退出。主执行者已在同一固定
production/test SHA 登记正式全量：

```text
1223 passed, 3 skipped, 19 warnings
```

该完整结果仅作为主执行者同 SHA 门禁证据引用，本 reviewer 没有冒充重复执行。

## 8. 顺序复审门禁

固定 production/test 候选
`4469953f1c5d47405b0e46adf7af07f4c971f1f6` 的最终合并模块安全复审结论为：

```text
blocking:     0
non-blocking: 1（NB-01，后续生产 caller 接线前必须关闭）
```

允许进入最终合并顺序复审 3/3。若后续修改任何 production 或测试，必须固定新的
候选 SHA，并使本报告失效后重新开始三名 reviewer 的顺序复核。
