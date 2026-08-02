# Phase 02A3：JSON 资源预算模块与安全复审

日期：2026-07-31

评审分支：`review/02a3-module-safety`

修复基线：`3e63ecff1c8de11766cef314efdcd869a8541fd0`

固定候选：`9b174ebf9bdbf52804f51c4ac79c304ee94374eb`

评审范围：独立复审 B-05、B-06，核对 D-101、生产实现、测试与既有
Backend-shaped Workflow route。重点验证不可信 body/integer 的拒绝时机、零业务
副作用、可信 canonical 整数、完整值深度、canonical value 不变量和 Request cache。
本报告只修改评审文档，不修改 production、测试、其他文档、Backend 或前端。

## 1. 结论

**B-05、B-06 均为 `accepted-fixed`。候选可以进入最后一名独立风险 reviewer。**

| 评审轴 | Blocking | Non-blocking | 结论 |
|---|---:|---:|---|
| Module/Standards | 0 | 1 | 通过，可后续加固 |
| Spec/Safety | 0 | 0 | 通过 |

D-101 把不可信 transport 预算与可信 canonical 数学值能力分开，同时把 depth
收口成完整 document/value 的一个定义。生产实现保持一个 JSON decoder 和一个
Schema complete-value validator，没有增加第二 Authority、业务字段或持久状态。

发现一个不影响 D-101 资源安全的 non-blocking：`Content-Length` 使用 Python
`int()` 解析，会接受 `+13`、`1_3` 等非 HTTP `1*DIGIT` 拼写。实际 stream 仍独立
执行 8 MiB 上限，因此不能借此绕过预算或触发 service；建议在后续风险收尾中以精确
ASCII digit grammar 收紧。

## 2. B-05 disposition：不可信 JSON 资源边界

### 2.1 Disposition

**`accepted-fixed`**

D-101 冻结：

- 公共 Workflow JSON body 最大 8 MiB；
- external integer token 最大 4096 个十进制数字，不计负号；
- declared oversize 零读取，missing/chunked 在第一个超限 chunk 停止；
- 失败是精确 `400 invalid_input` envelope，且不进入 service；
- trusted decoder 继续支持任意 Python `int`，不修改解释器全局状态。

生产实现与这些要求一致。

### 2.2 Body 读取停止线

`_read_limited_body` 在访问 `request.stream()` 前解析并检查 declared length
（`unilabos/app/workflow_api.py:49-59`）。因此合法的
`Content-Length: 8388609` 在 receive 调用次数为零时直接失败。

missing length 和 chunked 均由同一 `request.stream()` loop 累计实际 bytes；
判断发生在 `body.extend(chunk)` 前
（`unilabos/app/workflow_api.py:61-68`）。八个 1 MiB chunk 后，第九个 1-byte
chunk 首次超限时立即返回；第十个 sentinel chunk 没有被读取。没有另建 chunked
parser 或依赖 declared length 代替实际预算。

独立对抗还覆盖了 under-declared body：

```text
Content-Length: 1
actual: 8 MiB + 1 byte + sentinel
=> 400 invalid_input
=> receive calls = 9
=> sentinel 未读
=> service calls = 0
```

因此 declared/actual mismatch 不能绕过实际上限。actual body 在预算内而
`Content-Length` 与其不等时，Adapter 保持候选前的 Request.body 行为并继续交给
业务层；ASGI server 仍拥有 HTTP framing 一致性，D-101 没有在应用层新增
declared/actual equality 合同。

### 2.3 Integer 拒绝发生在 bigint 构造前

`decode_json_bytes` 先由已有 JSON number regex 完成词法识别，再计算符号之外的
digit count；超过传入预算时在 `_decode_json_integer(raw)` 之前抛
`ValueError`（`unilabos/workflow/json_codec.py:132-146`）。

对 `_decode_json_integer` 安装只记录调用的只读 spy 后：

| 场景 | HTTP/decoder 结果 | bigint helper 调用 |
|---|---|---:|
| 正 4096 digits | 接受 | 1 |
| 负 4096 digits | 接受 | 1 |
| 正 4097 digits | 拒绝 | 0 |
| 负 4097 digits | 拒绝 | 0 |

HTTP 对抗结果进一步确认：

```text
4096 digits => 201，service calls = 1
4097 digits => 400 invalid_input，service calls = 0
```

4097 位值放在 Backend-shaped create request 的 unknown future field 中也会在
Pydantic/业务 handler 前被拒绝，因此不能借 shared request 的 `extra="ignore"`
穿透预算。

### 2.4 Envelope、副作用和可信路径

`_BackendJSONRoute` 在读取或 decode 的
`OverflowError | UnicodeError | ValueError` 上直接返回既有
`WorkflowError("invalid_input")` envelope
（`unilabos/app/workflow_api.py:71-95`）。对 body、integer 和 depth 三类失败均是：

```json
{
  "code": 400,
  "error": {
    "code": "invalid_input",
    "message": "提交内容格式不正确"
  }
}
```

route handler 未被调用，所以 Workflow、Candidate、Task 及测试 service
side-effect list 都保持空。

公共 decoder 的 `max_integer_digits` 缺省仍为 `None`；可信 5001 位正负整数继续
标准 JSON number round-trip。production 未调用 `sys.set_int_max_str_digits`，
目标测试和独立 HTTP 对抗前后 `sys.get_int_max_str_digits()` 均不变。

### 2.5 Request cache 与既有 route 回归

成功读取后 Adapter 同时设置完整 `_body` cache 和 decoder 产生的 `_json` cache
（`unilabos/app/workflow_api.py:66-68`、`:82-86`）。独立自定义 route 在同一
Request 上依次执行：

```text
await request.body()
await request.body()
await request.json()
async for chunk in request.stream()
```

四次均得到同一 `{"x":1}` 内容，没有 `Stream consumed`、空 body 或二次 receive。

完整 Workflow 测试 621 个全部通过，覆盖既有成功 envelope、FastAPI validation
归一化、深层合法/截断 JSON、非有限数字、strict control 类型和 Authoring
错误投影；未发现 `_BackendJSONRoute` 对 Backend-shaped route 的回归。

## 3. B-06 disposition：完整值深度与 canonical 不变量

### 3.1 Disposition

**`accepted-fixed`**

原实现把 opaque object 脱离 array/Contract wrapper 单独计深。修复后：

- `_normalize_with_schema` 只完成值类型与 schema constraint 规范化；
- `normalize_value` 在返回 dict/list 前从 root 对完整 normalized value 统一调用
  `_validate_json_value`；
- Input/Output Contract 在 `_from_canonical` 前，从 envelope root 对完整
  canonical payload 调用同一 validator；
- validator 保持迭代式，并通过公共 codec 返回不共享的 copy
  （`unilabos/workflow/schema.py:471-511`、`:594-611`、`:798-807`、
  `:875-884`）。

caller 不再需要计算 array、descriptor 或 envelope 的隐含 wrapper 余量。

### 3.2 深度临界值与 pointer

独立检查与 16 个新 case 一致：

| 上下文 | 最大接受 | 首个拒绝 | 拒绝 code/path |
|---|---:|---:|---|
| standalone object | object depth 10000 | 10001 | `invalid_value`, `/next` × 10000 |
| `list[object]` | item depth 9999 | 10000 | `invalid_value`, `/0` + `/next` × 9999 |
| Input default | default depth 9997 | 9998 | `invalid_contract`, `/parameters/0/default` + `/next` × 9997 |

接受边界均能完整 traverse；Input default depth 9997 可 parse，并可重复
`to_dict()`。拒绝边界没有泄漏 `ValueError`、`RecursionError`、
`AttributeError` 或 `AssertionError`，pointer 指向完整值中的第一个超限容器。

### 3.3 Cycle、shared reference 与不可破坏性

额外只读对抗结果：

```text
standalone cycle => invalid_value /self
Input default cycle => invalid_contract /parameters/0/default/self
```

同一 child dict 被两个 sibling 引用时不被误判为 cycle；normalized value 和
Contract dump 中两个 sibling 内容相等但对象 identity 不同，也不与 caller
container 共享。修改第一次 `to_dict()` 的深层 list 后，再次 dump 仍返回原
canonical 数据。

因此：

- active-path cycle 检测没有把合法 shared reference 当成环；
- complete-value validator 同时承担 validation 与非共享 copy；
- parser 返回的 canonical value 在普通公开操作范围内始终可 `to_dict()`；
- B-06 修复没有恢复递归 `deepcopy` 或第二套深度 walker。

## 4. Non-blocking finding

### NB-01：`Content-Length` 词法比 HTTP grammar 宽

**级别：non-blocking**

`_read_limited_body` 直接执行 `int(content_length, 10)`
（`unilabos/app/workflow_api.py:52-57`）。Python 接受部分不是 HTTP
`Content-Length = 1*DIGIT` 的拼写。独立 ASGI 对抗：

| Header value | 当前结果 | receive/service |
|---|---|---|
| `abc` | 400 `invalid_input` | 0 / 0 |
| `-1` | 400 `invalid_input` | 0 / 0 |
| `+13` | 按 13 接受 | 1 / 1 |
| `1_3` | 按 13 接受 | 1 / 1 |

这不是 D-101 资源绕过：解析后的数值仍检查 declared 上限，实际 stream 始终独立
检查 8 MiB；部署所用 HTTP server 通常也会在构造 ASGI scope 前验证 framing。
因此本项不阻塞 B-05 关闭或最后风险评审。

建议用精确 ASCII `[0-9]+` grammar 后再转换，保留 leading zero 的合法语义，并补
`+13`、`1_3`、alphabetic、negative 和超长 header 回归。若最终风险 reviewer
要求应用层严格拥有 Content-Length grammar，可在合并前用独立小修复轮关闭。

## 5. 门禁证据

本 reviewer 在固定候选实际运行：

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

git diff --check 3e63ecf...9b174eb
=> passed
```

本次按任务要求没有重复正式 `pytest tests -q`；候选趋势报告已登记
`1033 passed, 3 skipped`。

另运行只读对抗 snippet，覆盖：

1. malformed/negative/under-declared/over-declared `Content-Length`；
2. under-declared actual oversize 在首个超限 chunk 停止；
3. HTTP 4096/4097 digits 与进程全局状态；
4. Request body/json/stream cache；
5. standalone 与 Contract cycle/shared reference/dump mutation。

没有修改 production 或测试。

## 6. 最终 disposition 与下一门禁

| Finding | Disposition | 证据 |
|---|---|---|
| B-05 不可信大整数无资源边界 | `accepted-fixed` | 8 MiB ingress、4096 digits pre-bigint、零 service side effect |
| B-06 wrapper 深度预算不一致 | `accepted-fixed` | 完整值统一计深、稳定 pointer、parser value 可 dump |
| NB-01 Content-Length 词法偏宽 | `non-blocking-open` | 无预算绕过；建议最终风险收尾 |

本模块/安全复审允许固定候选 `9b174eb` 进入最后一名独立风险 reviewer。只有最后风险
评审也无 blocking、任何必要修复均在新 SHA 完成复审、正式 1033+3 门禁记录仍有效，
02A3 才可合并 integration。任何 production 变更都会使本报告针对的固定 SHA 失效。
