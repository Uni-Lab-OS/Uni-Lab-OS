# Phase 02A：Schema v1 最终独立风险评审

日期：2026-07-31

评审分支：`review/02a-final-risk`

原始集成基线：`e85a60c1acec53cf8d6e2643e40a7ba0c12cd36f`

固定候选：`635594a8e5864f51a0f2e092529d0fe352825ceb`

评审范围：完整 02A、02A1、02A2、02A3，而非仅最近提交。重点检查 Schema
公共入口的异常与归属、JSON decoder 的恶意输入、公共 HTTP body 预算、Request
cache、零业务副作用、可信 canonical 大整数与外部预算隔离，以及后续
Authoring/FE-OS 接入风险。本报告只新增评审文档，不修改 production、测试、其他
文档、Backend 或前端。

## 1. 结论

**固定候选暂不可合并到 `integration/workflow-task-runtime`。**

| 评审轴 | Blocking | Non-blocking | 结论 |
|---|---:|---:|---|
| Regression / abuse / failure isolation | 1 | 1 | 不通过 |
| D-101 / Schema 合同 | 1 | 0 | 不通过 |

前两名 reviewer 已关闭 B-01～B-06；本次没有盲从其结论，重新检查后确认这些
修复本身成立。但是公共 body budget 只在来件声明 JSON MIME 时启用。攻击者可把
同一超限 body 标记为 `text/plain`，使 FastAPI 完整读取 body 后才做请求校验。
这违反 D-101“在 business validation 前应用公共 Workflow HTTP body 预算”的
停止线，记为 B-07。

上一份报告的 NB-01（`Content-Length` 接受 `+13`、`1_3`）仍是
non-blocking：它不能绕过 declared 数值上限或实际 stream 上限，且生产使用的
h11 会在创建 ASGI scope 前拒绝这些拼写。建议与 B-07 的小修复轮一起收紧，但它
本身不是拒绝合并的理由。

## 2. Blocking finding

### B-07：不可信 `Content-Type` 可关闭 8 MiB body 预算

**级别：blocking-new**

D-101 冻结的是公共 Workflow HTTP adapter 的 ingress 资源边界：

- body 最大 8 MiB；
- 合法且超限的 `Content-Length` 在读取前拒绝；
- missing/chunked length 在首个超限 chunk 停止；
- 预算失败发生在 business validation 前，使用 `400 invalid_input`，且没有业务
  副作用。

02A3 设计也把“公共 Workflow JSON 请求 body 最大 8 MiB”和“service 调用次数为
零”同时列为资源合同
（`02a3-json-resource-budget-design.md:22-36`）。这里的可信边界是目标 route
期待 JSON body，不是调用方可以用一个不可信 MIME header 选择是否启用资源保护。

生产实现却把 `_read_limited_body` 整体放在 MIME 判断内
（`unilabos/app/workflow_api.py:77-93`）：

```python
if mime == "application/json" or mime.endswith("+json"):
    body = await _read_limited_body(request)
    request._json = decode_json_bytes(...)
```

因此错误或恶意 MIME 会跳过 byte budget，然后由 FastAPI 的普通 body path 完整
读取。

#### 最小复现

对固定候选直接调用 ASGI app：

```text
POST /api/v1/workflows
Content-Type: text/plain
Content-Length: 8388609
body: 8388609 bytes
```

实际结果：

```text
HTTP status = 400 invalid_input
receive calls = 1
receive bytes = 8388609
service calls = 0
service side effects = 0
```

同一个 declared-oversize body 使用 `Content-Type: application/json` 时：

```text
HTTP status = 400 invalid_input
receive calls = 0
service calls = 0
```

因此错误 envelope 和零业务副作用是正确的，但攻击者仅修改 MIME 就能从“零读取”
切换为“应用层完整读取并持有任意大小 body”。这不是 Pydantic 业务校验能补救的
问题，也不能依赖客户端诚实声明 JSON。D-101 的 body budget 本来就是为了在不可信
输入进入同步 decoder/validation 前提供明确资源停止线。

#### 最小修复边界

修复不需要第二套 parser、全局中间件或新业务状态：

1. 在 `_BackendJSONRoute` 确认当前 `APIRoute` 有 request body field 时，对所有
   MIME 先调用同一个 `_read_limited_body`；
2. 成功读取后保持 `_body` cache，交给 FastAPI 继续处理错误 MIME；
3. 只有 JSON 或 `+json` MIME 才调用共享 `decode_json_bytes` 并设置 `_json`
   cache；
4. 无 body 的 GET、DELETE、SSE 不预读、不解码；
5. 独立 RED 至少覆盖：
   - `text/plain` declared 8 MiB + 1 在零次 receive 时拒绝；
   - `text/plain` missing/chunked length 在首个超限 chunk 停止；
   - 两种失败均为精确 `400 invalid_input` 且 service 调用为零；
   - JSON 成功路径的 `body()`、`json()`、`stream()` cache；
   - bodyless GET/SSE 不因全局 JSON header 读取空 body。

修复应保持 route-local，不扩散到 Backend、前端、其他 OS 路由或业务 Service。

## 3. 既有 finding disposition

| Finding | 最终 disposition | 本次独立证据 |
|---|---|---|
| B-01 canonical value 可破坏 | `accepted-fixed` | 普通 set/del 受阻；dump 独立；有界随机入口无 mutation |
| B-02 嵌套 nullable | `accepted-fixed` | finite grammar 与完整路径回归全绿 |
| B-03 深层 JSON 递归异常 | `accepted-fixed` | 迭代 codec；10000/10001 临界稳定 |
| B-04 合法大整数裸异常 | `accepted-fixed` | trusted 5001 digits round-trip；无全局设置修改 |
| B-05 不可信大整数无预算 | `accepted-fixed` | public 4096/4097 在 bigint 构造前分界 |
| B-06 wrapper 深度漏计 | `accepted-fixed` | standalone/list/Contract 均按完整值计深 |
| B-07 MIME 关闭 body budget | `blocking-open` | `text/plain` declared oversize 被完整读取 |

### 3.1 Schema 公共入口

四个公开入口仍形成一个纯内存深模块：

- `parse_value_schema`
- `parse_input_contract`
- `parse_output_contract`
- `normalize_value`

除现有 189 项合同外，本次对前三个 parser 各运行 10,000 个有界随机 built-in JSON
形状，对 `normalize_value` 再运行 10,000 个随机值。所有无效值只产生
`WorkflowSchemaError`，没有泄漏 `KeyError`、`AssertionError`、
`RecursionError`、`AttributeError` 或普通 `ValueError`；输入 `dict/list`
均未被修改。

opaque object、array、ResourceSlot 和 Contract dump 不共享 caller 容器；
修改一次 dump 不影响 canonical object 或后续 dump。cycle、shared reference、
深链和大整数回归继续通过。

### 3.2 malformed JSON、UTF-8、数字与深度

独立只读对抗结果：

| 输入 | 结果 |
|---|---|
| 非法 UTF-8 | HTTP `400 invalid_input`，service 0 |
| `+1`、`01`、`1.`、`1e` | decoder `ValueError`；HTTP 路径归一化为 400 |
| `NaN`、`Infinity`、`1e309` | 拒绝非有限数；无业务调用 |
| 截断 object、一个 body 两个值 | 拒绝；无业务调用 |
| UTF-8 code point 跨两个 chunk | 合法输入正常组合并接受 |
| 非法 UTF-8 跨两个 chunk | 400；service 0 |
| 完整 document depth 10000 | 接受 |
| 完整 document depth 10001 | 400；service 0 |
| external integer 4096 digits | 接受 |
| external integer 4097 digits | bigint helper 调用前拒绝 |
| trusted canonical integer 5001 digits | 标准 JSON number round-trip |

`decode_json_bytes(..., max_integer_digits=4096)` 只在公共 adapter 显式启用；缺省
`None` 的可信 Store/canonical path 继续支持任意 Python `int`。production
没有调用 `sys.set_int_max_str_digits`，数学值能力与不可信计算预算保持分离。

### 3.3 Request cache

成功的分块 JSON 请求在同一个 Request 上依次执行：

```text
await request.body()
await request.body()
await request.json()
async for chunk in request.stream()
```

两次 body、JSON 值和重新读取的 stream 均一致，没有 `Stream consumed`、空 body
或二次 receive。`_read_limited_body` 成功后设置 `_body`，adapter 设置 `_json`，
当前 Starlette cache 语义没有被破坏。

## 4. Non-blocking finding 与传输观察

### NB-01：`Content-Length` 词法比 HTTP grammar 宽

**级别：non-blocking-open**

`int(content_length, 10)` 接受 `+13` 和 `1_3`。raw ASGI 对抗中两者按数值 13
处理，合法小 body 会进入 service；但：

- `+8388609`、`8_388_609` 仍按数值在读取前拒绝；
- under-declared actual oversize 仍由实际 stream budget 拒绝；
- 无法借此越过 integer digit、JSON depth 或业务副作用停止线；
- h11 对 `+13`、`1_3` 均抛 `RemoteProtocolError: bad Content-Length`，不会把
  这类真实 HTTP 请求构造成 ASGI scope。

因此 NB-01 不升级为 blocking。建议 B-07 修复轮用精确 ASCII `[0-9]+` 收紧并补
回归，避免应用层行为依赖 server 实现。

### 4.1 重复 `Content-Length`

raw ASGI scope 中 Starlette 读取第一个 header：

- small 在前、oversize 在后时，实际小 body 可进入 service；
- oversize 在前时零读取拒绝；
- 无论顺序，实际 body 超限仍由 stream budget 拒绝。

h11 会拒绝冲突的重复长度，并把相同的重复长度规范为一个 header。该观察没有形成
D-101 预算绕过，故不另记 blocking。

### 4.2 客户端中途断连

分块 body 中途收到 `http.disconnect` 时，Starlette 的 `ClientDisconnect` 会传播，
raw ASGI harness 记录 500，service 与副作用均为零。候选基线
`e85a60c` 的 `await request.body()` 使用同一 `Request.stream()`，行为相同；
这不是新 reader 引入的 regression。连接已经断开时无法向该客户端可靠交付 JSON
envelope，因此不以 D-101 三类可响应失败的 envelope 要求将其升级为 blocking。

### 4.3 bodyless Workflow route 与其他 FastAPI route

当前 route class 会在任意 Workflow route 看到 JSON MIME 时尝试 decode；所以
`GET /api/v1/workflows` 带全局 `Content-Type: application/json` 且空 body 会返回
400。这一行为在原始基线 `e85a60c` 已存在，不是 02A3 stream reader 引入。
它仍是 FE-OS 接入风险；B-07 建议的 body-field gate 可同时消除该风险。

`_BackendJSONRoute` 只属于 Workflow router，不会改变同一 FastAPI app 上其他
router 的非 Workflow route。

## 5. 门禁证据

本 reviewer 在精确候选 `635594a8e5864f51a0f2e092529d0fe352825ceb`
实际运行：

```text
/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q \
  tests/workflow/test_json_resource_budget.py \
  tests/workflow/test_schema_codec_hardening.py \
  tests/workflow/test_value_schema_hardening.py \
  tests/workflow/test_value_schema_v1.py
=> 189 passed, 2 warnings

/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q tests/workflow
=> 621 passed, 3 warnings

/home/changjunhan/.micromamba/envs/unilab/bin/python -m ruff check \
  --select E,F,I --ignore E501 \
  unilabos/app/workflow_api.py \
  unilabos/workflow/json_codec.py \
  unilabos/workflow/schema.py \
  tests/workflow/test_json_resource_budget.py \
  tests/workflow/test_schema_codec_hardening.py \
  tests/workflow/test_value_schema_hardening.py \
  tests/workflow/test_value_schema_v1.py
=> All checks passed

/home/changjunhan/.micromamba/envs/unilab/bin/python -m ruff format --check \
  <上述 7 个 production/test 文件>
=> 7 files already formatted

git diff --check e85a60c...635594a
=> passed
```

候选趋势报告已登记正式完整测试
`1033 passed, 3 skipped`。本次已用确定性 ASGI 最小复现确认 blocking，未重复
正式全量；B-07 修复产生新 SHA 后，必须重跑目标、Workflow、正式
`pytest tests -q`、Ruff、format 与 diff gate。

另运行只读对抗，覆盖：

1. 20,000 组 Schema/parser/normalizer 随机 built-in JSON 形状；
2. malformed number、UTF-8、深度和分块组合；
3. Request body/json/stream cache；
4. declared/missing/chunked/重复 `Content-Length`；
5. 客户端断连、bodyless GET 与错误 MIME；
6. h11 对非规范和冲突 `Content-Length` 的真实 framing 行为。

没有修改 production、测试、Backend 或前端。

## 6. 合并条件

| Finding | 当前 disposition | 合并前置条件 |
|---|---|---|
| B-01～B-06 | `accepted-fixed` | 保持既有回归 |
| B-07 MIME 关闭 body budget | `blocking-open` | 独立 RED；route body-field 边界修复；完整门禁；独立复审 |
| NB-01 Content-Length 词法偏宽 | `non-blocking-open` | 建议随 B-07 收紧，不单独阻塞 |

当前固定候选 **不可合并**。B-07 必须在新的测试/实现分支按一轮一名 subagent 的规则
关闭；任何 production 变更都会使本报告针对的 SHA 失效。修复后的精确候选需重新
接受至少合同和最终风险复审，所有 blocking 清零后方可合并 integration。
