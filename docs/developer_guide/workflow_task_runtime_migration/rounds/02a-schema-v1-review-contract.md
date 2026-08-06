# Phase 02A：Workflow v1 Schema 第一轮独立合同评审

日期：2026-07-31

评审分支：`review/02a-schema-contract`

固定基线：`e85a60c1acec53cf8d6e2643e40a7ba0c12cd36f`

固定候选：`19d47c3473f32190a246b77070e54f8f37ecf2a7`

评审视角：Standards 与 Spec 双轴；本报告只修改评审文档，不修改 production
或测试。

## 1. 结论

**本候选暂不可进入第二轮评审。**

| 评审轴 | Blocking | Non-blocking | 结论 |
|---|---:|---:|---|
| Standards | 0 | 0 | 通过 |
| Spec | 3 | 0 | 不通过 |

已实现的 finite scalar/list/ResourceSlot 类型、约束、Input/Output descriptor、
错误 code/path 主干与冻结决策基本一致，目标测试和 lint 也通过；但测试没有覆盖
三个会破坏严格 v1 事实来源的实现缺口。修复后必须增加对应回归测试、重跑本轮完整
门禁并生成新的固定候选，再由第一轮 reviewer 复审；当前 SHA 不得直接进入第二名
reviewer。

## 2. Standards

### Blocking findings：0

### Non-blocking findings：0

证据：

- `unilabos/workflow/schema.py` 的注释、docstring 和错误消息均为简体中文，符合
  `AGENTS.md:75-79`；
- 新增公开函数、value object 和主要 helper 均有 Python 3.11 类型标注；
- 模块保持纯内存边界，没有 SQLite、HTTP、Material Authority、Template Catalog、
  设备或 Backend/前端依赖；候选也没有修改 Backend 或前端路径；
- 四个主操作形成较小 Interface，合同解析、值规范化和错误投影集中在一个模块，
  未发现需要以 smell finding 阻塞本轮的跨模块散布、Feature Envy 或
  Speculative Generality；
- Ruff `E/F/I`、Ruff format check 和 `git diff --check` 均通过。

## 3. Spec

### B-01：typed value object 并不真正不可变，且公开构造可绕过解析不变量

**级别：blocking**

冻结设计要求返回对象是“不可变的 typed value object”
（`02a-schema-v1-design.md:32-35`），并由四个主操作提供同一个严格事实来源。

实现把可变 `dict` 直接保存在 frozen dataclass 的 `_data`
（`unilabos/workflow/schema.py:34-61`）。`frozen=True` 只禁止属性重新赋值，
调用方仍可执行 `schema._data["type"] = ...`，从而永久改变同一个对象。三个
dataclass 的公开构造器还允许直接创建未经解析的对象，例如
`WorkflowValueSchema({"type": "bytes"})`；随后 `normalize_value` 会抛出
`AssertionError`，而不是稳定的 `WorkflowSchemaError`。

最小复现：

```text
nested_nullable_accepted {'anyOf': [...]}
mutated_schema {'anyOf': [...], 'type': 'number'}
WorkflowValueSchema({'type': 'bytes'}) -> AssertionError
WorkflowValueSchema({}) -> KeyError
```

影响：后续 compiler、Task preflight 和前端表单无法把该对象当作可信、不可变的
共享事实；无效状态还能绕过 `invalid_schema` 的稳定 code/path 合同。修复应让
canonical 数据由对象真正拥有且外部不可变，并阻止或校验绕过 parser 的直接构造。

### B-02：nullable 可以再次包裹 nullable，超出有限 v1 grammar

**级别：blocking**

D-082 规定 nullable 是“wrapper over a supported type”
（`decisions.md:2253-2289`）；本轮设计也只允许列出的 non-null 基础 schema，
并规定 nullable 只包裹完整值后规范化成单层 `anyOf`
（`02a-schema-v1-design.md:37-56`）。

`_parse_nullable` 在解析非 null member 时再次调用
`_parse_schema_dict(..., allow_array=True)`
（`unilabos/workflow/schema.py:248-284`）。这个入口仍允许 `anyOf`，因此下列
双层 nullable 被接受并原样保留：

```python
parse_value_schema(
    {
        "anyOf": [
            {"anyOf": [{"type": "string"}, {"type": "null"}]},
            {"type": "null"},
        ]
    }
)
```

这既不是有限 non-null 基础类型，也不是唯一确定的 normalized nullable 形状。
修复应拒绝 nullable 的 base member 再次为 nullable，并补充 standalone 及
Input/Output Contract 路径前缀测试。

### B-03：合法深层 opaque JSON default 会越过迭代校验后在 `deepcopy` 崩溃

**级别：blocking**

D-082/D-086 要求 opaque object 接受递归有效 JSON
（`decisions.md:2273-2283`、`:2431-2435`），本轮设计同样要求
string-keyed、递归 JSON-valid `dict`，且所有失败由稳定
`WorkflowSchemaError` 表达（`02a-schema-v1-design.md:32-35`、`:70-81`）。

实现已经使用非递归 walker 和 `json_codec` 接受最高
`MAX_BACKEND_JSON_DEPTH=10000` 的 JSON（`unilabos/workflow/schema.py:431-465`），
但 value object 的 `to_dict()` 以及 Input default 归属仍调用递归
`deepcopy`（`:40-61`、`:734-735`）。一个深度 1200、远低于 Backend 上限的合法
对象 default 因而抛出裸 `RecursionError`：

```text
deep_default_error RecursionError maximum recursion depth exceeded while
calling a Python object
```

影响：同一份合法 JSON 在普通 value normalization 可通过，在 Input Contract
default 路径却崩溃；这破坏 D-083/D-085 的统一严格 validator，也绕过稳定
code/path。修复应在 canonical 数据归属和 dump 路径使用有界、非递归复制，并增加
深层 default、dump 独立性和超限错误测试。

## 4. 测试与实现检查

独立合同测试对主干矩阵覆盖充分，但下列现有断言不足以发现上述问题：

- `test_parsed_schema_is_an_immutable_typed_value_object` 只尝试新增属性，没有尝试
  修改 `_data` 或直接构造无效 value object；
- nullable 表覆盖 member 顺序、数量、nullable list item 和闭合 null member，
  没有覆盖 nullable base 自身又是 `anyOf`；
- opaque JSON 覆盖递归内容、cycle、非 JSON 值和副本独立性，没有覆盖远低于
  `MAX_BACKEND_JSON_DEPTH`、但高于 Python 递归上限的合法 default。

已运行命令：

```text
/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q \
  tests/workflow/test_value_schema_v1.py
=> 148 passed, 2 warnings

/home/changjunhan/.micromamba/envs/unilab/bin/python -m ruff check \
  --select E,F,I --ignore E501 \
  unilabos/workflow/schema.py tests/workflow/test_value_schema_v1.py
=> All checks passed

/home/changjunhan/.micromamba/envs/unilab/bin/python -m ruff format --check \
  unilabos/workflow/schema.py tests/workflow/test_value_schema_v1.py
=> 2 files already formatted

git diff --check e85a60c...19d47c3
=> passed
```

另以只读 Python snippet 分别复现 B-01、B-02、B-03；没有修改测试或 production。
本轮无需重复候选已记录的完整 `pytest tests -q`，因为 blocking findings 已由
确定性最小样例确认。

## 5. Finding disposition 与下一门禁

| Finding | 当前 disposition | 进入第二轮前的条件 |
|---|---|---|
| B-01 value object 不变量可绕过 | blocking-open | 真正封装 canonical 数据；补 mutation/非法构造回归 |
| B-02 接受嵌套 nullable | blocking-open | 拒绝双层 nullable；补 standalone/Contract 路径回归 |
| B-03 深层 default `RecursionError` | blocking-open | 非递归归属与 dump；补合法深层/超限回归 |

只有三项全部变为 `accepted-fixed`，目标、Workflow 累积、正式完整测试、Ruff 和
diff check 在新 SHA 全绿，且第一轮复审确认后，才可进入第二轮模块/安全评审。
