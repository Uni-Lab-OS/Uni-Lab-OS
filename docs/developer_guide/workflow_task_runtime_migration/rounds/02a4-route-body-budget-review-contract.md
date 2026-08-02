# Round 02A4 路由请求体预算契约复核

日期：2026-07-31

评审分支：`review/02a4-contract`

固定候选：`e5b8d1cee58c949cc7643cff3ceecec59df18aa8`

对比基线：`1111380`

评审范围：仅复核 OS Workflow API 的路由形状、请求体预算分层、错误信封、无请求体路由，以及与 D-101、D-117 的一致性。

## 结论

本次独立契约复核没有发现阻塞项。固定候选可以进入第三名 reviewer 的最终门禁。

- 阻塞项：0
- 非阻塞跟进：2
- 生产代码或测试改动：0
- 本次新增内容：仅本报告

## 契约轴

### C-01 `body_field` 作为路由边界：accepted-fixed

实现以 FastAPI 路由在构建处理器时已经确定的 `self.body_field is not None` 判断该路由是否声明请求体，而不再以 `Content-Type` 推断。这与 D-101 的“所有声明请求体的 Workflow 路由均受 8 MiB 字节预算约束”一致。

只读动态盘点得到：

- Workflow API 共 16 条路由，全部使用 `_BackendJSONRoute`。
- 其中 6 条路由存在 `body_field`，10 条不存在。
- 16 条路由上，`body_field is not None` 与 OpenAPI 的 `requestBody` 是否存在逐条一致。

因此该边界对当前公开路由形状是确定且集中生效的，不依赖调用方 MIME，也没有为单一路由写特例。

### C-02 字节预算与 JSON 预算分层：accepted-fixed

当前处理顺序符合 D-101：

1. 路由声明请求体时，先执行 8 MiB 原始字节预算。
2. 仅对 `application/json` 或 `+json` MIME 使用唯一的受限 JSON 解码器。
3. 非 JSON 请求体在字节预算后交回 FastAPI 做既有解析和校验。
4. 路由未声明请求体时不预读请求流。

对全部 6 条有请求体路由逐条进行了只读对抗验证：

- `text/plain` 配合声明长度 `8 MiB + 1`：均返回 `400 invalid_input`，且未调用 `receive`。
- `application/problem+json` 配合 4097 位整数：均返回 `400 invalid_input`。
- 两类失败均未进入 service 层。

这同时证明了字节预算不受 JSON MIME 限制，而整数位数预算又只位于 JSON 解码层。

### C-03 错误信封与无副作用：accepted-fixed

超出字节预算、非法 UTF-8、受限 JSON 解码失败统一映射为冻结的 `400 invalid_input` 错误信封；对抗用例确认失败时 service 调用次数为 0。

已有 SSE 游标错误仍保持冻结的非包裹错误结构，没有因通用路由包装器而改变。

### C-04 无请求体 GET/SSE 不预读：accepted-fixed

带有非法 JSON 请求体且声明长度超过 8 MiB 时：

- 普通无请求体 GET 仍按原契约返回成功，包装器没有调用 `receive`。
- SSE 的非法游标仍按原契约返回 `400`，包装器同样没有调用 `receive`。

SSE 响应开始后为检测客户端断连而读取 ASGI 消息属于流式响应本身的行为，不等于路由包装器预读请求体。

### C-05 与 D-117/下一阶段 OS Interface 冲突：rejected-with-evidence

没有发现当前候选与 D-117 冲突：

- `ApplyRequest` 只接收不透明的 `candidate_hash`，未重新引入客户端 Candidate、旧 draft hash 或 Workflow revision。
- Draft 写入仍承载双 CAS 所需的 `expected_draft_hash` 与 `expected_workflow_revision`。
- 本轮只改变 OS 的通用请求体预算门禁，没有修改 Backend 或 FE。
- D-117 规划的顶层 `/api/v1/authoring/compile`、`/generate-python`、`/validate` 尚未实现，当前候选没有提前扩张其接口。

## 标准轴

### S-01 “测试只列两条有请求体路由，因此当前为 false-green”：rejected-with-evidence

静态参数表确实只选取了 `POST /workflows` 与 `PUT /workflows/{uuid}` 两条代表路由；但当前实现位于统一的 `_BackendJSONRoute`，并由 `body_field` 决定是否应用门禁。只读路由盘点确认全部 16 条路由使用该 route class，且全部 6 条有请求体路由均通过了逐条字节预算和 JSON 预算对抗验证。

因此“当前其余 4 条路由未受保护”的假设不成立。为了防止未来新增路由时回归，仍应把动态路由盘点固化为自动化测试，见 NB-02。

### S-02 代码结构与工具门禁：accepted-fixed

本轮变化集中在统一路由处理器与相应测试，没有按路由复制预算逻辑，也没有引入推测性 DTO 或 Backend/FE 改动。Ruff、格式检查和 diff whitespace 检查均通过，未发现与仓库标准冲突的新增代码气味。

受“本轮只能有一个 reviewer”的执行约束，本报告没有按 `code-review` 技能默认方式再派生两个并行 subagent；契约轴与标准轴由当前唯一 reviewer 分开检查和记录。

## 非阻塞跟进

### NB-01 `Content-Length` 词法仍较宽松：non-blocking-follow-up

当前整数解析会接受 `+13`、`1_3` 这类 Python `int()` 可解析形式。真实 h11 HTTP 栈会在请求进入应用前拒绝这些格式，因此目前不构成可达的安全绕过，也不阻塞本轮合并。

若后续需要支持非 h11 的自定义 ASGI 接入，可将语法收紧为仅十进制数字，并增加原始 ASGI 回归测试。

### NB-02 为 D-117 新路由固化自动盘点和错误归一化：non-blocking-follow-up

实现 D-117 的 `/api/v1/authoring/*` 顶层接口时，应同时完成：

- 继续通过 `_BackendJSONRoute` 接入相同的请求体门禁。
- 将顶层 authoring 路径纳入 Workflow 请求校验错误的冻结信封归一化范围。
- 增加动态测试，断言所有 OpenAPI `requestBody` 路由都使用该 route class，并逐条覆盖字节预算。

这些接口当前尚不存在，所以此项是下一阶段的前置验收条件，不是 02A4 的阻塞项。

## 验证记录

使用解释器：`/home/changjunhan/.micromamba/envs/unilab/bin/python`

```text
python -m pytest -q tests/workflow/test_route_body_budget.py tests/workflow/test_json_resource_budget.py
39 passed, 2 warnings in 2.32s

python -m pytest -q tests/workflow
644 passed, 3 warnings in 27.88s

python -m ruff check --select E,F,I --ignore E501 \
  unilabos/app/workflow_api.py \
  tests/workflow/test_route_body_budget.py \
  tests/workflow/test_json_resource_budget.py
All checks passed!

python -m ruff format --check \
  unilabos/app/workflow_api.py \
  tests/workflow/test_route_body_budget.py \
  tests/workflow/test_json_resource_budget.py
3 files already formatted

git diff --check 1111380...e5b8d1c
通过
```

补充只读对抗验证：

```text
Workflow 路由：16
声明请求体：6
无请求体：10
body_field 与 OpenAPI requestBody：逐条一致
全部 6 条请求体路由：字节预算与 JSON 整数预算均通过
无请求体 GET：未被包装器预读
无请求体 SSE：未被包装器预读，冻结错误结构保持
```

## 下一门禁

固定候选当前可进入第三名 reviewer。第三名 reviewer 应继续以 `e5b8d1c` 为生产/测试候选基准，并把本报告提交视为评审证据，不把文档提交误计为候选实现变化。
