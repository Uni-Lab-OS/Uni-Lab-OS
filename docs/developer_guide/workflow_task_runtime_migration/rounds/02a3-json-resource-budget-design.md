# Phase 02A3：Workflow JSON 资源预算与完整值深度设计

日期：2026-07-31

状态：**实现候选已通过测试，等待模块/安全 reviewer 复审。**

分支：`migration/02a3-json-resource-budget`

基线：`3e63ecf`

## 1. 修复范围

本轮只关闭第二名 reviewer 的两个 blocking：

1. B-05：不可信 HTTP JSON 可用超长整数同步占用 event loop；
2. B-06：opaque 子树校验没有计入 array/Contract wrapper，parser 可返回无法
   `to_dict()` 的 canonical value。

本轮修改 OS 公共 Workflow HTTP adapter、共享 JSON decoder 参数和 Schema 完整值
校验。不修改 Backend，不新增业务字段、Authority、持久模型或 schema 类型。

## 2. D-101 资源合同

公共 Workflow JSON 请求固定为：

- body 最大 `8 * 1024 * 1024` bytes；
- 单个 integer token 最大 4096 个十进制数字，不计负号；
- nesting 最大 10000，按完整 JSON document 计算。

有合法且超限的 `Content-Length` 时在读取 body 前拒绝。没有长度或使用 chunked
传输时逐 chunk 累计，超过上限立即停止。整数位数在构造 bigint 前检查。失败统一
返回既有 `400 invalid_input` envelope，且 service 调用次数为零。

公共 `decode_json_bytes` 增加可选 integer digit budget；缺省为 `None`，供可信
Store/canonical 路径继续支持任意 Python `int`。不得修改解释器全局整数转换设置，
不得复制第二套 parser。

## 3. 完整值深度

`MAX_BACKEND_JSON_DEPTH` 计量完整 document/value：

- standalone opaque object 的 root 是第一层；
- `list[object]` 的 list root 也占一层；
- Input Contract 的 envelope、parameters list、descriptor 和 default object 都
  占层。

`normalize_value` 在返回前对完整 normalized value 做一次非递归 JSON 校验和复制。
Input/Output Contract 在创建 canonical payload 前对完整 envelope 做一次非递归
校验。opaque object 不再先按脱离上下文的子树预算复制。

超限时：

- `normalize_value` 返回 `invalid_value` 和完整 value pointer；
- Contract 返回 `invalid_contract` 和完整 descriptor/default pointer；
- 不泄漏 `ValueError`、`RecursionError` 或 `AttributeError`；
- 所有 parser 返回对象的 `to_dict()` 必须成功。

## 4. 测试门

独立测试作者先提交 RED，至少覆盖：

- external integer 4096 digits 接受、4097 digits 在 bigint 构造前拒绝；
- 正负 token，可信 decoder 仍 round-trip 5001 digits；
- declared body 恰好 8 MiB 与 8 MiB + 1 的 pre-read 行为；
- chunked/missing length 的增量上限与零 service side effect；
- HTTP 失败使用精确 `400 invalid_input` envelope；
- standalone object depth 10000 接受、10001 拒绝；
- `list[object]` item 临界值计入 list wrapper；
- Input default depth 9997 可 parse/dump，9998 以完整 pointer
  `invalid_contract` 拒绝；
- 深度检查保持非递归，cycle/shared-reference 和原 173 case 不回归。

修复后必须通过新增目标、全部 Schema case、Workflow 累积、正式
`pytest tests -q`、Ruff `E/F/I`、format 与 `git diff --check`。再由第二名
模块/安全 reviewer 复审 B-05/B-06；清零后继续满足剩余独立评审门禁。
