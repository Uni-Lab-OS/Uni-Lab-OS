# Phase 02A4：Workflow route body budget 最终风险复审

日期：2026-07-31

评审分支：`review/02a4-final-risk`

修复基线：`1111380acd9e57be1f56223d9aa74a7ff5cb1304`

固定候选：`edef8003cabf246d89d45340ba94961f63b8a4b4`

评审范围：独立复审 B-07，检查所有声明 body 的 Workflow route 是否在任意 MIME
下执行同一个 8 MiB 停止线，并检查 Request cache、无 body GET/SSE、错误 envelope、
零业务副作用与 NB-01。本报告只新增评审文档，不修改 production、测试、其他文档、
Backend 或前端。

## 1. 结论

**B-07 为 `accepted-fixed`，固定候选可以进入下一名独立 reviewer。**

| 评审轴 | Blocking | Non-blocking | 结论 |
|---|---:|---:|---|
| Regression / abuse / failure isolation | 0 | 1 | 通过 |
| D-101 / B-07 合同 | 0 | 0 | 通过 |

候选把 byte budget 绑定到 FastAPI 已解析的 route shape
`body_field is not None`，不再绑定调用方可伪造的 `Content-Type`。JSON
integer/depth 仍只由唯一 JSON decoder 处理；非 JSON body 只形成受限 `_body`
cache，再交回 FastAPI 做原内容类型校验。

没有发现新的预算绕过、cache 破坏、业务副作用或无 body route 回归。NB-01
`Content-Length` 词法偏宽继续记为 `non-blocking-follow-up`，不阻止下一轮评审。

## 2. B-07 disposition

### B-07：不可信 `Content-Type` 可关闭 8 MiB body budget

**Disposition：`accepted-fixed`**

D-101 现在明确：

- byte budget 属于每一条声明 request body 的 Workflow route；
- 调用方提供的 MIME 不决定是否启用 byte budget；
- JSON integer/depth budget 只在实际 JSON decode 时启用；
- declared oversize 在读取前拒绝；
- missing/chunked length 在第一个超限 chunk 停止；
- 失败使用 `400 invalid_input` 且没有 Workflow、Task、Candidate 或其他业务
  副作用。

实现与该边界一致
（`unilabos/app/workflow_api.py:71-97`）：

```python
expects_body = self.body_field is not None

if expects_body:
    body = await _read_limited_body(request)
    if mime == "application/json" or mime.endswith("+json"):
        request._json = decode_json_bytes(...)
```

`expects_body` 在 route handler 构建时冻结，不需要调用方、Service 或业务 DTO
重复理解预算规则，也没有引入全局 middleware 或第二个 JSON parser。

### 2.1 当前全部 route shape

独立枚举 `create_workflow_app` 中的 `_BackendJSONRoute`：

| route 类型 | 数量 | `body_field` | 结果 |
|---|---:|---|---|
| POST/PUT Workflow、Graph、Task、Draft、Apply | 6 | 非空 | 全部进入 limited reader |
| GET/DELETE/SSE | 10 | 空 | 全部不读取 request body |

六条当前 body route 都是同一个 `_BackendJSONRoute`，没有遗漏另一个 adapter 或按
方法名硬编码的旁路。

### 2.2 declared oversize

新增测试对 POST Workflow 和 PUT Workflow 组合：

- `text/plain`
- 缺失 `Content-Type`
- 未知 MIME

均证明 `Content-Length: 8388609` 在零次 `receive` 时返回精确：

```json
{
  "code": 400,
  "error": {
    "code": "invalid_input",
    "message": "提交内容格式不正确"
  }
}
```

本 reviewer 又对全部六条 body route 分别执行同一
`text/plain + Content-Length: 8388609` 对抗。六条均为：

```text
HTTP 400 invalid_input
receive calls = 0
service calls = 0
side effects = 0
```

因此修复不是只让独立测试选中的两条 route 变绿。

### 2.3 missing/chunked actual oversize

新增参数矩阵覆盖两条代表 body route、三种非 JSON MIME 和 missing/chunked
framing。八个 1 MiB chunk 后再发送 1 byte 与 sentinel：

```text
receive calls = 9
sentinel 未读
HTTP 400 invalid_input
service calls = 0
side effects = 0
```

实际 byte 计数仍发生在 `body.extend` 前；声明长度不足、缺失或 chunked 都不能
绕过实际 stream 上限。

### 2.4 exact limit 与错误 MIME

三种非 JSON MIME 的恰好 8 MiB body 都只读取一次并写入 `_body` cache；FastAPI
随后按既有 strict content-type 规则返回相同 `400 invalid_input`。adapter 不会
把非 JSON bytes 解释成 JSON，也不会进入业务 Service。

缺失 `Content-Type` 在当前 FastAPI `strict_content_type=True` 下同样作为 bytes
交给 validation，而不是绕过 adapter 后调用标准库 JSON parser。即使内容碰巧是
合法 JSON，也不会进入 Workflow Service。

## 3. Regression hypotheses

### R-01：Request cache 被预读破坏

**Disposition：`rejected-with-evidence`**

独立自定义 `_BackendJSONRoute` 分别验证：

1. JSON 分两个 chunk 到达后，handler 读取 `body()`、`json()`、`stream()`；
2. `application/octet-stream` 分两个 chunk 到达后，FastAPI 注入 bytes，
   handler 再读取两次 `body()` 和一次 `stream()`。

结果分别为：

```text
JSON:
payload == parsed == {"x": 1}
body == stream == b'{"x":1}'

non-JSON:
payload == body #1 == body #2 == stream == b'abcd'
```

没有二次 receive、空 body 或 `Stream consumed`。`_read_limited_body` 只负责
形成完整 `_body` cache，JSON path 再形成 `_json` cache，职责没有交叉。

### R-02：body-field gate 破坏 GET/SSE

**Disposition：`rejected-with-evidence`**

新增测试证明：

- `GET /api/v1/workflows` 携带全局 `Content-Type: application/json` 时不 receive，
  正常返回 Workflow list；
- `GET /api/v1/events` 使用非法 cursor 时不 receive，仍保留冻结的 SSE
  unwrapped error exception。

本 reviewer 再给 bodyless GET 放入非空、非法 JSON body 和
`Content-Length: 9`；route 仍为 200、receive 次数为零。bodyless route 不再因为
调用方的 JSON header 主动消费或解析 request body。

### R-03：只保护 JSON 或只保护部分 MIME

**Disposition：`rejected-with-evidence`**

byte budget 判断发生在 MIME 分支外；`text/plain`、缺失 MIME、未知 MIME 与
`application/octet-stream` 均使用相同 reader。JSON-specific decoder 仍只在
JSON/`+json` 分支运行，所以修复没有把 integer/depth 业务复制到非 JSON path。

### R-04：影响同一 FastAPI app 的非 Workflow route

**Disposition：`rejected-with-evidence`**

`_BackendJSONRoute` 只配置在 Workflow `APIRouter`。本轮没有增加全局 middleware，
不改变 docs、health、设备或其他 router 的 request body 行为。

## 4. NB-01

### NB-01：`Content-Length` 词法比 HTTP grammar 宽

**Disposition：`non-blocking-follow-up`**

本轮没有修改 `int(content_length, 10)`，所以 raw ASGI scope 中 `+13`、`1_3`
仍可按数值 13 解析。但该行为仍不影响合并：

- 数值超过 8 MiB 时仍在读取前拒绝；
- 实际 stream 始终独立累计，under-declared body 不能绕过；
- B-07 修复后该停止线覆盖任意 MIME；
- h11 对 `+13` 与 `1_3` 均返回
  `RemoteProtocolError: bad Content-Length`，不会生成生产 ASGI scope；
- 没有第二个 parser、业务调用或持久副作用。

严格 ASCII `1*DIGIT` 可作为 transport hygiene 后续项，但不应把 HTTP server
framing parser 复制到 Workflow 业务 route，也不阻止 02A4 继续评审。

## 5. 测试与门禁证据

本 reviewer 在精确候选
`edef8003cabf246d89d45340ba94961f63b8a4b4` 实际运行：

```text
/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q \
  tests/workflow/test_route_body_budget.py \
  tests/workflow/test_json_resource_budget.py
=> 39 passed, 2 warnings

/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q \
  tests/workflow/test_route_body_budget.py \
  tests/workflow/test_json_resource_budget.py \
  tests/workflow/test_schema_codec_hardening.py \
  tests/workflow/test_value_schema_hardening.py \
  tests/workflow/test_value_schema_v1.py
=> 212 passed, 1 warning

/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q tests/workflow
=> 644 passed, 3 warnings

/home/changjunhan/.micromamba/envs/unilab/bin/python -m ruff check \
  --select E,F,I --ignore E501 \
  unilabos/app/workflow_api.py \
  tests/workflow/test_route_body_budget.py \
  tests/workflow/test_json_resource_budget.py \
  tests/workflow/test_schema_codec_hardening.py \
  tests/workflow/test_value_schema_hardening.py \
  tests/workflow/test_value_schema_v1.py
=> All checks passed

/home/changjunhan/.micromamba/envs/unilab/bin/python -m ruff format --check \
  <上述 6 个 production/test 文件>
=> 6 files already formatted

git diff --check 1111380...edef800
=> passed
```

候选趋势报告已登记正式完整测试
`1056 passed, 3 skipped`。生产实现 `a54c402` 后只有趋势文档提交，且本次目标、
Schema/资源、Workflow 累积与独立对抗没有出现可疑信号，因此不重复正式全量。

另运行只读对抗，覆盖：

1. 当前全部 16 条 Workflow route 的 `body_field` 与 route class 枚举；
2. 六条 body route 的错误 MIME declared oversize 零读取；
3. JSON 与 non-JSON 成功路径的 body/json/stream cache；
4. bodyless GET 携带非空非法 JSON body；
5. missing MIME 的 strict content-type 行为；
6. h11 对 NB-01 非规范长度的真实 framing 行为。

没有修改 production、测试、其他文档、Backend 或前端。

## 6. 下一门禁

| Finding | Disposition | 下一步 |
|---|---|---|
| B-07 MIME 关闭 body budget | `accepted-fixed` | 保持 23 项 route 回归 |
| Request cache / bodyless route / MIME 回归假设 | `rejected-with-evidence` | 无修复 |
| NB-01 Content-Length 词法偏宽 | `non-blocking-follow-up` | 不阻止 02A4 |

本轮 blocking 数为 **0**。固定候选可以进入下一名独立合同 reviewer；02A4 仍需按
迁移门禁完成后续顺序评审，所有 reviewer 通过且精确候选不再修改后，才能合并
`integration/workflow-task-runtime`。
