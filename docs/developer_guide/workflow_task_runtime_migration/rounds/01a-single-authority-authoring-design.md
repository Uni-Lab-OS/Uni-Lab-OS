# Phase 01A：单工作区 Authority 与单向 Authoring 设计

状态：**Catalog/Store 锁序修正已实现并通过完整测试，等待最终风险复审；本文件是 Round 14 旧并发/writeback 方案的替代契约。**

基线：`2a394737ec7a36f8710e0af27472953451c308bc`

修正分支：`migration/01a6-catalog-store-lock-order`

Backend 仍是只读合同参考。本轮只修改 Uni-Lab-OS；前端实现必须使用独立 FE
分支。

## 1. 设计目标

本轮按“实验室内只有一名调试人员”的真实部署约束收口 Authoring：

1. 一个工作区只运行一个 OS Workflow Authority；
2. 该 Authority 可以同时管理、执行和修改多个不同 Workflow；
3. 每个 Workflow 在一个前端编辑会话中只有一种可编辑表示；
4. Apply 只提交已物化到 Draft 文件的确定源码和对应图；
5. Apply 是纯 SQLite 事务，提交后不再写回 Draft 文件。

“只允许一名调试人员”不等于“只能打开一个 Workflow”。互斥边界是工作区的 OS
Authority 进程，不是 Workflow 数量。实例内继续保留每 Workflow 锁，因此不同
Workflow 可以独立编译、保存和应用；同一 Workflow 的 Draft/Apply 操作串行化。

## 2. 前端编辑模式

前端提供显式模式切换，但模式只属于前端会话，不成为第二个 OS 持久状态：

- **代码模式**：Python 可编辑，画布只投影当前 Candidate；
- **画布模式**：画布可编辑，Python 是确定性生成的投影；
- 切换模式前，必须先处理当前模式的未保存修改；
- 两种模式最终都把完整 Python 源码保存到同一个 package Draft 文件；
- OS 不新增 Canvas Draft、双向同步日志或 editable graph 副本。

画布模式使用现有纯转换接口 `authoring/generate-python` 生成完整 Python，前端展示
完整 diff；用户接受后，再调用 Workflow-scoped Draft PUT。图形布局等 Python
不表达的展示信息继续按既有 graph presentation 语义处理，不制造第二份业务图
Authority。

本轮 OS 接口实现完成后，前端另开分支实现模式按钮、只读投影、diff 接受和联调。

## 3. Draft 与 Candidate

持久 Draft PUT 保留双 CAS：

- `expected_draft_hash` 防止覆盖外部 coding-agent/Git 修改；
- `expected_workflow_revision` 防止基于过时 Applied Graph 保存；
- PUT 仍可保存无效或尚未规范化的源码，并返回诊断；
- 有效编译结果仍返回 server-owned Candidate。

Candidate hash 是一个不透明能力 token。它内部绑定：

- Draft hash；
- Workflow base revision；
- 完整 Backend-shaped Candidate graph；
- normalized Python source 与 source map；
- compiler version；
- Authority-scoped template catalog fingerprint；
- changeset。

Candidate 可以用于预览，但只有“已物化 Candidate”可以 Apply。已物化的精确定义
是：当前 package Draft bytes 的 SHA-256 等于 Candidate 的 `draft_hash`，并且
Draft 文本逐字节等于 Candidate 的 `normalized_python_source`。

如果编译器产生的 normalized source 与当前 Draft 不同，前端必须先展示完整 diff，
用户接受后使用 Draft PUT 保存 normalized source。第二次编译会签发绑定新 Draft
hash 的 Candidate。OS 不在 Apply 内代替用户接受 diff，也不在 Apply 后写回文件。

## 4. Apply 公共合同

路由保持：

`POST /api/v1/workflows/{workflow_uuid}/authoring/apply`

请求体缩减为一个字段：

```json
{
  "candidate_hash": "sha256:..."
}
```

请求模型保持 closed/strict。不得接收客户端 Candidate、graph、source、source map、
Draft hash、Workflow revision、catalog fingerprint 或 compiler version。Draft hash
和 Workflow revision 已由 server-owned Candidate 绑定，并在 Apply 内从数据库和
实际文件重新校验。

Apply 在每 Workflow 锁内依次执行：

1. 读取实际 Draft、当前 Workflow、持久 Candidate 和当前 Catalog；
2. 校验请求 token 命中当前 Candidate；
3. 校验 Candidate 内部 Draft hash、base revision 和 Catalog fingerprint；
4. 重新编译并验证 server-owned Candidate proof；
5. 获取稳定的 Catalog snapshot/guard，并再次校验其 fingerprint；
6. 在持有 Catalog guard 时获取 SQLite 写事务，并在修改任何数据库状态前，最后
   一次读取实际 Draft；
7. 以第 6 步的 Draft 校验为 Apply 线性化点，校验 normalized source 已物化到
   实际 Draft；Catalog guard 必须持续到事务结束，因此同一时刻的 fingerprint
   仍等于 Candidate；
8. 在已获取的同一事务内提交完整 graph、Applied Source、revision 和 SSE event；
9. 返回 Apply result 和完整 Authoring aggregate。

第 7 步以后不允许为了完成 Apply 再读取或写入 Draft，也不允许“尽力
settle/mark/recover”。在线性化点之前完成的外部写入必须参与校验，不匹配时返回
现有 `draft_hash_conflict`、`candidate_not_materialized` 或
`template_catalog_conflict`，且事务不产生任何状态变化。

Catalog snapshot 是 OS 内部编译器 Adapter 的窄 Interface，不进入 HTTP DTO。
可变 Catalog 的 Adapter 必须提供在上下文退出前保持目录稳定的 snapshot guard；
不可变、无状态的编译器可以退化为一次 fingerprint 快照。所有可变实现必须遵循
唯一锁序 `Catalog → Store`。Store 事务回调只允许读取 Draft，不得重新读取、解析
或锁定 Catalog。这样既保留“Catalog 在 Apply 线性化点稳定”的语义，也避免一条
路径持 Catalog 后访问 Store、另一条路径持 Store 后访问 Catalog 所形成的环锁。

coding-agent、Git 或编辑器不受 OS 文件锁约束，因此不能要求文件系统与 SQLite
共享一个墙钟提交瞬间。在线性化点之后发生的外部写入定义为后续的新 dirty Draft
编辑：Apply 可以提交刚才验证的不可变 Candidate，OS 必须保留外部文件，随后通过
返回 aggregate 和 source monitor 将 Applied Source 投影为 stale。不得为了恢复
“瞬时相等”覆盖外部文件或重新引入 writeback marker。

新增稳定冲突：

- HTTP `409`；
- machine code：`candidate_not_materialized`；
- 中文 message：`请先接受并保存规范化源码，再应用工作流`。

现有 `candidate_hash_conflict`、`workflow_revision_conflict`、
`draft_hash_conflict`、`template_catalog_conflict` 和 422 校验错误仍保留，但
Draft/revision 不再由客户端分别提交。

Apply 成功返回体中的 `warnings` 固定为空数组。移除
`draft_writeback_pending`，因为不再存在提交后文件写回。

## 5. 工作区唯一 Authority

`compose_workflow_runtime(working_dir)` 在打开 `workflow.db` 前获取工作区独占
租约：

- 租约文件位于 `working_dir/.workflow-authority.lock`；
- 使用进程级文件锁，锁随文件描述符/进程退出自动释放；
- 同一进程、同一 `working_dir` 的重复装配返回既有 Service；
- 同一进程切换 `working_dir` 继续拒绝；
- 第二个进程尝试装配同一 `working_dir` 时立即、明确失败，不进入 SQLite schema
  初始化、source monitor 或 Authoring 服务；
- `reset_workflow_service_for_test()` 停止 monitor、关闭 Service 后释放租约。

工作区租约只约束 OS Authority 进程。coding-agent、Git 和文本编辑器仍可直接修改
package Draft；Draft hash CAS、source monitor 和每 Workflow 锁继续负责这些外部
文件变化。

因此不再支持或测试“两个 OS 进程同时迁移/写同一个 workflow.db”。这是无效部署
拓扑，不是需要在 Store 内修复的业务并发。

## 6. 删除范围

替代契约转绿后删除以下未发布复杂度：

- `workflow_authoring.writeback_status`；
- `writeback_source`；
- `writeback_expected_hash`；
- `writeback_generation`；
- post-commit `mark_writeback_pending` / `settle_writeback`；
- writeback generation、ABA、marker backfill 和恢复分支；
- 为多 OS 进程同时初始化同一数据库加入的 deadline/retry/隔离逻辑；
- 只证明上述已删除行为的测试。

Phase 01 尚未合并或发布，没有生产数据库需要兼容这些临时列和 marker。新建数据库
不再创建它们；本分支不为未发布 schema 再造迁移层。

仍保留：

- package Draft 唯一性和安全路径解析；
- Draft 双 CAS 与原子文件替换；
- 外部文件修改监视和 SSE invalidation；
- 启动时按实际 package 文件重建 Candidate；
- 缺失/删除/移动 Draft 不删除 Applied Graph；
- Workflow revision、Catalog 和 Candidate proof 校验；
- 每 Workflow 进程内锁；
- SQLite Applied Graph、Task snapshot、Job 和事件 Authority。

## 7. 测试与清理顺序

本轮遵循“先替换、后删除”：

1. 独立测试作者只从本文件的公共接缝编写红测；
2. 主实现使单 token、物化门槛、纯事务 Apply 和工作区租约转绿；
3. 再删除旧 writeback/多进程竞争代码与专属测试；
4. 运行 Authoring 目标测试、完整测试和静态门禁；
5. 生成 Round 趋势报告，统计 production/test 文件与行数；
6. 下一轮使用唯一一个独立 reviewer 复审精确 SHA。

测试不得通过 skip、xfail 或弱化仍有效合同来转绿。删除测试仅限其生产行为已被本
文件明确废止，并且替代路径已有公共合同覆盖。
