# Round 02G：persistent Authoring production composition 设计基线

## 1. 目的与有效决策

本 Round 把 02C `TemplateCatalog`、02D `WorkflowAuthoringEngine`、02E pure Authoring
router、02F package source lifecycle 和 Phase 01 persistent Draft/Candidate/Apply Authority
组合到真实 OS server。完成后，focused test app 与 production app 不再使用不同的
compiler/transport 语义。

本轮以 Wayfinder [D-117](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/139) 和
[OS owning issue #300](https://github.com/deepmodeling/Uni-Lab-OS/issues/300) 为更新后的
canonical control plane。它们明确收窄了旧 D-076 和旧 02G 计划中的两点：

- public Apply **只接受一个**不透明 `candidate_hash`；旧 Draft/revision/Candidate 三 token
  请求和客户端 Candidate/Graph/bundle 都必须拒绝；
- Apply **不做 post-commit Draft writeback**。Candidate 的规范化源码必须先经完整 diff
  接受并通过 Draft PUT 物化；成功 Apply 的 `warnings` 固定为 `[]`。

因此本轮不得恢复旧三 token DTO，也不得实现旧计划中的
`draft_writeback_pending` warning。Draft PUT 仍使用 source hash + Workflow revision 双 CAS。

## 2. 边界与停止线

本轮修改 OS production composition、persistent Candidate 验证和相应 HTTP/Store 合同，
不修改 Frontend 或 Backend，不实现 02H Task input、P0-4 Action ResourceSlot、P0-5 final
output、Debugger、Conditional Join 或 `tool_call`。

Frontend 的代码/画布模式按钮、只读投影和完整 diff 接受必须在 02G 合并后的独立 FE
Round 实现；本轮只交付其真实 HTTP/SSE Authority。

## 3. production composition

### 3.1 一个进程根、一个 Store、一个 engine

production composition 必须按下列 ownership 创建对象：

```text
WorkflowStore(workflow.db)
  ├── TemplateCatalog
  │     └── WorkflowAuthoringEngine(selected CatalogAuthority)
  └── WorkflowService(compiler = 同一个 WorkflowAuthoringEngine)
          ├── registered package sources
          ├── startup recovery
          └── WorkflowSourceMonitor
```

同一个 engine 同时服务：

- `POST /api/v1/authoring/compile|generate-python|validate`；
- Workflow-scoped Draft PUT；
- external file reconciliation；
- startup recovery；
- Apply revalidation 与 Catalog snapshot guard。

不得为 pure router、persistent Service、watcher 或 recovery 各建一个 engine/Catalog，也
不得在请求时临时构造第二个 Store。

### 3.2 显式 Graph Authority 与 editable roots

`CatalogAuthority(authority_id, kind)` 必须由 production startup 显式配置并传给
composition；缺失、空值或非法 `kind` 时不得猜 `local`、回退 Backend 或静默选择另一个
Catalog partition。测试仍可显式注入 fake compiler；该 seam 不能成为 production 默认。

editable package roots 同样来自显式配置，并沿用 02F 的固定 root identity。server 不从
当前目录、`working_dir.parent`、Registry cache 或任意 `.py` 扫描推断 root。

同一进程第二次 setup 只能复用完全相同的 working dir、authority、engine 和 root set；
任何 hot switch 失败关闭。startup 失败仍遵守 02F 的 monitor/Store/lease ownership 和
可重试 cleanup。

### 3.3 production HTTP 挂载

真实 `setup_server()` 必须在同一个 app 上一次性挂载：

```text
/api/v1/authoring/*                         pure transforms
/api/v1/workflows/{workflow_uuid}/authoring persistent Authoring
/api/v1/events                              durable invalidation SSE
```

两组 JSON route 共用 02E 的 body/integer/depth budget、Backend envelope 和错误归一化。
重复调用 setup 不得重复路由或更换 engine。Graph Authority 配置非法时 production
Authoring 必须明确不可用，不能退回 `compiler=None` 后仍宣称接口已装配。

## 4. persistent Candidate 的唯一验证边界

02E 已建立 `candidate_validation.py`；02G 必须关闭 02E S-NB01：pure transform 与
persistent Service 的成功 Candidate 使用同一个 closed bundle semantic validator。

允许 Service 保留持久层特有的 hydration：补齐 Backend read DTO 的 timestamps、
Workflow ownership 和已持久 Catalog projection；hydration 后必须进入共享 validator。
不得继续维护第二套 changeset lifecycle、source-map membership、reserved metadata、
Workflow identity 或 graph semantic 判定。

对同一个恶意 compiler 结果，pure route 与 persistent Draft/recovery 必须一致地拒绝：

- private/unknown entity 字段；
- Workflow UUID/revision 漂移；
- duplicate/missing Node、Edge、Template 或 Handle identity；
- source map 引用 Candidate 外 Node；
- changeset 集合重复、重叠或与实际 diff 不符；
- 非 authoring Workflow metadata、Catalog projection 或不支持的 graph 变更。

用户源码诊断仍是可保存的 Draft 状态；坏 producer/bundle 不能作为可信用户诊断泄漏或
持久化，必须变为稳定 `candidate_invalid`/`internal_error` 边界。

## 5. Draft、recovery 与 Apply 不变量

必须证明：

1. Draft PUT、external file change 和 startup recovery 调用同一 production engine；
2. Draft PUT 双 CAS、8 MiB source budget、无 force write 和 source lifecycle 无回归；
3. Apply public DTO 严格只有 `candidate_hash`；Candidate 内部绑定 Draft hash、base
   revision、完整 graph、normalized source/source map、compiler version 与 Catalog
   fingerprint；
4. Apply 在 per-Workflow lock 下重新编译，持有当前 Catalog snapshot，再进入 SQLite
   transaction，并在 graph mutation 前做最终只读 Draft linearization；
5. normalized source 尚未通过 Draft PUT 物化时返回
   `candidate_not_materialized`，Apply 不写 package file；
6. source-only Apply 更新 applied source/source map/provenance/event，但不增加 Workflow
   revision；
7. semantic Apply 在一个 transaction 内提交 Node/Edge、server-owned reserved
   `input_contract`、`output_contract`、`output_bindings`、Node `input_bindings`、允许的
   Workflow authoring fields、applied source/source map/provenance、revision 和
   `workflow.authoring.changed` event；
8. transaction、Draft linearization 或 Catalog guard entry 失败时没有部分提交；guard
   exit 失败仍沿用 Phase 01A7 的 cleanup-only 语义；
9. successful Apply 返回完整 post-Apply aggregate，`warnings=[]`，不创建 Task/Job，
   不执行设备，也没有任何 post-commit source writeback。

普通 Workflow/Graph metadata 写接口继续保护 reserved `unilab` keys；只有 Apply 可以
改变这些 graph-semantic 字段。

## 6. 旧数据库启动诊断

关闭 02C P-NB01：在创建 active Catalog business-key unique index 前，Store 必须审计
旧库中同 authority 的 active Node/Handle 重复业务键。若存在冲突：

- 以稳定、可识别的 migration/domain error 失败；
- 不泄漏裸 `sqlite3.IntegrityError`、SQL、路径或 payload；
- 不自动任选、合并、删除、改 UUID 或复活 identity；
- 不发布半启动 Service，不释放仍需重试 cleanup 的 ownership。

空库、合法旧库和已迁移库继续幂等启动。

## 7. 测试验收

独立 test-author 至少覆盖：

1. production composition 显式 authority/root，same engine identity 跨 pure、PUT、external、
   recovery、Apply；
2. 真实 server 同时挂载 pure/persistent/SSE，重复 setup 不重复路由；
3. 缺失/非法 authority 不产生 fake/`compiler=None` production Authoring；
4. shared Candidate validator 的 pure/persistent adversarial parity；
5. reserved metadata 只能 Apply，source-only revision 保持，semantic Apply transaction
   完整性与事件一致性；
6. Apply 重新编译和 Catalog fingerprint/guard 冲突；
7. public Apply 仍是单 `candidate_hash`，三 token 与 Candidate/Graph extra 均拒绝；
8. Apply 不写 source，未物化 Candidate 失败，成功 warnings 为空；
9. duplicate legacy Catalog key 给出稳定迁移诊断且不破坏数据库；
10. 02F source lifecycle、Phase 01 Apply linearization/lock/cleanup 与 02E pure route 均无
    回归。

测试 fixture 可使用临时 SQLite Catalog、显式 authority 和 fake compiler 来隔离故障，
但 production composition 合同必须至少使用一次真实 `WorkflowAuthoringEngine` 与真实
`TemplateCatalog`，不能只验证 mock wiring。

## 8. 合并门与下一步

本轮遵守一个 test-author、一个 reviewer、每次仅一个 subagent 的门禁。目标、Phase 02
累积、完整 `tests/`、Ruff/format 和 `git diff --check` 全绿，reviewer 对精确候选确认
0 blocking 后，非 squash 本地合入 `integration/workflow-task-runtime`，不 push。

02G 合并后不等待人工确认，立即从 FE 最新目标基线新建独立分支，实现 D-117 单编辑权
按钮、只读投影、完整 diff 接受，并连接真实 OS production HTTP/SSE；Backend 保持只读。
