# Phase 02A1：Schema canonical value hardening 设计

日期：2026-07-31

状态：**设计冻结，等待独立回归测试先行。**

分支：`migration/02a1-schema-hardening`

基线：`6cb6e27`

## 1. 修复范围

本轮只关闭第一轮合同评审报告中的三个 blocking：

1. `WorkflowValueSchema`、`WorkflowInputContract` 和
   `WorkflowOutputContract` 的 canonical 数据可被外部原地篡改，且公开构造可绕过
   parser；
2. nullable 的 non-null member 还能再次是 nullable；
3. 合法深层 opaque JSON default 在递归 `deepcopy` 中抛出裸
   `RecursionError`。

不新增 schema 类型、合同字段、HTTP 路由、数据库状态或 Authority；不修改
Backend 或前端。

## 2. Canonical value ownership

三个 typed value object 继续是 parser 的返回类型，但改为真正拥有不可变 canonical
payload：

- 外部直接调用类构造器必须失败，不能创建未经 parser 验证的对象；
- 对象不暴露可原地修改的 `dict`/`list`；
- 普通属性赋值和新增属性继续失败；
- `to_dict()` 每次返回独立、可修改但不反向影响对象的 JSON 容器；
- 模块内部需要使用 schema 时，从不可变 payload 得到独立 canonical view；
- canonical payload 的编码和 dump 使用现有非递归 `json_codec`，不依赖
  `deepcopy`。

不要求抵御调用方显式使用 `object.__setattr__`、篡改 Python 进程内存或访问模块私有
构造 token；这些不属于公共 Interface。

## 3. Nullable grammar

`anyOf` 仍只允许恰好一个 supported non-null schema 和一个闭合
`{"type":"null"}`。解析 nullable 的 non-null member 时必须关闭 nullable 能力，
因此拒绝双层或更多层 `anyOf`。

错误保持：

- standalone 双层 nullable：`invalid_schema`，指向内层 `/anyOf/0/anyOf`；
- Input/Output Contract 中保留完整 descriptor 前缀；
- array item 仍在其 `/items/anyOf` 处拒绝 nullable item。

## 4. 深层 JSON

在 `MAX_BACKEND_JSON_DEPTH` 范围内的 string-keyed、无环、递归 JSON-valid opaque
object：

- 可作为独立 value 规范化；
- 可作为 Input Contract 的 non-null default；
- 可通过 `to_dict()` 多次导出；
- 导出结果互不共享，也不与原始输入共享。

超过统一深度上限、含环、含非 JSON 值或非字符串 key 仍抛稳定
`WorkflowSchemaError`，不得泄漏 `RecursionError`、`ValueError`、`KeyError` 或
`AssertionError`。

## 5. 测试门

独立测试作者先补充 RED，至少覆盖：

- 三种 value object 的直接非法构造；
- schema canonical 数据无法经公共属性原地篡改；
- 多次 `to_dict()` 的独立性；
- standalone/Input/Output 三条双层 nullable 路径；
- 深度高于 Python 递归上限但低于 Backend 上限的 object default 和 dump；
- 超过 Backend 上限时的稳定 `invalid_contract` 路径。

修复后必须通过新增回归、原 148 个合同测试、Workflow 累积测试、正式
`pytest tests -q`、Ruff `E/F/I`、format 与 `git diff --check`。然后由原第一名
reviewer 复审三个 finding；全部关闭后，才能启动第二名 reviewer。
