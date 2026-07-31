# Phase 01 收尾与 Phase 02 Authoring/Schema 具体执行计划

## 结论

现在可以直接执行本计划。

P0-1 已由 D-073～D-081 关闭，P0-2 已由 D-082～D-092 关闭，P0-3 已由
D-093～D-099 关闭。三者已经足以
支持：

1. 完成 Phase 01 剩余的冻结 Backend-shaped Node/Edge 和 Task command 接口；
2. 实现 version 1 Workflow Schema、严格值验证和共享 annotation parser；
3. 将老 OS 的 Python/Canonical 编写能力语义迁移为 Backend-shaped
   Authoring engine；
4. 接通三个纯 Authoring 路由和 Workflow-scoped 持久 Authoring；
5. 注册具体 package source，并在启动和文件变化时执行恢复/重新编译。

本计划不得越过以下尚未关闭的停止线：

- P0-4 关闭前，不发布 action-side ResourceSlot Handle 或隐式输出；
- P0-5 关闭前，不承诺最终 `WorkflowTask.output` 的外部 ResourceSlot JSON、
  失败 Task partial result 或 REST/SSE 投影；
- P1-1/P1-2/P2 仍分别约束完整 debugger、Conditional Join 和
  `tool_call` executor。

## 1. 每轮分支、测试与评审硬门禁

一轮等于一个可以独立合并的实现切片。每轮必须从最新
`integration/workflow-task-runtime` 新建分支，禁止在尚未合并的上一轮分支上
继续叠加下一轮。

### 1.1 分支规则

| 轮次 | 实现分支 |
|---|---|
| 当前 Phase 01 core | 现有 `migration/01-backend-contract`；只做补测、修复和评审，不再加入 01E |
| 01E | `migration/01e-node-edge-admin` |
| 01F | `migration/01f-task-commands` |
| 01G | `migration/01g-phase-closeout` |
| 02A | `migration/02a-schema-v1` |
| 02B | `migration/02b-annotation-schema` |
| 02C | `migration/02c-template-catalog` |
| 02D | `migration/02d-authoring-engine` |
| 02E | `migration/02e-authoring-transform-api` |
| 02F | `migration/02f-source-lifecycle` |
| 02G | `migration/02g-persistent-authoring` |
| 02H | `migration/02h-task-input-preflight` |

每一轮的基点必须是上一轮已经通过门禁并合入 integration 后的 commit。
测试 subagent 使用独立 worktree 和独立测试分支：

```text
test/<round>-contract
test/<round>-adversarial
```

禁止多个 subagent 在同一个工作目录并发编辑同一批文件。测试 commit 合入实现分支
时保留作者和 commit provenance，不 squash。

### 1.2 独立测试 subagent

每轮至少安排两名互相独立的测试作者，且测试先于实现：

1. **合同测试 subagent**：只依据冻结 Backend、`decisions.md` 和该轮验收条件，
   编写 route/DTO/state/transaction 合同测试；
2. **对抗与回归测试 subagent**：独立检查高京现状和老 OS 行为，编写 conflict、
   restart、atomicity、invalid input、旧 alias 拒绝和附近模块回归测试。

要求：

- 两名测试作者使用不同 worktree，不共享未提交测试实现；
- 每组新测试必须先在未实现状态下按预期失败；
- 测试失败原因必须是缺失行为，而不是 import、fixture 或环境错误；
- 实现阶段不得为了变绿而削弱断言、删除测试、添加 skip/xfail；
- 若两组测试互相冲突，先依据决策或冻结 Backend 解决合同冲突，再开始实现。

### 1.3 全量测试门

实现完成后，主执行者必须在同一个候选 commit 上依次运行：

1. 该轮 targeted tests；
2. Phase 累积测试；
3. 完整仓库 test suite；
4. 配置的 lint、静态检查和 `git diff --check`。

“测试通过”只指以上全部通过。只运行新测试或只运行目标目录不能作为合并证据。

### 1.4 多 subagent 独立评审

全量测试通过后，锁定候选 commit SHA，再安排至少三名没有编写本轮实现的独立
review subagent：

1. **合同评审**：检查冻结 Backend、P0/P1/P2 决策、路由、DTO、状态机和错误边界；
2. **代码与模块评审**：检查仓库规范、deep module Interface、重复逻辑、可测试性
   和维护 locality；
3. **回归与安全评审**：检查 transaction、CAS、restart、recovery、并发、
   filesystem containment、数据迁移和旧路径泄漏。

评审者必须同时阅读 production diff 和 tests，不能只认可测试输出。每项 finding
必须记录为：

```text
accepted-fixed | rejected-with-evidence | non-blocking-follow-up
```

以下条件之一发生时，原评审失效并必须重跑相关评审：

- 候选 SHA 改变；
- production code 改变；
- 为修复 finding 修改了相关测试；
- merge 前 rebase 改变了实际 diff。

### 1.5 合并条件

只有全部满足才能本地 merge 到 `integration/workflow-task-runtime`：

- 两名或以上独立测试 subagent 的测试已纳入；
- targeted、Phase 累积和完整仓库测试全部通过；
- lint、静态检查和 `git diff --check` 通过；
- 三名或以上独立 review subagent 完成评审；
- 所有 blocking finding 已修复并复审；
- migration ledger 记录 branch、tested SHA、测试命令/结果、测试作者、评审者和
  finding disposition；
- merge 保留 reviewable commits，不 squash；
- 未经明确授权不 push。

当前 `migration/01-backend-contract` 也必须补齐这套门禁后才能合并。它通过并合入
integration 之前，不创建 01E 实现分支。

每轮开始时复制
[迁移轮次门禁记录模板](round-gate-template.md)，形成
`rounds/<round>-<topic>.md`；测试、评审和合并证据随轮次持续填写。

## 2. 计划依据

本计划不是根据单一新设计文档推导，而是对以下四类实现事实进行交叉检查后形成：

| 依据 | 已检查内容 | 在计划中的作用 |
|---|---|---|
| 高京目标 OS `/home/gaojing/Uni-Lab-OS` | 当前 `workflow.db`、`WorkflowService`、FastAPI adapter、scheduler composition 和新测试 | 确定已有实现、实际缺口和继续深化的位置 |
| 老 OS `/home/changjunhan/Uni-Lab-Core/Uni-Lab-OS` | `authoring.py`、`canonical*.py`、`from_python_script.py`、`dag_compile.py`、Registry parser、local bridge/runtime 和测试 | 提取 AST-only 编译、source map、参数、运行前验证和恢复行为；不复制旧 wire model |
| 老 FE `/home/changjunhan/Uni-Lab-Core/uni-lab-fe` | Workflow service port、`WorkflowPanel`、Canonical projection、Run WebSocket 和 E2E | 确定后续 FE 消费需求及不得恢复的旧交互 |
| 冻结 Backend `09609a2` | Workflow/Node/Edge/Task/Job route、DTO、envelope、revision 和 Task command 行为 | 共享前端 Interface 的合同权威 |

详细旧 FE–OS 处置见
[旧版 FE–OS 交互迁移矩阵](fe_os_interaction_migration_matrix.md)。

## 3. 当前实现基线

目标分支：`migration/01-backend-contract`。

当前已经实现：

- `workflow`、`workflow_node`、`workflow_edge`、
  `workflow_node_template`、`workflow_handle_template`；
- `workflow_task`、`workflow_node_job`、不可变 Task snapshot 和 Job 预创建；
- Graph GET 和 revision-guarded full PUT；
- Workflow-scoped Authoring GET、Draft double CAS 和 Apply three-token CAS；
- server-owned Candidate、source-only Apply 和 writeback recovery marker；
- durable `workflow.authoring.changed` event 和全局 SSE replay；
- `BasicConfig.working_dir/workflow.db` composition；
- 新旧 `/workflows` 路由不在同一 public app 上重叠。

当前真实缺口：

- 缺少冻结 Backend 的个体 Node/Edge 管理和 batch delete；
- 缺少 `workflow_task_command` 持久模型和普通 Task command route；
- 当前 Authoring 只通过测试注入的 fake compiler 验证；
- 没有 production package source declaration/discovery；
- 没有启动 reconciliation 和 package source watcher 的 composition；
- 没有 D-040 三个 Backend-shaped 纯转换路由的 production 实现；
- 旧 Registry annotation parser 过于宽松，且 `Literal`、nullable、Field
  和 ResourceSlot 语义不满足 P0-2；
- `POST /workflow-tasks` 当前只安全接受空 input，尚未接入 v1 Contract。

## 4. 模块与 seam

计划按深模块组织，不按老文件逐一复制。

```text
unilabos/workflow/
├── models.py                 # Backend-shaped public records/write DTO
├── store.py                  # SQLite adapter；transaction 和持久事实
├── service.py                # Workflow public Interface；业务编排
├── schema.py                 # 新：v1 Contract/Value 深模块
├── authoring_engine.py       # 新：AST-only Authoring 深模块
├── catalog.py                # 新：authority-scoped Catalog snapshot seam
├── source_discovery.py       # 新：明确 package source declaration adapter
└── composition.py            # production adapter 装配和生命周期

unilabos/registry/
└── annotation_schema.py      # 新：Workflow/Action 共用 annotation 深模块

unilabos/app/
└── workflow_api.py           # 薄 HTTP adapter
```

### 4.1 `WorkflowService`

`WorkflowService` 继续作为 caller 和测试使用的主要 Interface。它负责：

- revision、transaction 和 reserved metadata 规则；
- Graph、Task、Job、Task command 和 Authoring 操作编排；
- Draft 文件与 SQLite 事实的顺序；
- 将 compiler、catalog、source lifecycle 和 store 错误归一化为稳定 domain
  error。

HTTP adapter 不得包含业务规则，Store 不得决定 public error message。

### 4.2 `WorkflowSchemaV1`

`schema.py` 是纯内存深模块，对外只暴露少量操作：

```python
validate_input_contract(raw) -> InputContract
validate_output_contract(raw) -> OutputContract
normalize_task_input(contract, raw_input, resource_resolver) -> dict
validate_output_value(contract, raw_output, resource_resolver) -> dict
```

P0-5 未关闭期间，`validate_output_value` 先通过明确的 unresolved
external-output 结果停止；`normalize_task_input` 使用 D-093～D-099 已冻结的
production Material resolver。内部实现隐藏：

- D-082 finite type vocabulary；
- D-083 strict typing 和唯一 numeric widening；
- D-084 input null-to-omission/default 规则；
- D-085 finite constraints；
- D-086 closed-object 和 unknown-key 规则；
- D-087 output required/no-default 规则；
- schema compatibility 和规范化 JSON。

Schema Pydantic model 是内部实现，不成为另一套 public wire model。

### 4.3 `annotation_schema`

Workflow compiler 和 Registry 是两个真实 caller，因此共享 annotation parser
是一个真实 seam，而不是测试专用抽象。它只解析静态 AST，不 import 或执行作者源码。

Interface：

```python
parse_parameter_annotation(ast_node, imports, doc_metadata) -> ParameterSchema
render_parameter_annotation(parameter_schema) -> ast.expr
parse_action_result_record(ast_node, imports) -> list[OutputFieldSchema]
```

它隐藏：

- `Optional[T]`/`T | None` 接受与统一输出；
- `Annotated` 中唯一 `Field`；
- `Literal` 的严格同族和顺序；
- `ge/le/min_length/max_length`；
- Field 与 docstring title/description precedence；
- `AllowedResourceTemplates(symbol...)` 的静态符号解析；
- D-100 `TypedDict`、frozen dataclass 和非推荐内联字典 Action 输出归一化；
- 不支持 annotation 的 fail-closed diagnostic。

P0-4 关闭前，该模块可以按 D-100 解析 Action 参数和结果记录，但 Registry
不得据此发布完整 ResourceSlot input/output Handle 合同。

### 4.4 `WorkflowAuthoringEngine`

现有 `AuthoringCompiler` seam 深化为一个 production engine：

```python
compile(...) -> CandidateCompilation
generate(...) -> AuthoringTransformResult
validate(...) -> AuthoringTransformResult
```

这是 D-040 三个纯转换路由和持久 Draft 编译共同使用的唯一实现。内部 AST、
私有 IR、normalizer、changeset 和 source-map helper 不暴露给 caller。

### 4.5 `TemplateCatalogSnapshot`

Compiler 只能读取一个带 authority identity 和 fingerprint 的不可变 Catalog
snapshot，不能在编译期间同步、创建或替换 template UUID。

提供两个 adapter：

- production adapter：从 `workflow.db` 的 authority-scoped template 表读取；
- test adapter：构造小型 in-memory snapshot。

Catalog sync 是独立写操作。P0-4 关闭前只允许迁移已确定的 scalar/action
合同和已有真实 Handle，不推断或发布 ResourceSlot Handle。

## 5. Phase 01 收尾切片

当前 Phase 01 core 先在现有 `migration/01-backend-contract` 上补齐独立测试、
全量测试和多 subagent 评审，然后合并。01E、01F、01G 必须分别使用第 1.1 节的
新分支，并从前一轮已经合并的 integration commit 开始。

### 01E — 个体 Node/Edge 管理

实现分支：`migration/01e-node-edge-admin`。

修改范围：

- `unilabos/workflow/models.py`
- `unilabos/workflow/store.py`
- `unilabos/workflow/service.py`
- `unilabos/app/workflow_api.py`
- Phase 01 两个现有测试文件

实现路由：

```text
POST   /api/v1/workflows/{workflow_uuid}/nodes
GET    /api/v1/workflows/{workflow_uuid}/nodes
GET    /api/v1/workflow-nodes/{node_uuid}
PUT    /api/v1/workflow-nodes/{node_uuid}
PATCH  /api/v1/workflow-nodes/{node_uuid}
DELETE /api/v1/workflow-nodes/{node_uuid}

POST   /api/v1/workflows/{workflow_uuid}/edges
GET    /api/v1/workflows/{workflow_uuid}/edges
GET    /api/v1/workflow-edges/{edge_uuid}
PUT    /api/v1/workflow-edges/{edge_uuid}
DELETE /api/v1/workflow-edges/{edge_uuid}

POST   /api/v1/workflows/{workflow_uuid}/batch-delete
```

必须证明：

- 每次 graph-semantic mutation 在同一 transaction 中只增加一次
  `Workflow.revision`；
- Node PATCH 区分 omitted 和 explicit null；
- Node root `param` 严格遵循 D-045；
- Edge endpoint、Handle 和 Workflow ownership 验证失败时不发生部分写入；
- 删除 Node 同时 soft-delete 相关 Edge；
- response envelope、分页和 delete status 与冻结 Backend 一致；
- 不接受 `workflow_id/node_id/edge_id` alias。

提交建议：

```text
feat(workflow): complete backend-shaped node and edge administration
```

### 01F — 普通 Task command

实现分支：`migration/01f-task-commands`。

新增 `workflow_task_command` 表及对应 model/store/service/HTTP route：

```text
POST /api/v1/workflow-tasks/{task_uuid}/commands
```

请求体：

```json
{
  "type": "step | pause | resume | cancel",
  "target_node_uuid": null,
  "idempotency_key": "<non-empty>",
  "description": null,
  "meta_data": {}
}
```

必须证明：

- terminal Task 拒绝新 command；
- `step` 只允许用于 `run_mode=step`；
- `target_node_uuid` 只允许用于 `step`；
- 同 Task、同 idempotency key、相同语义返回原 command；
- 同 key 不同语义返回 conflict；
- 创建 command 不直接伪造 Task/Job 状态变化；
- command restart 后仍可读取和去重；
- Phase 03/04 的 command consumer 可以在不修改 HTTP Interface 的情况下接入。

提交建议：

```text
feat(workflow): persist idempotent workflow task commands
```

### 01G — Phase 01 关闭

验证和 ledger 分支：`migration/01g-phase-closeout`。

执行：

```bash
/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q \
  tests/workflow/test_backend_contract_store.py \
  tests/app/test_workflow_contract_api.py

/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q

/home/changjunhan/.micromamba/envs/unilab/bin/python -m ruff check \
  unilabos/workflow \
  unilabos/app/workflow_api.py \
  tests/workflow/test_backend_contract_store.py \
  tests/app/test_workflow_contract_api.py

git diff --check
```

关闭前还需：

- 对冻结 Backend `09609a2` route/DTO/status/error 测试逐项做 parity 对照；
- 更新 README phase status 和精确测试结果；
- 每一轮通过独立测试和评审门后分别 merge 到
  `integration/workflow-task-runtime`；
- 从 01G 的合并点创建 `migration/02a-schema-v1`；
- 不 squash migration provenance，不修改或 push Backend。

## 6. Phase 02 实现切片

### 02A — v1 Contract 与严格值验证

实现分支：`migration/02a-schema-v1`。

新增：

```text
unilabos/workflow/schema.py
tests/workflow/test_value_schema_v1.py
```

先写 table-driven tests，再实现：

- 全部 D-082 类型以及 nullable wrapper；
- closed Contract/descriptor/schema object；
- required/non-null default/null default 三种输入声明；
- output 无 `required/default` 且每个 key 必须产生；
- strict scalar、opaque JSON object 和 homogeneous one-dimensional list；
- integer normalization、finite number、bool/int 分离；
- enum、numeric/string/list bounds；
- ResourceSlot external reference shape和 allowlist schema 的纯结构验证；
- 未知 key、非法 constraint、非法 default 和不支持 schema 的稳定 diagnostic。

本切片不访问数据库、Catalog 或 Material。

测试门：

```bash
/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q \
  tests/workflow/test_value_schema_v1.py
```

提交建议：

```text
feat(workflow): implement strict version one workflow schemas
```

### 02B — Workflow/Action 共用 annotation parser

实现分支：`migration/02b-annotation-schema`。

新增：

```text
unilabos/registry/annotation_schema.py
tests/registry/test_annotation_schema_v1.py
```

语义迁移老 OS：

- `registry/utils.py` 的 AST/type/docstring 基础能力；
- `ast_registry_scanner.py` 的 import map 和 literal/default 读取；
- `canonical.py` 的 required/default 测试意图。

拒绝迁移：

- 不支持类型 fallback 到 string/object；
- 把所有 `Literal` 当 string；
- 通过字符串包含关系判断复杂 ResourceSlot 类型；
- import/执行 annotation、decorator 或作者模块；
- `Field(default=...)` 和任意 JSON Schema keyword。

测试需覆盖 D-088～D-091 的 accepted/rejected matrix 和 deterministic render。

提交建议：

```text
feat(registry): share strict action and workflow annotation parsing
```

### 02C — authority-scoped Catalog snapshot

实现分支：`migration/02c-template-catalog`。

修改/新增：

```text
unilabos/workflow/store.py
unilabos/workflow/catalog.py
tests/workflow/test_template_catalog_snapshot.py
```

实现：

- Catalog import transaction 和 authority partition；
- WorkflowNodeTemplate/WorkflowHandleTemplate 真实 UUID upsert；
- business-key 保持和删除语义；
- deterministic fingerprint；
- compiler read snapshot；
- stale/unavailable/mismatch diagnostic；
- compiler 绝不在 read/compile 时同步 Catalog。

P0-4 停止线：

- 允许读取已经存在的 ResourceSlot Handle，并可测试 D-100 归一化合同；
- 参数名和结果字段名只有在 D-100 的显式类型声明内才是合同身份；不得从
  runtime example 或旧 Registry heuristic 生成 Handle；
- 不创建 D-068/D-069 implicit action output；
- P0-4 关闭后再扩展 Catalog projection。

提交建议：

```text
feat(workflow): add authority-scoped template catalog snapshots
```

### 02D — production Authoring engine

实现分支：`migration/02d-authoring-engine`。

新增：

```text
unilabos/workflow/authoring_engine.py
tests/workflow/test_authoring_engine.py
tests/workflow/test_authoring_roundtrip.py
```

语义迁移老 OS：

- `from_python_script.py` 的 AST-only statement parsing；
- `canonical_ir.py` 的 diagnostics、candidate 和 validation sequencing；
- `authoring.py` 的作者语法标记；
- `canonical.py` 的 graph/source-map invariant；
- Python round-trip、source-map、parameter 和 binding 测试意图。

目标实现：

- exactly one `@workflow_definition` function；
- `workflow_uuid` 与注册 source/path Workflow 匹配；
- module-scope typed `device()` selector；
- stable Node UUID anchor；
- keyword-only Workflow/Action 参数；
- normalized absolute imports、`list[...]`、`T | None`、Field 和 Literal；
- Backend-shaped Node/Edge/Catalog identity；
- Input/Output Contract 和 root Output Binding 写入 reserved metadata；
- one-result-object 和 named output attribute；
- exactly one final `return workflow_output(...)`；
- deterministic source、source map、changeset 和 Candidate；
- syntax/semantic/catalog 错误全部作为成功 transform response 内的 diagnostic；
- 不产生 public Canonical DTO。

P1-2 停止线：

- 仅迁移已经冻结的顺序、group、parallel 和现有可证明控制结构；
- Conditional Join 未关闭前不发布完整条件语法。

提交建议：

```text
feat(workflow): migrate python authoring to backend-shaped candidates
```

### 02E — 纯 Authoring HTTP Interface

实现分支：`migration/02e-authoring-transform-api`。

扩展 `workflow_api.py`：

```text
POST /api/v1/authoring/compile
POST /api/v1/authoring/generate-python
POST /api/v1/authoring/validate
```

新增：

```text
tests/app/test_authoring_transform_api.py
```

必须证明：

- 三个操作对 Graph、Draft、Task、Job 和 device dispatch 均无副作用；
- request/response 只使用 Backend identity；
- diagnostics 使用成功 envelope；
- malformed request 才使用 HTTP error envelope；
- pure route Candidate 不能直接替代 persistent server-owned Candidate；
- generate→compile→validate 对 proof-equivalent Graph 保持 UUID 和 normalized source。

提交建议：

```text
feat(workflow): expose pure backend-shaped authoring transforms
```

### 02F — package source declaration、启动恢复和 watcher

实现分支：`migration/02f-source-lifecycle`。

新增：

```text
unilabos/workflow/source_discovery.py
tests/workflow/test_authoring_source_discovery.py
tests/workflow/test_authoring_source_watcher.py
```

package declaration 使用明确条目，不扫描任意 `.py`：

```yaml
package:
  name: szlab_poly_studio

workflows:
  - workflow_uuid: 8feecdda-3898-4afc-9735-4f1ac59553fd
    source: szlab_poly_studio/workflows/magnetic_stirring.py
```

实现：

- 从已选 editable package root 解析声明；
- containment、regular UTF-8 file、symlink 和 duplicate UUID/path 验证；
- 启动时注册并调用 `reconcile_registered_source`；
- watcher debounce/coalesce 后只调用 `WorkflowService`；
- same-hash OS write 不重复事件；
- 删除/rename 不跟随新路径，不删除 Applied Workflow；
- canonical path 恢复时使用 `cause=recovered`；
- composition start/stop 关闭 watcher 和 store。

该 adapter 只把 package declaration 转成现有
`register_editable_source(...)` Interface；文件生命周期规则仍留在
`WorkflowService`。

提交建议：

```text
feat(workflow): register and watch package-owned authoring sources
```

### 02G — persistent Authoring 接入 production engine

实现分支：`migration/02g-persistent-authoring`。

修改：

```text
unilabos/workflow/composition.py
unilabos/workflow/service.py
unilabos/app/web/server.py
tests/app/test_workflow_contract_api.py
```

将 fake-only seam 替换为 production composition，同时保留 in-memory Catalog
和 compiler fixture 供 Interface 测试使用。

必须证明：

- Draft PUT、external file change 和 startup recovery 使用同一 engine；
- Apply 对当前 compiler/catalog 重新验证；
- reserved `input_contract`、`output_contract`、`output_bindings` 和 Node
  `input_bindings` 只能由 Apply 修改；
- source-only Apply 不增加 Workflow revision；
- semantic Apply 在一个 SQLite transaction 中提交 Graph、reserved metadata、
  applied source、source map、provenance、revision 和 event；
- normalized source writeback 失败仍返回已提交成功和 warning。

提交建议：

```text
feat(workflow): compose production persistent authoring
```

### 02H — Task input preflight 的可实施部分

实现分支：`migration/02h-task-input-preflight`。

修改：

```text
unilabos/workflow/service.py
unilabos/workflow/store.py
tests/workflow/test_workflow_task_input_v1.py
```

现在可以实现：

- 从 persisted Workflow metadata 读取 ordered Input Contract；
- unknown/missing/default/null/type/constraint 验证；
- 对 scalar、opaque object 及其 list 生成 canonical Task input；
- 在创建任何 Task/Job 前失败；
- 将完整 resolved input 写入 `WorkflowTask.input` 和 immutable snapshot；
- 根据真实 Handle UUID 把 Workflow input binding 投影到 Task-scoped Job
  `param`，不改 persisted Node `param`。

P0-3 已解锁，必须完整实现：

- non-null ResourceSlot 由接收 `POST /workflow-tasks` 的同一 OS authority
  本地 Material module 解析；
- canonical snapshot 固定为 `{uuid, resource_template_uuid}`；
- 结构、类型或模板不匹配返回 400，缺失或软删除返回 404，稳定不可运行返回
  409；
- Reservation 瞬时争用不撤销已创建 Task：Task 保持 pending，Reservation
  全有或全无并由协调器重试；
- `active/consumed/discarded/quarantined/reconciling` 是 Material disposition；
  `reserved/in_use` 从 Reservation/Claim 派生；
- Material 使用软删除；存在有效 Reservation、Claim、不确定执行结果或活跃
  关系时以 409 `material_in_use` 拒绝删除。

提交建议：

```text
feat(workflow): preflight version one scalar task inputs
```

## 7. Phase 02 测试门

阶段目标测试：

```bash
/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q \
  tests/workflow/test_value_schema_v1.py \
  tests/registry/test_annotation_schema_v1.py \
  tests/workflow/test_template_catalog_snapshot.py \
  tests/workflow/test_authoring_engine.py \
  tests/workflow/test_authoring_roundtrip.py \
  tests/workflow/test_authoring_source_discovery.py \
  tests/workflow/test_authoring_source_watcher.py \
  tests/workflow/test_workflow_task_input_v1.py \
  tests/app/test_authoring_transform_api.py \
  tests/app/test_workflow_contract_api.py
```

迁移后的老测试意图：

```text
tests/workflow/test_canonical_roundtrip.py
tests/workflow/test_canonical_schema.py
tests/workflow/test_python_projection_source_map.py
tests/workflow/test_python_roundtrip.py
tests/workflow/test_pythonic_bindings.py
tests/workflow/test_workflow_parameters.py
tests/contracts/test_workflow_revision_contract.py
tests/app/test_workflow_authoring_api.py
```

这些旧文件不能原样复制。新测试通过 `WorkflowSchemaV1`、
`WorkflowAuthoringEngine` 和 `WorkflowService` Interface 验证行为，并删除旧
Canonical wire assertion。

完整 gate：

```bash
/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q
/home/changjunhan/.micromamba/envs/unilab/bin/python -m ruff check \
  unilabos/workflow unilabos/registry tests/workflow tests/registry tests/app
git diff --check
```

## 8. 不在本计划内实现

即使附近代码容易顺手修改，也不得在本计划中实现：

- local Material authority/store selection、lookup 和 deleted/unavailable
  error；
- action ResourceSlot declaration 到 Handle Template 的完整投影；
- action implicit ResourceSlot output 发布；
- 最终 `WorkflowTask.output` external serialization 和 partial result；
- scheduler dispatch、Job feedback/terminal journal 和 runtime SSE 全量迁移；
- debugger launch/Hold；
- Conditional Join；
- `tool_call` process isolation；
- FE port 和 `WorkflowPanel` 改造；
- `/runtime/runs` 或旧 Canonical compatibility adapter。

## 9. 完成定义

Phase 01 完成：

- 当前 core、01E、01F 和 01G 均在独立分支通过至少两名测试 subagent 和三名
  review subagent 的门禁；
- 冻结 Graph/Node/Edge/Task/Job/Task command Interface 通过 parity 测试；
- Task snapshot、Job、command 和 Authoring state restart 后仍一致；
- 新模块不存在旧 Run public vocabulary；
- baseline、ruff 和 diff check 全绿。

Phase 02 完成：

- 02A～02H 每轮都从最新 integration 新建分支并独立通过测试/评审/合并；
- production Authoring 不再依赖 fake compiler；
- D-082～D-092 accepted/rejected matrix 有完整自动化测试；
- Python compile/generate/validate 和 persistent Draft 使用同一 engine；
- public Interface 中没有 Canonical DTO；
- package source 显式注册、启动恢复和 watcher lifecycle 可用；
- scalar/opaque/list Task input 在 Task/Job 创建前严格验证并快照；
- ResourceSlot runtime/material 已按 D-093～D-099 完整实现；action
  projection 和 external output 路径明确停在 P0-4/P0-5，不存在猜测实现；
- 迁移清单中的 Phase 02 旧测试均已被迁移、替代或保留明确后续 owner。
