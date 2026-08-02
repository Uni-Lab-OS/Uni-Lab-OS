# Phase 02A4：Workflow route body budget 不依赖 MIME 设计

日期：2026-07-31

状态：**设计冻结，等待独立回归测试先行。**

分支：`migration/02a4-route-body-budget`

基线：`1111380`

## 1. 修复范围

本轮只关闭最终风险 reviewer 的 B-07：声明 request body 的 Workflow route 不能因
错误或恶意 `Content-Type` 跳过 8 MiB body 读取预算。

不改变 JSON integer/depth 规则，不修改 Schema、codec、Backend 或前端，不给
GET/SSE 等无 request body 的 route 增加读取行为。

## 2. Route gate

`_BackendJSONRoute` 在构建 handler 时读取 FastAPI/Starlette 已解析的
`body_field`：

- `body_field is not None`：不论 MIME，先调用同一个 `_read_limited_body`，并缓存
  `request._body`；
- MIME 是 JSON：再从该缓存 body 调用唯一 decoder，并设置 `request._json`；
- MIME 不是 JSON：不自行解释 body，由 FastAPI 后续按原规则验证，但只能读取已
  受限缓存；
- `body_field is None`：保持候选前行为，不为了资源预算读取 body；现有 JSON MIME
  preload 兼容行为也不在本轮改写。

因此同一 oversized POST/PUT body 不能通过改成 `text/plain`、
`application/octet-stream`、缺少 `Content-Type` 或未知 MIME 绕过预算。

## 3. 错误与停止线

- declared 8 MiB + 1：任意 MIME 都在零 receive 时返回既有
  `400 invalid_input`；
- missing/chunked length：任意 MIME 都在第一个超限 chunk 后停止；
- service 调用和业务副作用均为零；
- exact 8 MiB non-JSON body 可完整缓存，再由 FastAPI 按既有内容类型规则处理；
- JSON exact-limit 成功路径、body/json/stream cache 与 D-101 case 不回归；
- GET/SSE 和没有 body field 的 command route 不进入 limited reader。

NB-01 `Content-Length` 词法偏宽仍记录为 non-blocking：真实 h11 会先执行 HTTP
framing 校验，且无论 declared spelling 如何，实际 stream 上限都不能绕过。本轮不
把 HTTP server parser 复制进业务 route。

## 4. 测试门

独立测试作者先补 RED，至少覆盖：

- `text/plain`、无 `Content-Type` 与未知 MIME 的 declared oversize 零读取；
- 同三类 MIME 的 missing/chunked actual oversize 首个超限 chunk 停止；
- 精确上限 non-JSON body 只读取一次且 FastAPI 可从缓存继续处理；
- JSON 成功/失败和 existing 16 个 D-101 case 不回归；
- GET/SSE 或无 body field route 不因本修复读取 body。

修复后运行新增目标、全部 189 个 Schema/资源 case、Workflow 累积、正式
`pytest tests -q`、Ruff `E/F/I`、format 与 `git diff --check`。由最终风险
reviewer 复审 B-07 后，所有 reviewer 再确认固定候选。
