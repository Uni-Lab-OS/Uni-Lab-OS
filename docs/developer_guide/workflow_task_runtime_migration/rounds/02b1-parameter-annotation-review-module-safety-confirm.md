# Round 02B1：Parameter Annotation 模块安全修复确认

日期：2026-07-31

评审分支：`review/02b1-module-safety-confirm`

基线：`ca6083badf9ac7db299b30c4f2999f1f32f6a445`

固定 production/test 候选：`a75e8fe113497d018cdce5c4da692a544f09667d`

旧模块安全报告：
`0c68b21a19ea20a582bcf940a7f1d53a00240ade`

参考合同确认报告：
`803c79e`

评审角色：新 production/test SHA 的顺序独立复核 2/3。Reviewer 未参与修复或
独立安全测试编写；本报告只新增评审文档，不修改 production、测试、前端或
Backend，也没有启动其他 subagent。

## 1. 结论

**Blocking 数为 0；M-01、M-02、M-03 和 S-01 均为 `accepted-fixed`，固定候选
可以进入顺序复核 3/3。**

| 评审轴 | Blocking | Non-blocking | 结论 |
|---|---:|---:|---|
| Module / safety | 0 | 1 | 通过 |
| Repository Standards | 0 | 0 | 通过 |

唯一 non-blocking 仍是合同评审已登记的未来 import map 作用域与名称遮蔽守护。
本轮没有生产 caller 接线，因此不扩大该 finding。

## 2. M-01：parser-only Authority

**Disposition：`accepted-fixed`**

`unilabos/registry/annotation_schema.py:49-76` 现在使用：

- `@dataclass(frozen=True, slots=True, init=False)`；
- 普通 `__new__` 无条件抛简体中文 `TypeError`；
- 模块私有 `_PARSED_PARAMETER_TOKEN`；
- 只供 parser 调用的 `_from_canonical()`。

`parse_parameter_annotation()` 仍先通过 `parse_input_contract()` 得到唯一
canonical contract，再使用模块 token 构造结果。独立复跑旧 finding 的普通构造：

```text
空 contract                              => TypeError
string contract + ResourceTemplateSymbol => TypeError
string contract + object metadata         => TypeError
私有 factory + 错误 token                => TypeError
```

这四条均在进入 `to_dict()` 或 render 前关闭，旧 `IndexError`、非法
round-trip AST 和 `AttributeError` 不再可达。只有显式访问模块私有 token 或使用
`object.__new__`/`object.__setattr__` 才能绕过；这不属于普通 Module Interface，
与仓库既有 canonical value 的 parser-only 模式一致。

合法 parser 值的行为独立复核为：

```text
is_dataclass=True
两个等价 parse：相等且 hash 相等
frozen assignment：FrozenInstanceError
to_dict：返回 canonical 独立 descriptor
render：Literal[2.5, 1, -0.0]
```

`repr`、slots 字段和 resource symbol tuple 也保持正常。`copy`、`deepcopy` 与
`dataclasses.replace` 会被 parser-only 构造策略拒绝；设计没有把这些 mutation-like
重建路径列为 Interface，拒绝它们正是保持唯一 Authority，而不是合法值回归。

## 3. M-02：literal 异常隔离

**Disposition：`accepted-fixed`**

`annotation_schema.py:123-127` 只在 `ast.literal_eval()` seam 捕获：

```python
(RecursionError, TypeError, ValueError)
```

随后复用 `_fail(path)`。没有 `except Exception` 或裸 `except`，parser、canonical
validator 和 render 的其他实现错误不会被伪装成输入诊断。

使用 2,500 层 singleton AST 分别放在 default 和 `Literal` member，连续调用两次：

```text
default：
  invalid_annotation /default 参数注解不符合 Workflow 版本 1 合同

Literal member：
  invalid_annotation /annotation 参数注解不符合 Workflow 版本 1 合同
```

两次 code/path/message 完全相同。把 `ast.literal_eval` 临时替换为
`RuntimeError("sentinel")` 时，`RuntimeError` 原样越过该 seam，证明修复没有宽泛
吞错。

旧报告把 5,000 个一元负号描述成“可由 `ast.parse` 正常解析”并不准确：本次使用
同一解释器重跑时，CPython 在构造 AST 阶段已经抛 `RecursionError`，尚未进入本
Module。该事实不应冒充 M-02 修复证据。M-02 的真实 public Interface 复现是直接
传入深 `ast.expr`；本次手工深 AST 与新增测试准确覆盖该 seam，修复仍然成立。

## 4. M-03：enum 复杂度与严格语义

**Disposition：`accepted-fixed`**

Annotation 层和 canonical Workflow Schema 层都改为：

```text
seen set 负责判重
normalized list 负责保留声明顺序
```

进入 set 前仍先完成严格 family/type/finite normalization，因此没有把 Python
`bool`/`int` 相等关系误当成合同兼容：

- `Literal[True, 1]` 仍因混族拒绝；
- `Literal[1, 1.0]` 仍按 number 数值等价拒绝；
- `Literal[-0.0, 0]` 仍按 number 数值等价拒绝；
- Workflow integer 在判重前把合法 integral float 规范化为 int；
- bool、NaN 和 infinity 在进入 set 前拒绝；
- 合法 enum 输出仍来自 list，顺序不变。

使用与旧报告相同的 parse Interface 和更强的三次取最小值基准：

| 成员数 | annotation 字符数 | 修复前旧报告 | 本次 |
|---:|---:|---:|---:|
| 250 | 898 | 0.0056 s | 0.0020 s |
| 500 | 1,898 | 0.0176 s | 0.0037 s |
| 1,000 | 3,898 | 0.0614 s | 0.0074 s |
| 2,000 | 8,898 | 0.2293 s | 0.0146 s |
| 4,000 | 18,898 | 0.8780 s | 0.0295 s |

1,000 → 4,000 成员耗时增长 `4.01x`，不再接近二次实现的 `16x`。单独复核
`parse_input_contract()` 的第二层为 `0.0063 s → 0.0253 s`，增长 `4.03x`；
因此不是只优化外层后仍把 O(n²) 留在 canonical parser。

没有新增 enum 数量 cap，也没有改变 D-091 的非空、唯一、严格类型和保序合同。

## 5. S-01 与 Standards

**Disposition：`accepted-fixed`**

以下两个新增构造器均已显式标注 `-> None`：

- `AnnotationSchemaError.__init__`；
- `AllowedResourceTemplates.__init__`。

新增/修改 production 的参数和返回类型完整，注释、docstring 与运行时错误信息使用
简体中文。修复没有触碰 FE、Backend、HTTP、Catalog、SQLite 或未来 Registry/
Compiler caller，继续满足 round scope gate。

## 6. 深模块与 Fowler smell 复核

修复没有扩大外部 Interface；token、异常隔离和线性判重均隐藏在原
`parse_parameter_annotation()` / `render_parameter_annotation()` Implementation
内。删除该 Module 仍会让 Workflow compiler 与 Action Registry 重复有限语法、
canonical validation 和确定性 render，deletion test 继续成立。

parser-only factory 是为关闭真实伪造路径新增的内部 seam，不是
Speculative Generality。两层 seen-set 位于 AST→canonical 和 canonical validator
两个不同职责点，使用相同严格语义但没有引入新的公共 class hierarchy。

重新应用 Fowler baseline 后：

- 命名准确，无 Mysterious Name；
- `ResourceTemplateSymbol` 继续收拢 identity，无 Data Clumps；
- 没有 Feature Envy、Message Chains 或 Refused Bequest；
- 没有把一个修复散到无关 caller，无 Shotgun Surgery；
- v1 parser/render 的双向分派仍由冻结合同要求，不构成当前
  Repeated Switches、Divergent Change 或 Speculative Generality finding；
- canonical validator 的第二层判重不可删除，否则直接 schema caller 会失去
  Authority，因此不是无价值重复。

没有新的 Module 或 Standards finding。

## 7. 新增安全测试是否过拟合

新增 251 行文件产生 12 个 cases：

- 6 个普通构造伪造形态；
- default/Literal 两个深 AST 位置；
- 宽 unique enum 与宽 duplicate enum；
- 2 个构造器返回标注。

测试全部从公开 parse/构造/descriptor/异常 Interface 观察行为，没有断言
`seen`、token 值或 parser 局部变量。性能测试同时断言完整逆序 enum 未被重排，并用
宽松的四倍输入不超过八倍工作量加固定噪声余量；旧 O(n²) 实测会失败，新线性实现
稳定通过。它守护的是复杂度 Interface，不是某个 set 写法。

深 AST 测试手工构造输入以确保进入本 Module，而不是被 CPython parser 提前拒绝；
这与 `parse_parameter_annotation(annotation: ast.expr, ...)` 的公开形状一致。

251 行主要来自复现矩阵、稳定 error helper 和性能噪声控制，没有删除、skip、xfail
或弱化原 127 cases。测试量与四项旧 blocking 的边界相称，不构成测试内部结构
过拟合。

## 8. 门禁证据

本 reviewer 在固定候选实际运行：

```text
/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q \
  tests/registry/test_annotation_schema_v1.py \
  tests/registry/test_annotation_schema_safety_regressions.py
=> 139 passed in 0.92s

/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q \
  tests/workflow/test_route_body_budget.py \
  tests/workflow/test_json_resource_budget.py \
  tests/workflow/test_schema_codec_hardening.py \
  tests/workflow/test_value_schema_hardening.py \
  tests/workflow/test_value_schema_v1.py
=> 212 passed, 2 warnings in 2.58s

/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q tests/registry
=> 165 passed in 2.79s

/home/changjunhan/.micromamba/envs/unilab/bin/python -m ruff check \
  --select E,F,I --ignore E501 \
  unilabos/registry/annotation_schema.py \
  unilabos/registry/annotations.py \
  unilabos/workflow/schema.py \
  tests/registry/test_annotation_schema_v1.py \
  tests/registry/test_annotation_schema_safety_regressions.py
=> All checks passed

/home/changjunhan/.micromamba/envs/unilab/bin/python -m ruff format --check \
  <上述 5 个 production/test 文件>
=> 5 files already formatted

git diff --check ca6083b...a75e8fe
=> passed

git diff --check 097d0df...a75e8fe
=> passed
```

主执行者已在同一固定候选登记正式完整测试：

```text
1195 passed, 3 skipped, 19 warnings
```

该结果仅作为主执行者门禁证据引用，本 reviewer 没有冒充重复执行。

## 9. 下一步

固定候选 `a75e8fe113497d018cdce5c4da692a544f09667d` 当前为 **0 blocking、
1 non-blocking**。允许进入顺序独立复核 3/3；只有第三名 reviewer 针对同一
production/test SHA 也通过后，02B1 才可进入 integration 合并候选。

任何 production 或测试修改都会产生新 SHA，并使本确认失效。
