# Phase 02A2：Schema codec hardening 第一轮合同复审

日期：2026-07-31

评审分支：`review/02a2-schema-contract`

修复基线：`d340b1942d8f34c672a23f88180b4cff485a7f1c`

固定候选：`04d74adf4e1dff08a05005815674ca7c4532f0ab`

评审范围：重点复审 B-01、B-04；对已关闭的 B-02、B-03 做回归确认。本报告只
修改评审文档，不修改 production、测试、Backend 或前端。

## 1. 结论

**第一轮合同复审通过，可以进入第二名模块/安全 reviewer。**

| 评审轴 | Blocking | Non-blocking | 结论 |
|---|---:|---:|---|
| Standards | 0 | 0 | 通过 |
| Spec | 0 | 0 | 通过 |

B-01、B-04 均已按上一轮最小修复边界关闭；B-02、B-03 回归保持通过。未发现
新的合同、范围或异常稳定性 finding。第二名 reviewer 仍需独立检查整个
02A/02A1/02A2 候选的模块边界、资源上限与安全风险，本报告不替代该门禁。

## 2. Standards

### Blocking findings：0

### Non-blocking findings：0

证据：

- canonical value 删除防护只增加与 `__setattr__` 对称的 `__delattr__`，没有引入
  第二套状态或额外公共 Interface；
- 大整数实现位于现有公共 `unilabos.workflow.json_codec`，Schema、持久 JSON 和
  后续 HTTP 边界继续复用同一编解码语义，没有在 Schema 内复制 codec；
- 固定 9 位 chunk 保证每次 `str`/`int` 只处理小整数或短 token，不调用
  `sys.set_int_max_str_digits`，没有进程级并发副作用；
- bool 分支仍先于 int，float finite 规则、JSON number regex、key order、深度和
  closed-value 规则未改变；
- 新增注释/docstring 为简体中文，类型标注完整；
- 本轮 Ruff `E/F/I`、format check 和候选 diff check 全部通过。

## 3. Finding disposition

### B-01：canonical typed value object 可被普通属性操作破坏

**Disposition：accepted-fixed**

`_CanonicalValue` 现在同时拒绝普通属性赋值和删除
（`unilabos/workflow/schema.py:33-45`）。

独立对抗复验对 `WorkflowValueSchema`、`WorkflowInputContract` 和
`WorkflowOutputContract` 分别执行：

```text
set value._payload
set value.other
del value._payload
del value.other
```

四种操作均稳定抛 `AttributeError`。每次失败后：

- `to_dict()` 仍返回原 canonical 数据；
- 对象仍与同源对象相等且 hash 相同；
- `WorkflowValueSchema` 仍可用于 `normalize_value`；
- 原 162 case 中的公开直构拒绝、无可变容器暴露和独立 dump 继续通过。

设计明确排除的 `object.__setattr__`、`object.__delattr__` 和模块私有构造 token
不在公共 Interface 防护范围内；本实现没有越过该停止线。

### B-04：canonical codec 对合法大整数泄漏裸 `ValueError`

**Disposition：accepted-fixed**

codec 现在用固定 9 位十进制 chunk 迭代转换：

- decode 逐块执行短 `int(chunk)` 并按 `10**9` 累积
  （`unilabos/workflow/json_codec.py:15-33`）；
- encode 以 `divmod(10**9)` 拆块，只格式化不超过 9 位的小整数
  （`:36-51`）；
- 公共 encode/decode 分支统一调用这两个 helper（`:127-138`、`:210-229`）。

独立复验覆盖：

- `0`、`±1`、`10**9` 前后边界、`±10**18`；
- 当前解释器 4300 位限制的 4300/4301 位正负整数；
- 5001 位正负整数；
- 由 600 个不同 chunk 组成的 5400 位正负整数，避免测试只覆盖
  `1` 后全零的特殊形状；
- 大整数嵌套于 object/list；
- integer schema 的正负 `minimum`/`maximum`、边界接受和越界拒绝；
- 正负 Input default 的 parse/dump；
- integer/number `normalize_value` 保持精确 `int` 类型和值。

所有 round-trip 均保持标准十进制 JSON number token，不添加引号、`+`、前导零或
其他非标准包装。测试前后
`sys.get_int_max_str_digits()` 均为 `4300`，没有全局状态变化。

### B-02：嵌套 nullable grammar

**Disposition：accepted-fixed（回归确认）**

新增测试与原 162 case 全部通过；独立抽查双层 nullable 仍稳定返回
`invalid_schema` 和 `/anyOf/0/anyOf`，本轮没有修改 nullable grammar。

### B-03：深层 opaque JSON default 的递归异常

**Disposition：accepted-fixed（回归确认）**

原深层 hardening case 全部通过；独立抽查深度 1200 的 object default 可解析并
重复独立 dump。本轮整数 codec 没有改变 container stack、深度上限或 canonical
bytes ownership。

## 4. 数字 token 回归

以下既有语义经独立对抗复验保持：

| 输入 | 编码/解码结果 |
|---|---|
| `True` / `False` | `true` / `false`，类型仍为 bool |
| `7` / `-7` | `7` / `-7`，类型仍为 int |
| `1.5` | `1.5`，类型仍为 float |
| `-0` token | 解码为整数 `0` |
| `00`、`01`、`-01`、`+1` | 拒绝为非法 JSON |

因此 chunk helper 没有打破 bool/int 分离、small-int、float 或标准 JSON token
边界。

## 5. 测试与命令证据

已运行：

```text
/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q \
  tests/workflow/test_schema_codec_hardening.py \
  tests/workflow/test_value_schema_hardening.py \
  tests/workflow/test_value_schema_v1.py
=> 173 passed, 2 warnings

/home/changjunhan/.micromamba/envs/unilab/bin/python -m ruff check \
  --select E,F,I --ignore E501 \
  unilabos/workflow/json_codec.py \
  unilabos/workflow/schema.py \
  tests/workflow/test_schema_codec_hardening.py \
  tests/workflow/test_value_schema_hardening.py \
  tests/workflow/test_value_schema_v1.py
=> All checks passed

/home/changjunhan/.micromamba/envs/unilab/bin/python -m ruff format --check \
  unilabos/workflow/json_codec.py \
  unilabos/workflow/schema.py \
  tests/workflow/test_schema_codec_hardening.py \
  tests/workflow/test_value_schema_hardening.py \
  tests/workflow/test_value_schema_v1.py
=> 5 files already formatted

git diff --check d340b19...04d74ad
=> passed
```

另运行两个只读 Python 对抗 snippet，覆盖三类对象 set/del 后完整性、chunk/解释器
位数边界、混合 5400 位 token、schema/default/normalize、标准数字 token 及
B-02/B-03 回归。候选趋势报告已记录 Workflow 累积 `605 passed` 和正式测试
`1017 passed, 3 skipped`；本复审没有修改任何 production 或测试文件。

## 6. 下一门禁

| Finding | 最终 disposition |
|---|---|
| B-01 canonical value 可破坏 | `accepted-fixed` |
| B-02 嵌套 nullable | `accepted-fixed` |
| B-03 深层 JSON 递归异常 | `accepted-fixed` |
| B-04 合法大整数裸异常 | `accepted-fixed` |

第一轮合同 finding 已清零，允许启动第二名独立 reviewer。只有第二轮也无 blocking、
且最终固定 SHA 的完整门禁继续全绿，02A 才可合并 integration。
