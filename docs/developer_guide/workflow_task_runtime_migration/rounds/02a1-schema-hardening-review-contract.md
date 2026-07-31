# Phase 02A1：Schema canonical value hardening 第一轮复审

日期：2026-07-31

评审分支：`review/02a1-schema-contract`

原始集成基线：`e85a60c1acec53cf8d6e2643e40a7ba0c12cd36f`

本轮修复基线：`6cb6e27df20d9db33a7637e36030a08a3fd8b65e`

固定候选：`3ba51eb8af99408f944de67356d58b08e439ee96`

评审范围：逐项复审原报告 B-01、B-02、B-03，并检查修复引入的异常稳定性。
本报告只修改评审文档，不修改 production、测试、Backend 或前端。

## 1. 结论

**本候选仍不可进入第二名 reviewer。**

| 评审轴 | Blocking | Non-blocking | 结论 |
|---|---:|---:|---|
| Standards | 0 | 0 | 通过 |
| Spec | 2 | 0 | 不通过 |

原三个 finding 中，B-02、B-03 已按证据关闭；B-01 只完成公开构造和属性赋值
防护，普通属性删除仍可销毁 canonical payload。此外，新的 bytes ownership 实现
对 D-083 允许的合法大整数泄漏裸 `ValueError`，记为 B-04。两项均需在新候选中
关闭并复审，不能把当前趋势报告中的“0（待复审确认）”当作门禁通过。

## 2. Standards

### Blocking findings：0

### Non-blocking findings：0

证据：

- 修复仍局限在纯内存 Schema 深模块，没有增加 SQLite、Catalog、Material、HTTP、
  Backend 或前端依赖；
- 新增注释、docstring 和异常消息为简体中文，Python 3.11 类型标注完整；
- nullable grammar 的单一 `allow_nullable` 开关把策略集中在 parser seam；
- canonical bytes 方案移除了递归 `deepcopy`，没有把复制逻辑散布到 caller；
- 本轮 production/测试 Ruff `E/F/I`、format check 和候选 diff check 均通过。

## 3. 原 finding disposition

### B-01：typed value object 不变量可绕过

**Disposition：blocking-open（部分修复）**

已确认修复：

- 三个公开 value object 直接构造均稳定抛 `TypeError`；
- `_payload` 是不可原地修改的 `bytes`，对象不暴露 `dict`、`list` 或 `set`
  canonical 容器；
- `setattr(value, "_payload", ...)` 和新增普通属性均抛 `AttributeError`；
- `to_dict()` 每次返回独立容器。

未关闭点：

`_CanonicalValue` 只覆盖 `__setattr__`，没有覆盖 `__delattr__`
（`unilabos/workflow/schema.py:33-42`）。因此以下普通 Python 属性操作仍成功：

```python
schema = parse_value_schema({"type": "string"})
del schema._payload
schema.to_dict()  # AttributeError
```

原 B-01 就是通过公开对象的 underscore payload 破坏不变量；把 `_data` 换成
`_payload` 不能改变同一评审边界。删除 payload 后，对象不再是不可变、始终有效的
typed value object，并在 `to_dict()` 泄漏裸 `AttributeError`。

最小修复边界：像 `__setattr__` 一样覆盖 `__delattr__` 并始终拒绝普通属性删除；
对三种 value object 增加删除实际 payload 后对象仍完好的回归测试。不要求防御设计
已明确排除的 `object.__setattr__` 或模块私有构造 token。

### B-02：nullable 可以再次包裹 nullable

**Disposition：accepted-fixed**

实现用独立 `allow_nullable` 参数控制 grammar；nullable 的 base member 和 array
item 均以 `allow_nullable=False` 解析
（`unilabos/workflow/schema.py:276-313`、`:394-402`、`:428-451`）。

独立对抗复验结果：

| 场景 | code | path |
|---|---|---|
| standalone | `invalid_schema` | `/anyOf/0/anyOf` |
| standalone，外层 null 在前 | `invalid_schema` | `/anyOf/1/anyOf` |
| Input Contract | `invalid_schema` | `/parameters/0/schema/anyOf/0/anyOf` |
| Output Contract | `invalid_schema` | `/outputs/0/schema/anyOf/0/anyOf` |

三条要求路径和反转 member 顺序均稳定，未发现 grammar 绕过。

### B-03：深层 opaque JSON default 泄漏递归异常

**Disposition：accepted-fixed**

canonical value 现在以 `encode_json` 的 immutable bytes 持有，每次通过
`decode_json_bytes` 导出独立容器
（`unilabos/workflow/schema.py:50-71`）；Input default 不再调用 `deepcopy`
（`:753-780`）。

独立对抗复验确认：

- 深度 1200 的合法 opaque JSON default 可解析并重复 dump，原始输入和两次 dump
  均不共享容器；
- 恰好 `MAX_BACKEND_JSON_DEPTH=10000` 的独立 opaque value 可规范化；
- 深度 10001 的独立 value 稳定为 `invalid_value`，Input default 稳定为带完整
  `/parameters/0/default/...` 前缀的 `invalid_contract`；
- cycle 和非 JSON 子值分别在 `/self`、`/x` 返回 `invalid_value`；
- 上述路径未泄漏 `RecursionError`、`KeyError`、`AssertionError` 或
  `ValueError`。

原 B-03 的深度、归属、独立 dump 和超限异常条件已全部满足。

## 4. 新 finding

### B-04：canonical bytes 编码对合法大整数泄漏裸 `ValueError`

**级别：blocking-new**

D-083 明确规定 integer 接受无小数部分的 JSON number，number 接受 finite JSON
integer 或 fractional number（`decisions.md:2291-2317`）；v1 没有定义整数位数或
数值绝对值上限。Python `int` 本身也是有限整数。

新实现的 `_from_canonical` 无条件调用 `encode_json(data)`
（`unilabos/workflow/schema.py:50-61`），而 codec 用 `str(item)` 编码整数
（`unilabos/workflow/json_codec.py:188-193`）。Python 3.11 默认限制整数与十进制
字符串的转换位数，因此合同允许的 5001 位整数出现以下结果：

```text
parse_value_schema({"type": "integer", "minimum": 10**5000})
=> ValueError

parse_input_contract(... integer default = 10**5000 ...)
=> ValueError
```

同一数值已通过 strict integer 和 constraint/default 校验，失败发生在新增的
canonical ownership 编码阶段，且没有稳定 `WorkflowSchemaError` code/path。
这会让 Authoring default、schema constraint 与 standalone value 对同一 D-083
整数产生不一致行为。

最小修复边界：让 canonical 编码完整支持当前冻结合同允许的整数，并补
standalone schema constraint、Input default 和 dump 回归；若工程上必须限制整数
位数，应先新增明确的产品决策和统一上限，再以稳定 `invalid_schema` /
`invalid_contract` 拒绝，不能依赖解释器的裸异常。

## 5. 测试与命令证据

已运行：

```text
/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q \
  tests/workflow/test_value_schema_hardening.py \
  tests/workflow/test_value_schema_v1.py
=> 162 passed, 2 warnings

/home/changjunhan/.micromamba/envs/unilab/bin/python -m ruff check \
  --select E,F,I --ignore E501 \
  unilabos/workflow/schema.py \
  tests/workflow/test_value_schema_hardening.py \
  tests/workflow/test_value_schema_v1.py
=> All checks passed

/home/changjunhan/.micromamba/envs/unilab/bin/python -m ruff format --check \
  unilabos/workflow/schema.py \
  tests/workflow/test_value_schema_hardening.py \
  tests/workflow/test_value_schema_v1.py
=> 3 files already formatted

git diff --check 6cb6e27...3ba51eb
=> passed
```

另以只读 Python snippet 复验公开直构、普通赋值/删除、四种双层 nullable、深度
1200/10000/10001、cycle、非 JSON 子值和 5001 位整数。没有修改 production 或
测试。候选趋势报告已记录 Workflow 累积 `594 passed` 和正式测试
`1006 passed, 3 skipped`；本复审不重复全量门禁，因为两个 blocking 已由确定性
最小样例确认。

## 6. 下一门禁

| Finding | Disposition | 新候选前置条件 |
|---|---|---|
| B-01 canonical value 可破坏 | `blocking-open` | 封锁普通属性删除并补三类型回归 |
| B-02 嵌套 nullable | `accepted-fixed` | 无 |
| B-03 深层 JSON 递归异常 | `accepted-fixed` | 无 |
| B-04 合法大整数裸异常 | `blocking-new` | 支持冻结整数合同或先冻结统一限制；补稳定错误/接受测试 |

B-01、B-04 全部修复、相关新测试先 RED 后 GREEN、完整门禁在新 SHA 通过并由本
reviewer 复审后，才能启动第二名模块/安全 reviewer。
