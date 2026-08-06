# Phase 02A4：Route body budget 契约顺序复验

日期：2026-07-31

评审分支：`review/02a4-contract-confirm`

固定评审候选：`4cf570cabb4eb840265478e2edff85e1a833d93c`

固定 production：`c0d5874c35230608eb77f22475bf5ff061de5981`

评审角色：新 production SHA 顺序复验 2/3。本次只新增本报告，不修改
production、测试、其他文档、Backend 或前端。

## 结论

**Blocking 数为 0；S-03 已关闭，固定 production 可以进入顺序复验 3/3。**

| 评审轴 | Finding | Disposition |
|---|---|---|
| Repository Standards | S-03 新改 class docstring 非简体中文 | `accepted-fixed` |
| D-101 契约 | body route 预算、JSON 分层和错误信封回归 | `rejected-with-evidence` |
| D-117 契约 | Apply 或 Authoring 公共边界回归 | `rejected-with-evidence` |

## Production 差异

`c0d5874^...c0d5874` 的 production/test 差异只有
`unilabos/app/workflow_api.py` 一行 docstring：

```diff
-    """Bound body routes, then preload JSON with the frozen Backend rules."""
+    """限制有请求体的路由，再按冻结 Backend 规则预载 JSON。"""
```

新文本准确描述现有 Adapter，且满足 `AGENTS.md` 的简体中文规则，S-03 因此为
`accepted-fixed`。测试文件没有变化。

重新检查 `1111380...c0d5874` 的完整合同 diff 后确认，这一行文字修正没有改变：

- D-101 的 `body_field is not None` 路由边界；
- 8 MiB 原始字节预算先于 JSON/业务校验的顺序；
- 仅对 `application/json` 和 `+json` 使用冻结受限 decoder；
- `400 invalid_input` 错误信封和失败时不进入 service 的约束；
- 无请求体 GET/SSE 不由路由包装器预读的行为；
- D-117 的 Draft PUT 双 CAS；
- `_StrictModel` 下只含 `candidate_hash` 的 `ApplyRequest`，没有客户端 Candidate、
  Draft hash 或 Workflow revision；
- 当前持久 Workflow-scoped Authoring 路径及尚未实现的纯顶层 Authoring Interface。

因此没有新增、删除或改变 route、DTO、校验、缓存、异常处理、业务调用及
Backend/FE 边界。

## 门禁

在固定评审候选上运行；`c0d5874..4cf570c` 对 production/test 无差异：

```text
/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q \
  tests/workflow/test_route_body_budget.py \
  tests/workflow/test_json_resource_budget.py
=> 39 passed, 2 warnings in 2.24s

/home/changjunhan/.micromamba/envs/unilab/bin/python -m ruff check \
  --select E,F,I --ignore E501 \
  unilabos/app/workflow_api.py \
  tests/workflow/test_route_body_budget.py \
  tests/workflow/test_json_resource_budget.py
=> All checks passed

/home/changjunhan/.micromamba/envs/unilab/bin/python -m ruff format --check \
  unilabos/app/workflow_api.py \
  tests/workflow/test_route_body_budget.py \
  tests/workflow/test_json_resource_budget.py
=> 3 files already formatted

git diff --check 1111380...c0d5874
=> passed

git diff --check c0d5874^...c0d5874
=> passed
```

本次是文案修正后的顺序复验，不重复已登记的正式全量测试。

## 下一步

固定 production `c0d5874c35230608eb77f22475bf5ff061de5981` 仍为 0 blocking，
可以进入独立顺序复验 3/3。后续任何 production 或测试改动都必须产生新 SHA，并使
本确认失效。
