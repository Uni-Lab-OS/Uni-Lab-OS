# Phase 02A4：Route body budget 模块安全最终评审

日期：2026-07-31

评审分支：`review/02a4-module-safety`

本轮基线：`1111380acd9e57be1f56223d9aa74a7ff5cb1304`

生产/测试候选：`a54c402c5f18f53dff0abe98fe8aa0f89c3686d0`

固定评审证据候选：`5c8e47fcf62d9a9b4787ef5d73760317fda71f53`

评审范围：第三名 reviewer 独立检查 02A4 production/test diff，并回看完整 02A
lineage 的 B-01～B-07。重点是 FastAPI Adapter seam、route shape、Request cache、
异常隔离、未来 route 漏接、测试维护成本和仓库 Standards。本报告只新增评审文档，
不修改 production、测试、其他文档、Backend 或前端。

## 1. 结论

**行为和模块安全通过，但固定候选当前仍不可作为完整 02A 合并候选。**

| 评审轴 | Blocking | Non-blocking | 结论 |
|---|---:|---:|---|
| Module / safety | 0 | 3 | 通过 |
| Repository Standards | 1 | 0 | 不通过 |

B-01～B-07 全部保持 `accepted-fixed`。本轮没有重新引入可变 canonical value、
嵌套 nullable、递归异常、大整数裸异常、不可信资源预算缺口、完整值深度漏计或
MIME budget 绕过。

唯一 blocking 是生产 diff 主动修改了 `_BackendJSONRoute` 的 class docstring，
但新文本仍是英文，直接违反 `AGENTS.md:75-78` 的简体中文约束。该行修正会产生新的
production SHA；依照迁移 round gate，三名 reviewer 都必须针对新 SHA 顺序复验后，
完整 02A 才能进入 integration 合并候选。

## 2. Standards finding

### S-03：本轮修改的 class docstring 未使用简体中文

**Disposition：`blocking-open`**

仓库规则明确要求：

```text
AGENTS.md:77
Code comments and log messages in simplified Chinese
```

本轮 production diff 把原 class docstring：

```python
"""Preload JSON with the frozen Backend depth and error-envelope rules."""
```

改为：

```python
"""Bound body routes, then preload JSON with the frozen Backend rules."""
```

位置为 `unilabos/app/workflow_api.py:71-72`。这不是候选前未触碰的历史行，而是
`a54c402` 中主动修改的模块说明；docstring 是维护者阅读该 Adapter Interface 的
直接文档，必须服从本轮“新/改注释使用简体中文”的约束。

该 finding 不影响运行时正确性，但它是明确的 repository Standards 门禁，不应淡化
为可永久遗留的 hygiene。最小修复只需把该 docstring 准确改为简体中文，不改变
Interface 或 Implementation；修复后仍须按新 production SHA 重跑相应门禁和顺序
review。

## 3. Module seam 与深度

### M-01：route-local 实现过浅，应抽取新的公共模块

**Disposition：`rejected-with-evidence`**

当前结构是：

```text
_BackendJSONRoute              FastAPI Adapter
  └─ _read_limited_body        Adapter 内部 seam
       └─ Request.stream/cache  Starlette Interface
```

`_BackendJSONRoute` 通过一个 `get_route_handler()` Interface 隐藏：

- route 是否声明 body 的判断；
- declared/actual byte budget；
- MIME 分层；
- JSON integer/depth decoder；
- Request cache；
- `400 invalid_input` 映射。

删除该 Adapter 会让以上规则重新散布到六条 body route，因此 deletion test 成立。
`_read_limited_body` 也不是 pass-through：它集中完成零读取、逐 chunk 停止、受限
复制和 body cache。

当前只有一个 Workflow FastAPI Adapter；把二十余行 reader 提升为新的公共 port 或
跨包 Module 不会增加第二 Adapter，只会扩大 Interface 和测试面。route-local 是
正确的 locality，不存在 Middle Man、Feature Envy 或 speculative seam finding。

### M-02：`body_field` gate 不稳定或不能覆盖真实 route shape

**Disposition：`rejected-with-evidence`**

候选在 handler 构造时冻结：

```python
expects_body = self.body_field is not None
```

它不依赖 method、path、调用方 MIME 或业务 Service。独立动态盘点：

| Route shape | 数量 | `body_field` | `dependant.body_params` |
|---|---:|---|---|
| POST/PUT Workflow、Graph、Task、Draft、Apply | 6 | 非空 | 每条至少 1 |
| GET/DELETE/SSE | 10 | 空 | 0 |

16 条 route 全部使用 `_BackendJSONRoute`；`body_field` 有无与 OpenAPI
`requestBody` 逐条一致。另构造一个通过 FastAPI dependency 声明 Body 的自定义
route，即使 endpoint 本身没有直接 body parameter，`body_field` 仍为非空并进入
预算 gate。

因此当前实现没有按两个代表测试 route 硬编码，也没有遗漏当前其余四条 body route。

### M-03：Request 私有 cache 会破坏下游 FastAPI

**Disposition：`rejected-with-evidence`（当前支持版本）**

当前环境：

```text
FastAPI 0.140.0
Starlette 1.3.1
```

Starlette 的公开 `Request.body()`、`Request.json()` 和 `Request.stream()`
Implementation 分别使用 `_body`、`_json` cache；候选写入的正是当前框架自己使用的
缓存名。

独立自定义 `_BackendJSONRoute` 对抗：

| Body 类型 | 下游读取序列 | 结果 |
|---|---|---|
| JSON，两 chunk | Pydantic payload、两次 `body()`、`json()`、`stream()` | 全部相同，无二次 receive |
| `application/octet-stream` bytes | Pydantic payload、两次 `body()`、`stream()` | 全部相同，无空 body |

没有 `Stream consumed`、重复读取或 JSON/bytes 混淆。bodyless GET/SSE 不进入
reader，也不会创建虚假 cache。

### M-04：预算异常会吞掉业务异常

**Disposition：`rejected-with-evidence`**

`try/except (OverflowError, UnicodeError, ValueError)` 只包围
`_read_limited_body` 和 `decode_json_bytes`；`await route_handler(request)` 位于
该 `try` 外（`unilabos/app/workflow_api.py:78-95`）。

独立自定义 body route 在业务 handler 中抛 `ValueError("business-boom")`，异常保持
原类型和原消息传播，没有被错误改写为 `400 invalid_input`。反向地，body/decoder
失败仍在进入 handler 前返回冻结 envelope。Transport failure 与业务 failure 的
异常域没有混合。

客户端中途断连的 `ClientDisconnect` 也没有被宽泛捕获；这保持候选前
`Request.body()` 的行为，不把不可可靠响应的 disconnect 伪装成可交付业务错误。

## 4. Non-blocking follow-up

### NB-M01：框架内部约定需要升级门禁

**Disposition：`non-blocking-follow-up`**

`APIRoute.body_field`、`Request._body` 和 `Request._json` 是 FastAPI/Starlette
内部约定，不是独立于框架版本的公共 Python protocol；同时
`unilabos/utils/requirements.txt` 只写 `fastapi`，没有锁定 FastAPI/Starlette
版本。

这不构成当前 blocking：

- 当前实际版本的 framework source 与候选写法一致；
- app 启动、JSON/bytes cache、全部当前 route shape 和 644 个 Workflow 测试已
  验证；
- `body_field` 若在升级中消失，会在 app 构造期显式失败，不会静默关闭预算；
- cache 语义变化会被现有 raw ASGI/cache 回归发现；
- 改用 `_receive` replay 或另造 Request subclass 同样依赖私有框架细节，且更复杂。

后续依赖升级必须把 route body、JSON/bytes cache 与 bodyless route case 作为
兼容门禁；仓库级依赖版本治理可单独处理，不应在此轮复制一层虚假框架抽象。

### NB-M02：未来新 router 的覆盖需要动态守护

**Disposition：`non-blocking-follow-up`**

同一个 `create_workflow_router` 中新增 route 会自动继承
`route_class=_BackendJSONRoute`，因此当前 locality 对普通扩展是安全的。风险只在
未来开发者另建一个顶层 Workflow/Authoring router、手工读取 raw Request body，
却没有复用该 route class。

当前 D-117 `/api/v1/authoring/*` route 尚未实现，不能因此判当前 02A4 不完整。
实现这些 route 时应增加一个动态 Interface 测试：每条 OpenAPI `requestBody`
Workflow/Authoring route 必须由同一受预算 route class 承载。该守护比把预算复制到
Service 或全局 middleware 更符合 seam discipline。

### NB-01：Content-Length 词法偏宽

**Disposition：`non-blocking-follow-up`**

既有 `int(content_length, 10)` 仍接受 raw ASGI scope 中的 `+13`、`1_3`。
前两份 reviewer 已证明：

- 超限数值仍在 receive 前拒绝；
- under-declared actual oversize 仍由 stream budget 拒绝；
- h11 在生成生产 ASGI scope 前拒绝这些非 `1*DIGIT` 拼写。

02A4 没有修改该解析逻辑，本项不重开 B-05/B-07。若未来支持非 h11 的自定义
ASGI server，应以 transport hygiene 小轮收紧；不应把 HTTP framing parser 复制进
Workflow Service。

## 5. 测试成本

02A4 production 是一个文件 `+16/-14`，独立测试为一个文件 277 行、23 cases。
表面比例较高，但测试通过 raw ASGI Interface 证明“零 receive”“首个超限 chunk
停止”“无 service side effect”和“bodyless 不预读”；这些结果不能用普通
TestClient status assertion 替代。

测试使用参数表复用同一 receive spy、service spy 和 envelope assertion，断言的是
Interface 可观察行为而非 local variable。当前无需为减少行数删除或降级这些 case。

`test_route_body_budget.py` 与 `test_json_resource_budget.py` 的 ASGI harness 有少量
重复，可在未来测试维护中合并为私有 helper；这不会改变 production Module，也不是
合并 finding。

## 6. 完整 02A finding disposition

| Finding | Disposition | 本次证据 |
|---|---|---|
| B-01 canonical value 可破坏 | `accepted-fixed` | 旧加固与 212 case 保持通过 |
| B-02 嵌套 nullable | `accepted-fixed` | finite grammar/path 回归保持通过 |
| B-03 深层 JSON 递归异常 | `accepted-fixed` | 非递归 10000/10001 临界保持通过 |
| B-04 合法大整数裸异常 | `accepted-fixed` | trusted 5001+ codec case 保持通过 |
| B-05 不可信大整数无预算 | `accepted-fixed` | 4096/4097 pre-bigint 分界保持通过 |
| B-06 wrapper 深度漏计 | `accepted-fixed` | standalone/list/Contract 完整值 case 通过 |
| B-07 MIME 关闭 body budget | `accepted-fixed` | 任意 MIME body route 使用同一 byte gate |
| S-03 新改 docstring 非简体中文 | `blocking-open` | `workflow_api.py:71-72` |

没有证据支持重新打开 B-01～B-07。

## 7. 门禁证据

本 reviewer 在固定候选实际运行：

```text
/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q \
  tests/workflow/test_route_body_budget.py \
  tests/workflow/test_json_resource_budget.py \
  tests/workflow/test_schema_codec_hardening.py \
  tests/workflow/test_value_schema_hardening.py \
  tests/workflow/test_value_schema_v1.py
=> 212 passed, 2 warnings

/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q tests/workflow
=> 644 passed, 3 warnings

/home/changjunhan/.micromamba/envs/unilab/bin/python -m ruff check \
  --select E,F,I --ignore E501 \
  unilabos/app/workflow_api.py \
  unilabos/workflow/json_codec.py \
  unilabos/workflow/schema.py \
  tests/workflow/test_route_body_budget.py \
  tests/workflow/test_json_resource_budget.py \
  tests/workflow/test_schema_codec_hardening.py \
  tests/workflow/test_value_schema_hardening.py \
  tests/workflow/test_value_schema_v1.py
=> All checks passed

/home/changjunhan/.micromamba/envs/unilab/bin/python -m ruff format --check \
  <上述 8 个 production/test 文件>
=> 8 files already formatted

git diff --check 1111380...a54c402
=> passed

git diff --check 1111380...5c8e47f
=> passed
```

本次按任务要求没有重复正式 `pytest tests -q`；趋势报告已登记 production/test
候选 `a54c402` 为 `1056 passed, 3 skipped`。

另运行只读对抗，覆盖：

1. 当前 16 条 route 的 class、`body_field`、body params 与 OpenAPI；
2. dependency-declared body shape；
3. JSON 与 bytes body 的 Pydantic/cache/stream 一致性；
4. 业务 `ValueError` 与 budget `ValueError` 的异常隔离；
5. 当前 FastAPI/Starlette cache Implementation。

没有修改 production、测试、其他文档、Backend 或前端。

## 8. 合并门禁

当前行为候选的 B-01～B-07 和所有自动门禁均通过，但 Standards blocking 数为
**1**。固定候选 `5c8e47f` **不允许**直接作为完整 02A integration 合并候选。

合并前必须：

1. 把本轮修改的 `_BackendJSONRoute` class docstring 改为准确的简体中文；
2. 以新的 production SHA 重跑目标、Workflow、正式测试和静态门禁；
3. 三名 reviewer 按迁移规则针对新 SHA 顺序确认，且不得重开 B-01～B-07。

NB-M01、NB-M02 和 NB-01 是明确的后续守护，不要求在该最小 Standards 修复中扩大
production 设计。只有新 SHA 的三名 reviewer 全部通过，完整 02A 才可以合并到
`integration/workflow-task-runtime`。
