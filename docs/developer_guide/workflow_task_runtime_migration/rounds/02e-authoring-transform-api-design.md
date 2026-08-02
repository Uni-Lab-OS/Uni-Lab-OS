# Round 02E：纯 Authoring HTTP Interface 设计冻结

日期：2026-07-31

实现分支：`migration/02e-authoring-transform-api`

基线：`2aa8595`（Round 02D 合并后的 `integration/workflow-task-runtime`）

## 1. 目标和停止线

02E 只把 02D 的唯一 `WorkflowAuthoringEngine` 包成三条 OS-only、无持久副作用的
HTTP Interface：

```text
POST /api/v1/authoring/compile
POST /api/v1/authoring/generate-python
POST /api/v1/authoring/validate
```

Router 直接依赖一个注入的 transform engine，不通过 `WorkflowService`，不读取或写入
Draft，不调用 graph save，不创建 Task/Job，不接触 Scheduler/device，也不访问 Backend。
`workflow_api.py` 提供可单独安装的 router/app seam；把真实 Catalog/Authority engine
装入进程级 server 属于后续 composition 切片，02E 不提前恢复 02G persistent wiring。

本轮允许修改：

- `unilabos/app/workflow_api.py`；
- 为关闭 D-102 而修改 Authoring source coordinate helper 及 Service range validation；
- `tests/app/test_authoring_transform_api.py`；
- 本设计、评审与趋势文档。

本轮禁止修改 Backend、Frontend、persistent Draft/Apply 语义、Store schema、Catalog
import/sync、Task/Job 或 device dispatch。

## 2. 请求 DTO

三种 body 都是 closed object（未知字段为 malformed request），继续受 D-101 的 8 MiB
body、4096 位外部整数与完整 JSON 深度预算约束。

### 2.1 Compile

```json
{
  "workflow_uuid": "<uuid>",
  "revision": 7,
  "python_source": "...",
  "source_uri": "package://pkg/workflows/demo.py",
  "applied_graph": {
    "workflow": {},
    "nodes": [],
    "edges": [],
    "node_templates": [],
    "handle_templates": []
  }
}
```

### 2.2 Generate Python

```json
{
  "workflow_uuid": "<uuid>",
  "revision": 7,
  "graph": {},
  "source_uri": "package://pkg/workflows/demo.py"
}
```

### 2.3 Validate

```json
{
  "workflow_uuid": "<uuid>",
  "revision": 7,
  "graph": {},
  "python_source": "...",
  "source_uri": "package://pkg/workflows/demo.py"
}
```

公开字段使用 `workflow_uuid`、Backend `revision`、Node/Edge `uuid` 及 Backend-shaped
五集合 graph。禁止 `workflow_id`、`base_revision_id`、`node_id`、Canonical IR，且三条
pure request/response 都没有 `candidate_hash`、Draft hash 或 Apply token。

外层 graph 字段必须是 JSON object；五集合、Workflow identity、Node/Edge/Template
结构属于 well-formed transform 的 semantic validation，由 02D engine 返回 diagnostic，
而不是让 FastAPI 先返回裸 422。`workflow_uuid` 必须为非 nil UUID；`revision` 必须为
严格正整数；`source_uri` 必须是非空字符串；源码必须可无损 UTF-8 编码。

## 3. 成功 DTO 与错误边界

每个 well-formed 请求均返回 HTTP 200：

```json
{
  "code": 0,
  "data": {
    "diagnostics": [],
    "graph": {},
    "normalized_python_source": "...",
    "source_map": [],
    "changeset": {},
    "compiler_version": "...",
    "template_catalog_fingerprint": "sha256:..."
  }
}
```

语法、semantic、identity mismatch 或 round-trip 失败仍是成功 envelope 中的结构化
diagnostic；失败结果的 `graph`、`normalized_python_source`、`changeset` 为 `null`，
`source_map` 与所有 collection 为 `[]`。Adapter 在出站前用
`CandidateDiagnostic`、`CandidateSourceMapEntry`、`CandidateChangeset` 验证 engine
结果，内部对象或非法 range 不得泄漏为 wire payload。

只有以下情况使用错误 envelope：

- malformed JSON、body budget、缺字段、未知字段、类型/UUID/revision/source UTF-8
  错误：HTTP 400 `invalid_input`；
- Catalog snapshot 不可用：HTTP 503 `template_catalog_unavailable`；
- engine 抛出的未预期基础设施失败或非法出站结果：HTTP 500 `internal_error`。

`template_catalog_mismatch` 若由一个已成功取得的 snapshot 对请求 graph/source 的合同
检查产生，仍是 HTTP 200 diagnostic。Router 不把普通 compilation diagnostics 错误
升级为 422；D-079 的 `draft_invalid/candidate_invalid` 422 属于 persistent Apply。

## 4. D-102 source coordinate

本轮关闭 02D 遗留的 P-NB01：所有 diagnostic、duplicate-anchor repair 与 source map
统一为一基、end-exclusive 的 UTF-16 code-unit 坐标。

- `a中b` 的行尾 end column 为 `4`；
- `a😀b` 的行尾 end column 为 `5`，因为 emoji 占两个 UTF-16 code unit；
- tab 只占一个单位；
- AST UTF-8 byte offset、tokenize code-point offset、SyntaxError offset 与 renderer
  都必须通过同一个源码坐标模块转换；
- Service 的范围边界检查也按 UTF-16，而不是 UTF-8 byte length；
- 非 BMP emoji 前后的 diagnostic 和 normalized source map 必须有公开 HTTP 回归。

不在 HTTP adapter 做结果临时换算，否则 persistent aggregate 与 pure route 会再次形成
两套坐标。实现应提供一个小而纯的 `source_coordinates` deep module，compiler 与
Service 共同消费。

## 5. 纯度与注入

新增 `create_authoring_transform_router(engine)` 与 focused test/app seam。现有
`create_workflow_router(service)` 不获得 transform 行为；这样测试能用真实 02D engine
加 read-only Catalog snapshot 验证，同时能用 spy engine 证明 handler 不接触
`WorkflowService`。

每次请求只调用对应 engine 方法一次：

| Route | 唯一 engine 调用 |
|---|---|
| compile | `compile(workflow_uuid, workflow_revision=revision, python_source, source_uri, applied_graph)` |
| generate-python | `generate_python(workflow_uuid, workflow_revision=revision, graph, source_uri)` |
| validate | `validate(workflow_uuid, workflow_revision=revision, graph, python_source, source_uri)` |

Router 不缓存 Candidate、不签发 `candidate_hash`，所以 pure route 返回的 graph 不能替代
server-owned persistent Candidate，也不能直接调用 Apply。

## 6. TDD 与门禁

唯一独立 test-author 在独立 `test/02e-authoring-transform-api` 分支先提交
`tests/app/test_authoring_transform_api.py`，至少证明：

1. 三条 path、closed request DTO、Backend identity 与统一 envelope；
2. syntax/semantic diagnostic 是 HTTP 200，malformed request 是 400；
3. unavailable Catalog 是 503，内部异常是 500 且不泄漏文本；
4. 对 Graph、Draft、Task、Job、Store 和 device dispatch 无写副作用；
5. response 无 Candidate hash/Apply token，且不能作为 persistent Apply request；
6. generate→compile→validate 对 proof-equivalent graph 保持 UUID 和 normalized source；
7. 中文、emoji、tab 下 diagnostic/source-map 为 D-102 UTF-16 坐标；
8. D-101 body/integer/depth预算仍作用于 `/authoring/*`。

实现完成后运行本轮 tests、`tests/workflow`、完整 `tests/`、Ruff E/F/I、对新增文件
完整 Ruff、format 与 `git diff --check`。固定全绿 SHA 后只启用一个未参与实现/测试的
review subagent，按 Standards 与 Spec 双轴评审；blocking 关闭并由同一 reviewer
确认后才能非 squash 合入 integration。

02E 合并后，另开 Frontend 分支实现单编辑权按钮切换和 pure transform contract；OS
与 FE 分支不混合，Backend 继续保持只读且不修改。
