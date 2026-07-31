# Phase 02A：Workflow v1 Contract 与严格值 Schema 设计

状态：**设计冻结，等待独立测试先行。**

分支：`migration/02a-schema-v1`

基线：`e85a60c`

## 1. 目标与边界

本轮新增纯内存深模块 `unilabos.workflow.schema`，统一实现 D-082～D-087 的
Contract 结构和严格值规则，为后续 annotation parser、Authoring compiler、
Task input preflight 和前端表单提供同一个事实来源。

本轮不访问 SQLite、Template Catalog、Material Authority 或设备，不新增 HTTP
路由，也不修改 Backend 或前端。ResourceSlot 只验证外部引用
`{"uuid": "..."}` 的闭合结构和 schema allowlist；实际 Material 查找、状态和
template 匹配由后续 Authority Adapter 负责。P0-5 关闭前不定义最终
WorkflowTask ResourceSlot output 的外部序列化。

## 2. 公共 Interface

模块只暴露以下主操作：

```python
parse_value_schema(raw) -> WorkflowValueSchema
parse_input_contract(raw) -> WorkflowInputContract
parse_output_contract(raw) -> WorkflowOutputContract
normalize_value(schema, raw_value) -> JSONValue
```

返回对象是不可变的 typed value object，并提供 `to_dict()` 生成确定 JSON。失败
抛出 `WorkflowSchemaError`，至少携带稳定 `code`、JSON Pointer `path` 和中文
`message`。调用方可以把它转换成 Compile diagnostic 或 Task input error；本模块
不决定 HTTP status。

## 3. v1 schema 形状

非 null 基础 schema 只允许：

```json
{"type":"string"}
{"type":"integer"}
{"type":"number"}
{"type":"boolean"}
{"type":"object"}
{"$slot":"ResourceSlot"}
{"type":"array","items":{"type":"string"}}
```

数组 `items` 可以是 scalar、opaque object 或 ResourceSlot，但不能是 array 或
nullable schema。nullable 只包裹完整值，并规范化为：

```json
{"anyOf":[{"type":"string"},{"type":"null"}]}
```

允许的约束只有：

- scalar：非空、严格同类型且无重复的 `enum`；
- integer/number：有限的 inclusive `minimum`/`maximum`；
- string：非负整数 `minLength`/`maxLength`；
- array：非负整数 `minItems`/`maxItems`；
- ResourceSlot：非空、唯一、合法 UUID 的
  `allowed_resource_template_uuids`。

lower bound 不得大于 upper bound。所有 schema 对象闭合；未知 key、空 enum、
非有限数字、unsupported type、nested array、nullable item 或矛盾约束均失败。

## 4. 严格值规范化

- string 只接受 `str`；
- boolean 只接受 `bool`；
- integer 接受非 bool 的数学整数，`3.0` 规范化为 `3`；
- number 接受非 bool 的有限 `int|float`；
- object 只接受 string-keyed、递归 JSON-valid `dict`；
- array 只接受 `list`，逐项使用同一个 non-null item schema；
- ResourceSlot 外部值只接受闭合 `{"uuid": "<uuid>"}`，UUID 规范化为小写；
- nullable schema 接受 `None`，non-null constraints 对 null 不执行；
- enum 在基础类型规范化之后判断，避免 `True == 1` 的 Python 相等陷阱；
- 返回值不保留调用方可变容器的共享引用。

ResourceSlot 的 allowlist 在本轮只验证 schema 自身；没有 Material
`resource_template_uuid` 就不猜测匹配结果。

## 5. Input Contract

Input envelope 恰好是：

```json
{"version":1,"parameters":[]}
```

parameter descriptor 只允许 `name`、`schema`、`required`、可选 `default`、
`title` 和 `description`。名称必须是非关键字 Python identifier，按声明顺序唯一。
presentation 字段 trim 后必须非空。

只允许三种声明：

1. `required=true`、non-null schema、没有 default；
2. `required=false`、non-null schema、存在同 schema 的 non-null default；
3. `required=false`、nullable schema、`default=null`。

拒绝 required-nullable、required+default、optional 无 default、non-null+null
default、nullable+non-null default。ResourceSlot 不得嵌入非 null UUID default；
`list[ResourceSlot]` 的 non-null default 只能是空数组。

## 6. Output Contract

Output envelope 恰好是：

```json
{"version":1,"outputs":[]}
```

output descriptor 只允许 `name`、`schema`、可选 `title`、`description` 和
`implicit`。`implicit` 缺省为 false，存在时必须是 bool。输出不得出现
`required` 或 `default`；声明的每个名称都必须由后续 compiler/runtime 显式
解析，nullable 只允许该 key 的值为 null，不允许 key 缺失。

本轮只验证 Output Contract 和独立值，不实现完整 WorkflowTask output map 或
ResourceSlot external projection。

## 7. 测试门与停止线

独立测试作者先以 table-driven 方式覆盖：

- 全部支持类型、nullable 和数组；
- strict bool/int/number、opaque JSON、enum 与边界；
- closed envelope/descriptor/schema；
- 三种合法 Input 声明和所有非法 default/required 组合；
- Output 禁止 default/required；
- ResourceSlot 引用/allowlist 的纯结构；
- 错误 `code/path` 稳定；
- 输入对象不被 mutation，输出容器不共享。

目标测试、Workflow 累积测试、正式 `tests/`、Ruff `E/F/I`、format 和
`git diff --check` 全部通过后，才锁定候选给多个 reviewer 串行评审。任何
Catalog/Material/HTTP/annotation 需求留给后续轮次，不在本模块增加第二个 Authority。
