# Phase 01A：单工作区 Authority 重设计趋势与策略报告

状态：**实现与本地完整门禁已通过，尚未完成顺序独立评审，禁止合并。**

基线：`2a394737ec7a36f8710e0af27472953451c308bc`

实现候选：`3728551a47436841f3c2b34b6c9283c318f0f63e`

分支：`migration/01a-single-authority-authoring`

Backend 继续保持只读，本轮没有修改 Backend。本轮没有修改前端，也没有开展
FE–OS 联调。

## 1. 本轮为什么重设计

旧 Round 14 同时支持多个 OS Service/Store/进程竞争一个工作区，并允许 Apply
提交 SQLite 后再写回 package Draft。这个组合产生了连续六轮 blocker：

- Draft/Catalog/SQLite 锁序；
- post-commit marker 归属；
- 同内容跨代 ABA；
- writeback generation；
- 旧 marker schema 回填；
- 多进程 Store 初始化重试、隔离和调用级 deadline。

用户确认的实际部署约束是一个工作区只有一个 OS Workflow Authority，且同一名
调试人员在一个 Workflow 会话内一次只编辑代码或画布中的一种表示。一个 OS
实例仍可管理多个 Workflow；被取消的是“多个 OS Authority 进程争用同一
`workflow.db`”，不是多 Workflow 能力。

因此本轮没有继续修补第七层初始化/marker 并发，而是替换造成这些问题的
生命周期。

## 2. 独立红测

本轮严格只启用一个 subagent。独立测试作者
`/root/round14_single_authority_tests` 从设计 SHA
`f5cdcf84fd4235f411626cde900f668a2b046bb6` 创建独立 worktree 和分支
`test/01a-single-authority-contract`，源测试 commit 为
`b97f5d0212e1dc4f60f8734a6b75183de2a02691`，父分支引入 commit 为
`e82de4f`。

测试只新增
`tests/workflow/test_phase01a_single_authority_contract.py`，没有修改
production、Backend 或既有测试。旧实现上的结果为：

`5 failed, 1 passed`

五个 RED 分别证明：

1. 单字段 `candidate_hash` 被旧 Apply DTO 拒绝；
2. 旧 `draft_hash + revision + candidate_hash` 三 token 请求仍被接受；
3. 未物化 normalized source 没有返回
   `409 candidate_not_materialized`；
4. Apply 仍进入文件写路径；
5. 第二个 OS 进程仍可打开同一工作区。

唯一既有 PASS 是 closed request 已经能拒绝额外客户端 Candidate bundle。

实现后该独立合同为 `6 passed`。

## 3. 实现结果

### Apply

- HTTP Apply 请求只保留 `candidate_hash`；
- Candidate 在服务端绑定 Draft hash、Workflow base revision、Catalog、
  compiler、normalized source、source map、changeset 和完整图；
- Apply 从 server-owned Candidate 解析并重检这些事实；
- 当前 Draft 与 Candidate normalized source 不同，返回
  `409 candidate_not_materialized`；
- 用户必须先通过 Draft PUT 接受并保存完整规范化源码；
- Store Apply 从持久 Candidate 构造 graph 和 Applied Source，在一个 SQLite
  事务中提交 graph/revision/source/event；
- Apply 提交后不再打开、替换或恢复 package Draft；
- 成功返回的 `warnings` 固定为 `[]`。

### 工作区进程边界

- composition 在打开 `workflow.db` 前锁定
  `working_dir/.workflow-authority.lock`；
- 同进程同工作区重复装配返回已有 Service；
- 一个实例可以创建、查询和管理多个 Workflow；
- 第二个进程非阻塞失败，不进入 Store 初始化和 source monitor；
- reset/启动失败会关闭 monitor/Service 并释放租约；
- fork 子进程只关闭继承的文件描述符，不显式解锁父进程租约。

### 保留的并发与安全

工作区唯一 Authority 没有取消外部编辑：

- coding-agent、Git 和编辑器仍可修改 package Draft；
- Draft PUT 继续使用 Draft hash + Workflow revision 双 CAS；
- 原子替换、旧文件描述符写入、symlink/path containment 和 fsync 测试继续保留；
- source monitor、启动扫描、缺失/删除 Draft、Catalog、Revision 和 Candidate
  proof 测试继续保留；
- 每 Workflow RLock 继续让同一 Workflow 操作串行化，不同 Workflow 互不阻塞。

## 4. 删除结果

替代合同转绿后，删除了：

- 四个 `workflow_authoring.writeback_*` 字段；
- `settle_writeback`、`mark_writeback_pending`；
- generation/ABA/marker recovery/backfill；
- Store 初始化专用 retry/deadline/跨数据库隔离；
- source monitor 的 pending-writeback 重试支路；
- 18 个完全只验证上述废止行为的测试文件；
- 既有混合测试文件中的 post-commit writeback 专属用例；
- Apply 请求 revision 数值边界测试，因为 revision 已变为 Candidate 内部事实。

没有删除或弱化独立新合同。原 Apply 文件 CAS 风险测试中仍有效的部分改写为
Draft PUT CAS 测试。

## 5. 代码与测试趋势

相对旧候选基线 `2a394737`：

| 范围 | 变动文件 | 新增行 | 删除行 | 净变化 |
|---|---:|---:|---:|---:|
| Production `unilabos/` | 5 | 192 | 512 | **-320** |
| Tests | 35 | 463 | 5,818 | **-5,355** |

测试文件中新增 1 个独立合同文件，删除 18 个纯旧行为文件。完整测试数量从旧候选的
`867 passed` 变为 `833 passed`：新合同新增 6 个通过用例，因此净减少的 34 个
通过项对应 40 个被明确废止的旧行为实例，不是未解释的覆盖丢失。

旧 Round 14 报告的 production 变化为 `+718/-188`、净增 530 行；本轮在该候选
之上净删 320 行，说明架构复杂度已经实质回落，而不是把并发处理移动到另一个
模块。

## 6. 验证证据

| 门禁 | 结果 |
|---|---|
| 独立新合同 | `6 passed` |
| 完整仓库 `tests/` | `833 passed, 3 skipped` |
| Production/新合同 Ruff `E4/E7/E9/F/I/B` | 通过 |
| Production/新合同 Ruff format | 通过 |
| `git diff --check` | 通过 |
| 废止符号静态扫描 | 0 命中 |

3 个 skip 均为既有条件测试；本轮没有新增 skip 或 xfail。

## 7. 问题趋势判断

本轮不是继续发现更多并发问题，而是删除了错误部署拓扑制造的问题面：

- 新设计入口为 `5 RED / 1 PASS`；
- 实现结束为 `0 RED / 6 PASS`；
- 旧趋势 `4 → 2 → 1 → 1 → 1 → 1` 中最后连续的 1 类问题，都依赖
  post-commit writeback 或多 OS 进程竞争；
- 这两条路径已从产品合同和代码中同时删除；
- production 净减 320 行、测试净减 5,355 行，与问题面收缩方向一致。

当前已知实现 blocker 为 0，但没有独立评审证据，因此只能判断为“实现问题明显
变少、进入待审平台期”，不能判断为可合并。

## 8. 下一轮策略调整

1. 冻结候选 `3728551a`，下一轮只启用一个未参与实现/测试的 subagent 做独立
   评审；禁止并发启动其他 reviewer。
2. 后续评审按不同新分支顺序覆盖决策/合同、模块设计、事务/安全；每轮一个
   subagent。任何代码修复都会生成新候选并使旧精确 SHA 结论失效。
3. 评审重点不再追逐已废止的 generation/marker/schema backfill，而是检查：
   Candidate 内部 token 是否闭合、未物化是否 fail-closed、工作区租约释放、
   fork/异常清理、不同 Workflow 独立性以及现存 Draft CAS。
4. 顺序评审和完整门禁全绿前，不合并到
   `integration/workflow-task-runtime`，不 push。
5. 合并后立即为前端创建独立 FE 分支，实现代码/画布模式按钮、非当前模式只读
   投影、完整 diff 接受，以及单 token Apply adapter；随后开展 FE–OS 联调。

## 9. 前端覆盖结论

本轮**没有覆盖前端实现**。但 OS 已经冻结了前端所需的简化 seam：

- 代码模式：Draft PUT → Candidate 画布投影；
- 画布模式：pure `generate-python` → 完整 diff → Draft PUT；
- 两种模式：物化 Candidate 后用一个 `candidate_hash` Apply；
- 模式只属于 FE 会话，不新增 OS 持久状态。

启动前端实现的合适时机是本 OS 候选完成顺序独立评审并合并之后。前端必须另开
分支；Backend 仍不得修改。
