# Round 02B1：Parameter Annotation 模块安全与 Standards 评审

日期：2026-07-31

评审分支：`review/02b1-module-safety`

基线：`ca6083badf9ac7db299b30c4f2999f1f32f6a445`

固定 production/test 候选：`097d0df26d4555be42bde0889153e5596d83f2dd`

参考合同评审提交：`e8ab7e88732b79fcd60baa0e1ca499b487bbadb1`

评审角色：顺序独立评审 2/3。Reviewer 未参与本轮实现或测试编写；本报告只新增
评审文档，不修改 production、测试、前端或 Backend，也没有启动其他 subagent。

## 1. 结论

**Blocking 数为 4；固定候选当前不可进入顺序评审 3/3 或 integration 合并候选。**

| 评审轴 | Blocking | Non-blocking | 结论 |
|---|---:|---:|---|
| Module / safety | 3 | 1 | 不通过 |
| Repository Standards | 1 | 0 | 不通过 |

三个 Module blocking 都可通过当前公开 Interface 独立复现，不依赖尚未实现的
Registry/Compiler caller 接线：

1. `ParsedParameter` 的公开构造器允许伪造违反单参数及 metadata 不变量的值；
2. 合法解析得到的约 5 KB 深一元 AST 会泄漏裸 `RecursionError`；
3. 唯一 `Literal` 成员的重复检查是二次复杂度，约 19 KB 输入已耗时约 0.88 秒。

唯一 Standards blocking 是两个本轮新增的 `__init__` 缺少 `-> None`。这四项关闭
后必须产生新的 production/test SHA，重跑正式门禁，并使现有顺序评审失效。

## 2. Blocking findings

### M-01：`ParsedParameter` 可伪造并破坏 Module Interface 不变量

**Disposition：`blocking-open`**

设计 `02b1-parameter-annotation-design.md:66-73` 要求 `ParsedParameter` 内部持有
“由 `WorkflowInputContract` parser 构造的单参数 canonical contract”，冻结保存
合法的 `ResourceTemplateSymbol`。但
`unilabos/registry/annotation_schema.py:48-53` 使用普通公开 dataclass
构造器，没有 parser-only token、`init=False` 或 `__post_init__` 不变量校验。

只使用仓库公开类型即可构造三种 nominal `ParsedParameter`：

```text
ParsedParameter(empty_input_contract, ()).to_dict()
=> IndexError: list index out of range

ParsedParameter(string_contract, (ResourceTemplateSymbol("plate", ...),))
=> render 为 Annotated[str, AllowedResourceTemplates(plate)]
=> 该 render 结果无法由同一 parser 接受

ParsedParameter(string_contract, (object(),))
=> render 时 AttributeError: 'object' object has no attribute 'local_name'
```

这不是通过 `object.__setattr__` 破坏 Python 对象的对抗，而是 dataclass 正常公开
构造路径。`render_parameter_annotation()` 在
`annotation_schema.py:605-607` 只检查 nominal class，随后相信内部状态，因此一个
通过 Interface 类型检查的对象既可能泄漏非稳定异常，也可能违反设计
`188-199` 的 parse/render closure。

冻结 dataclass 只能阻止构造后的字段赋值，不能证明值在构造时合法。应让
`ParsedParameter` 只能由本模块 parser 构造，或在构造时完整验证：

- contract 恰有一个参数；
- `_contract` 与 metadata 类型合法；
- ResourceTemplate metadata 只与 slot shape 配对；
- symbol identity 非空、唯一且可确定性 render。

测试应覆盖正常公开构造无法伪造上述状态，而不只覆盖返回对象的字段不可赋值。

### M-02：深一元 literal 泄漏裸 `RecursionError`

**Disposition：`blocking-open`**

设计 `02b1-parameter-annotation-design.md:75-81,204-216` 明确要求不支持或不确定的
AST 统一产生稳定 `AnnotationSchemaError(code, path, message)`。实现
`annotation_schema.py:100-104` 调用 `ast.literal_eval()`，却只捕获
`TypeError` 和 `ValueError`。

以下约 5 KB 的输入可由 Python `ast.parse(..., mode="eval")` 正常解析，并直接调用
本轮公开 parser：

```python
default = ast.parse("-" * 5000 + "1", mode="eval").body
parse_parameter_annotation(
    "value",
    ast.Name(id="int", ctx=ast.Load()),
    default=default,
    imports=MappingProxyType({}),
)
```

实际结果：

```text
RecursionError
code/path/message 均不存在
```

该表达式不应成为合法 default，但合同要求它“失败关闭并稳定投影”，不是让
`ast.literal_eval` 的实现异常越过 Module Interface。输入远低于既有 8 MiB body
停止线，因此不能依赖 transport byte budget 关闭。

修复应以有限 literal AST 结构检查或等价防护确保深度工作有界，并把拒绝结果稳定
映射为 `AnnotationSchemaError`；新增 default 与 `Literal` member 的深一元回归。
只在最外层补一个宽泛 `except Exception` 会掩盖实现错误，不是合适的异常隔离。

### M-03：`Literal` 唯一性校验是可观测的 O(n²)

**Disposition：`blocking-open`**

`annotation_schema.py:138-149` 对每个新成员扫描整个 `normalized` 列表；
随后 `parse_input_contract()` 又进入
`unilabos/workflow/schema.py:255-266` 的同形扫描。候选因此在同一个 public parse
调用中执行两轮随 enum 长度平方增长的比较。

只读基准使用唯一整数成员，排除了提前发现 duplicate 的影响：

| 成员数 | annotation 字符数 | `parse_parameter_annotation` |
|---:|---:|---:|
| 250 | 898 | 0.0056 s |
| 500 | 1,898 | 0.0176 s |
| 1,000 | 3,898 | 0.0614 s |
| 2,000 | 8,898 | 0.2293 s |
| 4,000 | 18,898 | 0.8780 s |

成员翻倍时耗时接近四倍；约 19 KB 的本轮 Interface 输入已经接近一秒。
D-091 与设计 `116-124` 要求非空、唯一、保序，但没有冻结 enum 数量上限，因此不能
未经新决策直接加一个较小 cap 来绕过复杂度问题。

应使用严格 scalar family 对应的可哈希 seen key 在线性时间判重并保持原输出顺序，
同时消除或优化 canonical parser 的第二轮二次扫描。测试至少应有足以区分线性与
二次实现的宽 enum 守护；不要求脆弱的墙钟阈值。

### S-01：本轮新增构造器缺少返回类型标注

**Disposition：`blocking-open`**

`AGENTS.md` 的 Code Conventions 明确写明“Python 3.11+, type hints expected”。
本轮新增的两个构造器都缺少 `-> None`：

- `unilabos/registry/annotation_schema.py:33`
  `AnnotationSchemaError.__init__`；
- `unilabos/registry/annotations.py:18`
  `AllowedResourceTemplates.__init__`。

参数已有类型不等于完整函数类型；这两处是候选新增行，且当前 Ruff 配置不会替代
该仓库规则检查。应补齐返回类型。其余新增 production 函数、简体中文注释及
docstring 符合仓库 Standards。

## 3. 深模块与 Fowler smell 评估

### 630 行 Module 是否冗余

**Disposition：`rejected-with-evidence`**

外部 Interface 仍集中为：

```text
parse_parameter_annotation(...) -> ParsedParameter
render_parameter_annotation(ParsedParameter) -> ast.expr
```

一个 parser 调用隐藏有限类型、nullable、Literal、Field、presentation、
ResourceTemplate identity、default canonicalization 和稳定诊断；一个 render 调用
隐藏全部逆向 AST 细节。删除该 Module 会迫使 Workflow compiler 与 Action
Registry 各自重建同一语法和验证，因此 deletion test 成立，630 行
Implementation 没有把等量复杂度推给 caller。

`annotations.py` 虽然很小，但它承担“作者源码可真实 import 的 typing/metadata
词汇”这一不同 Interface；把运行时 metadata carrier 塞进 AST parser 不会让
caller 更简单。当前两个文件的 seam 合理。

### Parser/render 分派与未来扩展

**Disposition：`rejected-with-evidence`（除 M-03）**

AST → canonical 与 canonical → AST 是两个方向不同的 Implementation，不是两个
caller 各自猜类型。D-082～D-092 把 v1 词汇冻结；未来 v2 应经版本决策扩展，而不是
为了假想类型提前引入 codec class hierarchy。当前没有
Repeated Switches、Divergent Change 或 Speculative Generality blocking。

唯一实质重复是 M-03 中同一 enum 唯一性规则的两次二次扫描；它因可测 CPU 增长而
升级为安全 finding，而不是仅凭 Fowler 名称升级。

其余 smell baseline 结果：

- 名称能表达 AST、schema、metadata 和 render 角色，无 Mysterious Name；
- `ResourceTemplateSymbol` 已收拢 identity data clump，无 Primitive Obsession；
- parser 只通过 `WorkflowInputContract` 的小 Interface 取得 canonical 值，无
  Feature Envy 或 Message Chains；
- 没有跨 production 文件散布同一变更，无 Shotgun Surgery；
- `ParsedParameter.to_dict()` 返回独立 canonical descriptor，不是无价值
  Middle Man；
- 没有继承层次，因此无 Refused Bequest。

## 4. `NO_DEFAULT` 与 `annotations.py` runtime 行为

公开 `NO_DEFAULT` 使用 identity 区分缺失与显式 `None`，本身不可变；错误 default
对象会在 `/default` 失败，没有发现 sentinel collision。

`JSONValue` 可真实 import，并形成递归 typing alias；`AllowedResourceTemplates`
使用 frozen/slots dataclass，把 resource function/class 引用保存为 tuple。runtime
helper 不承担 Catalog resolution，允许 Python 对象作为 symbol carrier 与设计
`159-161` 一致。

`AllowedResourceTemplates()` 或传入非资源对象在 Python runtime 可被构造，不单独
判为合同漏洞：AST parser 才是 fail-closed 语义 Authority，并已拒绝空参数、非
imported Name、Call、Attribute、`*args` 和 `**kwargs`。真正越过 Authority 的路径
是 M-01 的 `ParsedParameter` 正常公开构造器。

## 5. 测试边界审计

新增 1126 行测试主要从 `parse_parameter_annotation`、
`render_parameter_annotation`、`ParsedParameter.to_dict()` 和异常类型观察行为，
没有断言 parser 局部变量，符合“Interface 是测试表面”。

已有 127 cases 充分覆盖有限 accepted/rejected matrix、round-trip、presentation、
metadata identity、默认值、原 AST 修改隔离和 import/exec 零调用。测试规模来自
闭合语法矩阵，不构成冗余 blocking。

但以下缺口使三个已复现错误实现仍可全绿：

1. 只测返回对象的赋值不可变，没有测普通构造器能否伪造 Module 状态；
2. unsupported AST 只覆盖浅表达式，没有递归深度异常；
3. Literal 只覆盖小集合，没有复杂度守护。

这些是 M-01～M-03 的关闭测试，不另计 finding。

## 6. Scope 与既有 follow-up

提交顺序保持设计 → 独立 RED 测试 → 测试夹具修正 → production → 趋势报告；
production 只新增 Registry 内两个文件。候选没有修改 FE、Backend、HTTP、Catalog、
SQLite、现有 Registry scanner 或完整 Workflow compiler，满足设计停止线。

合同评审的“未来 import map 必须 module-scope 且 shadow-aware”继续是
`non-blocking-follow-up`。本次没有把它扩大成当前 parser finding，也不要求在
02B1 修复中接入任何未来 caller。

## 7. 门禁证据

本 reviewer 在固定候选实际运行：

```text
/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q \
  tests/registry/test_annotation_schema_v1.py
=> 127 passed in 0.77s

/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q \
  tests/workflow/test_route_body_budget.py \
  tests/workflow/test_json_resource_budget.py \
  tests/workflow/test_schema_codec_hardening.py \
  tests/workflow/test_value_schema_hardening.py \
  tests/workflow/test_value_schema_v1.py
=> 212 passed, 2 warnings in 2.55s

/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q tests/registry
=> 153 passed in 2.78s

/home/changjunhan/.micromamba/envs/unilab/bin/python -m ruff check \
  --select E,F,I --ignore E501 \
  unilabos/registry/annotation_schema.py \
  unilabos/registry/annotations.py \
  tests/registry/test_annotation_schema_v1.py
=> All checks passed

/home/changjunhan/.micromamba/envs/unilab/bin/python -m ruff format --check \
  unilabos/registry/annotation_schema.py \
  unilabos/registry/annotations.py \
  tests/registry/test_annotation_schema_v1.py
=> 3 files already formatted

git diff --check ca6083b...097d0df
=> passed
```

趋势报告在同一 production/test 候选登记正式完整测试
`1183 passed, 3 skipped, 19 warnings`。自动门禁全绿不覆盖 M-01～M-03 的公开
Interface 复现，也不关闭 S-01。

## 8. 合并门禁

固定候选 `097d0df26d4555be42bde0889153e5596d83f2dd` 当前为 **4 blocking、
1 non-blocking**，不允许进入顺序评审 3/3 或合并。

关闭条件：

1. 让 `ParsedParameter` 构造保持单参数 canonical contract 与 metadata 不变量；
2. 深/异常 literal 统一稳定失败，不泄漏 `RecursionError`；
3. Literal 判重及 canonical 复核不再随成员数平方增长；
4. 补齐两个新增 `__init__` 的 `-> None`；
5. 由独立测试作者补相应边界测试，不弱化既有 127 cases；
6. 在新 production/test SHA 上重跑目标、02A、Registry、Workflow、正式全量、
   Ruff、format 和 diff 门禁；
7. 三名 reviewer 针对新 SHA 重新顺序确认。

以上修复不要求接未来 caller、FE 或 Backend，也不改变 D-082～D-092/D-100 的有限
类型合同。
