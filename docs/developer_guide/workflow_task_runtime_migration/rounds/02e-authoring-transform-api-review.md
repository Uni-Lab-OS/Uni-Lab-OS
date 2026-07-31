# Round 02E：纯 Authoring HTTP Interface 独立评审

## 1. 固定对象与结论

- 基线：`2aa859535bd75e776080e8c4247e41f0b21e0cec`
- 被评审且已测试的候选：`583f6df462d21367a763f33062eb29e1f9166d42`
- 比较命令：`git diff 2aa8595...583f6df`
- reviewer：`round02e_review`；未参与 02E 实现或独立测试编写。
- 轮换视角：module design + regression/security，同时保持 Standards 与 Spec 两轴独立。
- 结论：**暂不允许合并**。Standards 有 1 个 blocking、1 个 non-blocking；Spec 有
  1 个 blocking。blocking 同源：成功响应没有对 Candidate graph 及其 bundle 做
  Backend-shaped、identity/semantic fail-closed 验证。修复后必须形成新候选 SHA、重跑
  完整 round gate，并由同一 reviewer 确认。

除该阻塞外，三条路由保持薄且纯，没有接入 `WorkflowService`、Draft/Apply、Task/Job、
Scheduler、device 或 Backend；02G production composition 也没有提前混入。D-102 的
AST、`tokenize`、`SyntaxError`、renderer 与 persistent Service 已统一消费一个 UTF-16
坐标模块。此次 diff 没有修改 Backend 或 Frontend。

## 2. 验证证据

| 检查 | 结果 |
|---|---|
| `pytest -q tests/app/test_authoring_transform_api.py` | `31 passed, 2 warnings in 1.45s` |
| 02E + 02D engine/round-trip/review regression + D-102 历史契约目标 | `105 passed, 1 warning in 2.45s` |
| `ruff check --select E,F,I`（本轮 production/tests） | 通过 |
| `git diff --check 2aa8595...583f6df` | 通过 |

105 项命令为：

```text
/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q \
  tests/app/test_authoring_transform_api.py \
  tests/workflow/test_authoring_engine.py \
  tests/workflow/test_authoring_roundtrip.py \
  tests/workflow/test_authoring_engine_review_regressions.py \
  tests/workflow/test_phase01_review_contract_round14_followup.py::test_source_map_uses_one_based_utf16_code_unit_columns
```

warning 仅为既有 FastAPI TestClient/httpx 弃用提示及一次既有 escape-sequence 提示，不是
本轮失败。

另执行了不写文件的最小 probe：让注入 engine 返回结构合法的
`CandidateCompilation`，但令 `graph={"canonical_private": ...}`；另一次在五集合
`graph.workflow` 中加入 `canonical_private`。两次响应均为 HTTP 200，私有字段原样出现在
`data.graph`。真实 02D engine 的 compile 路径也会保留 `applied_graph.workflow` 的未知
字段并返回 HTTP 200，故这不是只存在于虚假 mock 的理论问题。

## 3. Standards 轴

### 3.1 Blocking

#### S-B01：公开 Candidate graph 没有执行仓库要求的 Backend-shaped fail-closed 边界

- 规则：`AGENTS.md:991-995` 要求 pure transform 使用 Backend-shaped wire models，且
  Internal Canonical models 不得泄漏；02E 设计 `:81-88, 109-120` 要求 Backend-shaped
  五集合并把非法出站结果收敛为 sanitized `500 internal_error`。
- 位置：`unilabos/app/workflow_api.py:287-355`。`_transform_data()` 对 diagnostic、
  source map、changeset 使用 closed Pydantic DTO，但对 `compilation.graph` 只调用
  `validate_json_value(graph)`，随后在 `:347-355` 原样返回。
- 复现结果：非五集合 graph、错误 Workflow identity/revision、未知 Workflow/Node 私有
  字段、悬空 source-map Node identity 或与 graph 不一致的 graph changeset 都没有在此
  seam 被证明。最小 probe 已实证前两类 graph 以 HTTP 200 原样出站。
- 影响：一个 engine bug、未来 adapter 或内部 Canonical 字段都可能成为稳定 wire 字段；
  FE/CLI/MCP 也可能把并非请求 Workflow 的 graph 当成成功 Candidate。HTTP seam 对其他
  Candidate 字段 fail-closed，却在最重要的 graph/identity 上 fail-open。
- 最小修复：抽取一个 transport-independent 的 Backend Candidate bundle validator，至少
  关闭五集合及各实体 read DTO、校验请求 `workflow_uuid/revision`、Catalog projection、
  source-map Node 引用和 changeset 与输入/候选 graph 的精确关系。三条 handler 把请求
  identity 及对应基图传入该 validator；非法 engine 输出统一 sanitized 500。不要让
  pure router 依赖 `WorkflowService` 的私有方法。

### 3.2 Non-blocking

#### S-NB01：Candidate 出站验证在 HTTP adapter 与 persistent Service 形成两套近似实现

- 位置：`workflow_api.py:269-355` 与 `service.py:1768-1801, 1855-1885`。
- 证据：diagnostic range 枚举、source-map/changeset validation 已复制；persistent
  Service 还会执行 `_backend_candidate_graph()` 和
  `_validate_candidate_bundle_semantics()`，pure adapter 则缺少这两步。S-B01 正是两套
  逻辑漂移后的行为差异。
- 判断：这是 **Duplicated Code / Shotgun Surgery** 的 judgement-call smell，不单独增加
  blocking 计数；建议在修复 S-B01 时把共同纯验证下沉为一个 deep module，由 persistent
  与 pure 两条 seam 复用。

## 4. Spec 轴

### 4.1 Blocking

#### P-B01：非法成功 bundle 未按 02E 冻结合同转为 500

- 规范：02E 设计 `:81-88` 要求公开 graph 为 Backend-shaped 五集合，`:109-120` 要求
  非法出站结果为 `500 internal_error`；D-040 要求返回 Backend-shaped Candidate，而不是
  Canonical-only wire contract。
- 代码/测试证据：`workflow_api.py:328` 仅证明 graph 是 JSON；
  `tests/app/test_authoring_transform_api.py:460-477` 只覆盖 open diagnostic DTO 与非法
  range，没有覆盖 graph 形状/identity、source-map membership 或 changeset consistency。
- 实际行为：`graph={"canonical_private": ...}` 和含未知 Workflow 字段的五集合 graph
  都得到 `200 {"code": 0, ...}`，而不是冻结的 500 envelope。
- 验收修复：由原 test-author 增加 RED，至少覆盖非五集合/未知字段、错误
  workflow UUID/revision、source map 指向候选外 Node、changeset 生命周期与候选不一致；
  修复后这些非法 engine 结果必须统一为不泄漏原值的 500。well-formed 请求自身的
  semantic graph 错误仍应由真实 engine 返回 HTTP 200 diagnostic，不能误改为请求 400。

### 4.2 Non-blocking

无。

## 5. 已确认通过的边界

1. Router 只有 compile、generate-python、validate 三个 POST；每次只调用对应 engine
   一次，不签发 `candidate_hash`，也没有 Draft/Apply route。
2. closed request body、非 nil UUID、严格正整数 revision、UTF-8 source/source URI、
   8 MiB body、4096 位整数与完整 JSON 深度预算均有绿灯证据。
3. syntax/semantic/catalog mismatch 保持 HTTP 200 diagnostic；Catalog unavailable 为
   503；未预期异常的 HTTP body 为固定 500 envelope，未回显异常文本。
4. 真实 engine 的 generate→compile→validate 保持 Node UUID 与 normalized source，并且
   指定持久表行数不变。
5. `source_coordinates.py` 是小而纯的单一换算模块：AST 的 UTF-8 byte offset、
   `tokenize`/`SyntaxError` 的 code-point offset、renderer 与 Service range check 都以
   一基、end-exclusive UTF-16 code unit 公开；中文、emoji、tab、duplicate repair 与
   HTTP source-map 回归通过。历史 UTF-8 列测试改为 UTF-16 是 D-102 的正当迁移，不是
   弱化测试。
6. diff 不含 production server composition、Store schema、Catalog import/sync、
   persistent Apply、Task/Job/device、Backend 或 Frontend 文件；02G 与 FE 独立分支边界
   保持。

## 6. 复审门

当前 `583f6df` **不得合并**。修复 S-B01/P-B01 后：

1. 由原 02E test-author 提交上述独立 RED；
2. 形成新的 production 候选 SHA；
3. 重跑 02E 目标、相关 105 项、完整 `tests/`、configured Ruff/format 与
   `git diff --check`；
4. 由同一 `round02e_review` reviewer 对精确新 SHA 确认 blocking 关闭；
5. 确认过程中不得新增 02G composition，也不得修改 Backend/Frontend。

