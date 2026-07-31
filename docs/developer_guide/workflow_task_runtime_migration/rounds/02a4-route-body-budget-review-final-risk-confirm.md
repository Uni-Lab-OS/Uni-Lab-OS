# Phase 02A4：Route body budget 最终风险确认

日期：2026-07-31

评审分支：`review/02a4-final-risk-confirm`

固定候选：`c0d5874c35230608eb77f22475bf5ff061de5981`

评审角色：新 production SHA 顺序复验 1/3。本报告只新增评审文档，不修改
production、测试、其他文档、Backend 或前端。

## 1. 结论

**Blocking 数为 0；固定候选可以进入顺序复验 2/3。**

| Finding | Disposition | 证据 |
|---|---|---|
| B-07 MIME 可关闭 body budget | `accepted-fixed` | 212 项 route/Schema/资源测试全绿；完整行为 diff 未变化 |
| S-03 新改 class docstring 非简体中文 | `accepted-fixed` | docstring 已准确改为简体中文 |
| NB-01 Content-Length 词法偏宽 | `non-blocking-follow-up` | 解析实现未变，既有实际 stream 与 h11 停止线未变 |

## 2. 新 production SHA 核对

相对上一固定行为候选 `edef8003cabf246d89d45340ba94961f63b8a4b4`，
`c0d5874` 的 production diff 只有一行：

```diff
-    """Bound body routes, then preload JSON with the frozen Backend rules."""
+    """限制有请求体的路由，再按冻结 Backend 规则预载 JSON。"""
```

该文本准确描述现有 Interface：

- 先按 `body_field` 限制有请求体的 route；
- 再只对 JSON MIME 使用冻结 decoder；
- 无 request body 的 GET/SSE 不预读。

没有修改 `expects_body`、MIME 判断、8 MiB 常量、stream reader、Request cache、
JSON integer/depth budget、异常 envelope 或业务调用顺序。因此上一轮 B-07 的
`accepted-fixed` 证据仍成立。

S-03 要求本轮新改注释/docstring 使用简体中文。新文本已满足
`AGENTS.md` 的仓库规则，且没有混入行为改动，故为 `accepted-fixed`。

## 3. 门禁

本 reviewer 在精确候选实际运行：

```text
/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q \
  tests/workflow/test_route_body_budget.py \
  tests/workflow/test_json_resource_budget.py \
  tests/workflow/test_schema_codec_hardening.py \
  tests/workflow/test_value_schema_hardening.py \
  tests/workflow/test_value_schema_v1.py
=> 212 passed, 2 warnings

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

git diff --check 1111380...c0d5874
=> passed

git diff --check edef800...c0d5874
=> passed
```

主执行者已在同一固定 production SHA 登记：

```text
Workflow：644 passed
正式完整 tests：1056 passed, 3 skipped
```

本次复验没有发现可疑行为变化，因此不重复正式全量。

## 4. 下一步

固定候选 `c0d5874c35230608eb77f22475bf5ff061de5981` 的 B-07 与 S-03 均为
`accepted-fixed`，没有新 finding。允许进入独立顺序复验 2/3；后续 reviewer
仍必须固定到同一 production SHA，任何 production 改动都会使本确认失效。
