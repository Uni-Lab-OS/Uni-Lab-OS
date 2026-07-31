# Phase 02A4：Route body budget 模块安全顺序复验

日期：2026-07-31

评审分支：`review/02a4-module-safety-confirm`

固定评审候选：`ac45c32a3cedbdf7db7eb7289eb03a4124ad3f57`

固定 production：`c0d5874c35230608eb77f22475bf5ff061de5981`

评审角色：新 production SHA 顺序复验 3/3。本报告只新增评审文档，不修改
production、测试、其他文档、Backend 或前端。

## 1. 结论

**Blocking 数为 0；S-03 已准确修复，允许完整 02A 进入 integration 合并候选。**

| 评审轴 | Blocking | Non-blocking | 结论 |
|---|---:|---:|---|
| Module / safety | 0 | 3 | 通过 |
| Repository Standards | 0 | 0 | 通过 |

本次固定候选在固定 production 之后只新增前两名 reviewer 的确认文档，没有
production 或测试差异。B-01～B-07 均保持 `accepted-fixed`；NB-M01、NB-M02 和
NB-01 仍是明确的后续守护，不阻塞 02A 合并。

## 2. S-03 确认

相对上一 production/test 候选 `a54c402c5f18f53dff0abe98fe8aa0f89c3686d0`，
`c0d5874` 只修改 `_BackendJSONRoute` 的一行 class docstring：

```diff
-    """Bound body routes, then preload JSON with the frozen Backend rules."""
+    """限制有请求体的路由，再按冻结 Backend 规则预载 JSON。"""
```

新文本符合 `AGENTS.md`“Code comments and log messages in simplified Chinese”的
仓库规范，也准确描述现有 Interface：

1. 只对 `body_field is not None` 的有请求体路由执行读取预算；
2. JSON MIME 在受限读取后使用冻结 decoder 预载；
3. 无请求体的 GET、DELETE 和 SSE 不预读。

运行时直接读取 `_BackendJSONRoute.__doc__` 得到同一简体中文文本。该修复没有修改
`get_route_handler()`、`_read_limited_body()`、MIME 判断、Request cache、异常映射
或业务调用顺序，因此 S-03 为 `accepted-fixed`，没有产生新的 Standards finding。

## 3. Module / safety 复验

### Route-local seam

`_BackendJSONRoute` 仍是一个小 Interface 后隐藏路由形状判断、8 MiB declared/actual
预算、MIME 分层、冻结 JSON decoder、Request cache 和错误信封映射的 FastAPI
Adapter。删除它会把同一规则散回六条 body route；当前也没有第二种 Adapter 需要
抽取新的公共 port。原“route-local 实现过浅”疑虑继续为
`rejected-with-evidence`。

动态盘点固定候选：

| Route shape | 数量 | 结果 |
|---|---:|---|
| `_BackendJSONRoute` 总数 | 16 | 全部 Workflow route 使用同一 Adapter |
| `body_field` 非空 | 6 | POST/PUT body route 均进入预算 gate |
| `body_field` 为空 | 10 | GET/DELETE/SSE 不预读 |

六条 body route 仍为 Workflow 创建、Workflow 更新、Graph 更新、Task 创建、
Draft PUT 和 Apply POST。修复没有造成未来 route leakage 或当前 route 漏接。

### FastAPI 私有状态与异常隔离

`APIRoute.body_field`、`request._body` 和 `request._json` 的使用没有变化。既有
JSON/bytes cache、重复读取、stream 消费和 raw ASGI 回归仍全部通过；本次文案修复
不可能改变 FastAPI/Starlette 的 Request 状态。

预算 reader/decoder 的 `try/except` 范围也没有变化，业务
`await route_handler(request)` 仍位于预算异常捕获之外。业务 `ValueError` 不会被
误写为 `400 invalid_input`，transport failure 与业务 failure 继续隔离。

因此原模块安全 finding M-01～M-04 均保持 `rejected-with-evidence`。

## 4. 完整 02A finding disposition

| Finding | Disposition | 本次证据 |
|---|---|---|
| B-01 canonical value 可破坏 | `accepted-fixed` | Schema/codec 回归保持通过 |
| B-02 嵌套 nullable | `accepted-fixed` | finite grammar/path 回归保持通过 |
| B-03 深层 JSON 递归异常 | `accepted-fixed` | 10000/10001 临界回归保持通过 |
| B-04 合法大整数裸异常 | `accepted-fixed` | trusted 大整数 codec 回归保持通过 |
| B-05 不可信大整数无预算 | `accepted-fixed` | 4096/4097 预算分界保持通过 |
| B-06 wrapper 深度漏计 | `accepted-fixed` | 完整值深度回归保持通过 |
| B-07 MIME 可关闭 body budget | `accepted-fixed` | 六条 body route 继续使用同一 byte gate |
| S-03 新改 docstring 非简体中文 | `accepted-fixed` | 新文本为准确的简体中文 |

production/test 行为相对 `a54c402` 只有上述 docstring 文案变化，没有证据支持重开
B-01～B-07。

## 5. Non-blocking follow-up

以下三项 disposition 均不变：

- **NB-M01：框架内部约定需要升级门禁。** FastAPI/Starlette 依赖升级时继续运行
  route shape、JSON/bytes cache 和 bodyless route 回归；当前版本及现有测试已证明
  兼容，不要求复制一层虚假框架抽象。
- **NB-M02：未来新 router 的覆盖需要动态守护。** D-117 顶层 Authoring route
  实现时，增加 OpenAPI `requestBody` route 与预算 Adapter 的动态一致性测试；
  当前 16 条 route 没有 leakage。
- **NB-01：Content-Length 词法偏宽。** `int(value, 10)` 的解析实现未变，实际
  stream 预算与 h11 停止线未变；非 h11 transport 的词法收紧仍可独立处理。

三项均是 `non-blocking-follow-up`，本次简体中文修复不要求扩大 production 设计。

## 6. 门禁证据

本 reviewer 在固定评审候选上实际运行：

```text
/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q \
  tests/workflow/test_route_body_budget.py \
  tests/workflow/test_json_resource_budget.py \
  tests/workflow/test_schema_codec_hardening.py \
  tests/workflow/test_value_schema_hardening.py \
  tests/workflow/test_value_schema_v1.py
=> 212 passed, 2 warnings in 2.30s

/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q tests/workflow
=> 644 passed, 4 warnings in 27.95s

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

git diff --check 1111380...c0d5874
=> passed

git diff --check c0d5874^...c0d5874
=> passed
```

另执行只读 route 盘点与 docstring 运行时断言：

```text
routes=16, body=6, bodyless=10
限制有请求体的路由，再按冻结 Backend 规则预载 JSON。
```

主执行者和前序 reviewer 已在同一固定 production 登记正式完整测试
`1056 passed, 3 skipped`。本次没有行为变化或可疑信号，因此不重复该正式全量。

## 7. 合并门禁

顺序复验 1/3、2/3 和本次 3/3 均固定到同一 production
`c0d5874c35230608eb77f22475bf5ff061de5981`，当前 blocking 总数为 **0**。

**允许完整 02A 进入 `integration/workflow-task-runtime` 合并候选。** 合并时必须保留
迁移 provenance，并确认候选只增加三份顺序复验报告；若在合并前再次修改
production 或测试，则本确认失效，必须针对新 SHA 重跑相关门禁与顺序评审。
