# Phase 02A2：canonical deletion 与 JSON 大整数 hardening 设计

日期：2026-07-31

状态：**修复候选已通过测试，等待合同 reviewer 复审。**

分支：`migration/02a2-schema-codec-hardening`

基线：`d340b19`

## 1. 修复范围

本轮只关闭 02A1 复审中的两个 blocking：

1. 普通 `del value._payload` 可以删除 canonical payload；
2. Python 3.11 的整数十进制转换保护使合同允许的 5001 位 JSON integer 在
   canonical codec 中泄漏裸 `ValueError`。

B-02 双层 nullable 与 B-03 深层 JSON 已由 reviewer 确认关闭，本轮不再改变其
grammar 或深度规则。不新增 schema 类型、数值上限、合同字段、HTTP 路由、持久
状态或 Authority；不修改 Backend 或前端。

## 2. Canonical value 删除防护

`_CanonicalValue` 对普通属性删除采用与属性赋值相同的封锁规则：

- 三种公开 typed value object 的 payload 和任意普通属性都不能删除；
- 删除尝试稳定抛 `AttributeError`；
- 失败后对象仍可 `to_dict()`、比较和用于 `normalize_value`；
- 继续不要求防御明确排除的 `object.__delattr__` 等主动绕过。

## 3. JSON integer codec

D-083 没有定义整数位数或绝对值上限，因此现有 `json_codec` 必须在不修改解释器
全局状态的前提下支持任意 Python `int`：

- 编码正数、负数和零时，不直接对大整数调用 `str()`；
- 解码整数 token 时，不直接对超长 token 调用 `int(raw)`；
- 使用固定小十进制 chunk 迭代转换，每个 chunk 始终低于解释器转换限制；
- 保持标准十进制 JSON 表示，不引入字符串包装或非标准 token；
- 现有 bool/int 区分、finite float、key order、depth 和错误规则不变；
- 不调用 `sys.set_int_max_str_digits`，避免进程级并发副作用。

这个修复位于公共 `unilabos.workflow.json_codec`，因为 Schema canonical payload、
持久 JSON 和后续 HTTP 边界必须使用同一编解码语义，不能在 Schema 内复制第二套
codec。

## 4. 测试门

独立测试作者先补充 RED，至少覆盖：

- 三种 value object 删除 `_payload` 均失败且对象仍完好；
- 公共 codec 对正、负 5001 位整数的 encode/decode round-trip；
- encoded bytes 是标准十进制 JSON，不是字符串；
- schema `minimum`/`maximum` 含大整数时可 parse/dump；
- Input Contract 大整数 default 可 parse/dump，并与原输入值相等；
- `normalize_value` 对 integer/number 大整数保持严格类型和原值；
- 测试前后解释器 `sys.get_int_max_str_digits()` 不变。

修复后必须通过新增回归、原 162 个 Schema case、Workflow 累积测试、正式
`pytest tests -q`、Ruff `E/F/I`、format 和 `git diff --check`。随后由原第一名
reviewer 复审 B-01/B-04；清零后才启动第二名 reviewer。
