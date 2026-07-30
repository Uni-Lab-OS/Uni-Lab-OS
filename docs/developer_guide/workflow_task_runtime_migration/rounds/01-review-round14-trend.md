# Phase 01 Core：Round 14 趋势与策略报告

状态：**第二次复审修正与本地全量测试已通过，新精确 SHA 复审尚未完成，禁止合并**。

统计范围：本报告把 Round 14 视为 legacy-named `migration/01-backend-contract`
合并轮次内的第十四次审查修复循环。生产实现只统计 `unilabos/`；测试、文档和
用户工作区中未提交的设计决策不计入生产代码。Backend 是只读参考，本轮没有
修改 Backend。

## 1. 本轮入口与独立红测

Round 14 从候选 `3d30c591cc9d765d1f81cd2fbfc1427946674ba7` 开始。只读风险
审查报告了 7 类 blocker：

1. Apply 在编译期间存在 Draft bytes TOCTOU；
2. Apply 在编译期间存在 Template Catalog TOCTOU；
3. Candidate proof 使用 Python 宽松相等，混淆 `true`、`1` 和 `1.0`；
4. raw HTTP JSON 可在被 DTO 忽略的字段中携带 `NaN`/`Infinity`；
5. router/app 初始化修改进程级 recursion limit；
6. source-map 和 diagnostic range 没有绑定真实源码边界；
7. diagnostic severity 可由空白绕过，且 D-030 的 duplicate UUID repair
   语义尚无精确 wire DTO。

两名独立测试作者在不同 worktree 和分支上提交红测：

| 测试维度 | subagent | 源测试 commit | 引入 commit | 父实现上的红测 |
|---|---|---|---|---|
| 回归、并发与事务风险 | `/root/phase01_adversarial_tests` | `ce1eeaac890ac51b41b8ed4c63b7e37c63f4ff0c` | `f329381` | `7 failed, 1 passed` |
| HTTP/Authoring 公开合同 | `/root/phase01_contract_tests` | `facd30757f884ee95006291ed83a9d5d42647b17` | `acd1288` | `9 failed` |

两份测试共新增 17 个行为用例。父实现上的合并结果为 `16 failed, 1 passed`；
通过项是同型整数值仍可签发合法 source-only Candidate 的正控制。两个测试提交
都只新增测试文件，没有修改 production、既有测试或 Backend，也没有
skip/xfail。

第一版实现形成候选
`21d7f181a6f00d219695a372136b264325146d0f`，并通过当时的整仓测试
（`844 passed, 3 skipped`）。三名独立 reviewer 对该精确 SHA 的结论不是全绿：

| 评审维度 | subagent | 结论 | blocker |
|---|---|---|---|
| 决策与合同 | `/root/round14_spec_reviewer` | PASS | 0 |
| 仓库规范与模块设计 | `/root/round14_design_reviewer` | FAIL | 2 |
| 回归、事务、恢复与安全 | `/root/round14_risk_reviewer` | FAIL | 3 |

两份失败评审的发现存在一项重叠，归并为 4 类问题：

1. Apply 最终检查与 SQLite 提交之间仍有跨 `WorkflowService` 实例的 Draft
   竞态；
2. Candidate proof 和 graph `uniqueItems` 的严格 JSON 比较仍递归，超深合法
   JSON 会触发裸 `500`；
3. source range 错把 Python form-feed 等字符当换行，且没有遵循 AST 的
   1-based UTF-8 byte column 与尾换行 EOF 语义；
4. composition 与 source monitor 直接穿透 `WorkflowService.store`，Store
   被意外暴露为公共依赖。

针对失败评审，两名原独立测试作者在各自 worktree 又提交了补充红测：

| 测试维度 | subagent | 源测试 commit | 引入 commit | 第一版实现上的红测 |
|---|---|---|---|---|
| 跨 Service Apply 竞态 | `/root/phase01_adversarial_tests` | `d397e040990b1926722a393620f6cbfb2820a1d6` | `0c1d1ee` | `1 failed` |
| 深层 proof 与源码坐标 | `/root/phase01_contract_tests` | `ba0fa09c0e18900b3bade162f9f67c02aad067a9` | `9a75570` | `8 failed` |

补充提交只新增测试文件，共 9 个失败用例；没有修改 production、既有测试、
Backend，也没有 skip/xfail。失败评审的 reviewer 必须在最终精确 SHA 上确认
修正，旧结论不能复用。

修正后形成第二个精确候选
`bab0d65a5b6734f860863561a9c230ea1d96e57f`，其整仓门禁为
`853 passed, 3 skipped`。三名 reviewer 再次独立检查整个 Round 14 差异，三方
均判定 FAIL，并收敛到相同的 2 类 blocker：

1. Store 在 `BEGIN IMMEDIATE` 和自身 RLock 内调用 Service 提供的
   `authority_guard`；该回调读取文件和 Catalog，形成
   `SQLite -> Catalog` / `Catalog -> SQLite` 的确定性锁反转，也违反 D-076
   “compiler/catalog 重验证必须在打开 Apply 事务前完成”的顺序；
2. 旧 Apply 提交后，若另一 Store 连接先保存新 Draft，旧 Apply 的无 CAS
   `mark_writeback_pending` 或 `settle_writeback` 会污染/覆盖新 Draft marker，
   产生无恢复载荷的永久 pending 或 `candidate_stale`。

第二次 reviewer 还要求历史遗留的
`pending + writeback_source=NULL + writeback_expected_hash=NULL` 不能被
`actual_hash == observed_hash` 的快速返回永久保留。

两名独立测试作者再次在不同 worktree 冻结红测：

| 测试维度 | subagent | 源测试 commit | 引入 commit | 第二候选上的红测 |
|---|---|---|---|---|
| Store/Catalog 锁反转 | `/root/phase01_adversarial_tests` | `17ba8fd6c4c7986d4247efec6e3f788cf3fbff66` | `c26f4ec` | `1 failed` |
| 陈旧 mark/settle 与坏 marker 恢复 | `/root/phase01_contract_tests` | `3bd7743ef33b138ab64f510cf36ac28d1b9bdf3f`, `29bc0349a62e92f1c2a7a01cff2a699238037cdd` | `da55763`, `5811280` | `3 failed` |

两个新测试文件只包含有界并发与恢复测试，worker 均能在失败路径回收；没有修改
production、Backend，没有 skip/xfail。

## 2. 实现结果

实现 commits：

- `ad71e7c`（`fix(workflow): close round 14 authority races`）；
- `bb32c37`（`fix(workflow): linearize authoring apply`）；
- `9b3a938`（`fix(workflow): bind writeback recovery tokens`）。

本轮完成：

- Apply 按 D-076 在打开事务前完成实际 Draft 与 Catalog 的最后重验证；SQLite
  事务只对自身持久 Draft/Candidate/Catalog token 做 CAS，不再回调文件、
  compiler 或 Catalog，Store 锁序不再依赖外部实现；
- 跨 Service/Store 更新若先提交，会使旧 Apply 的 DB token CAS 返回稳定
  `409`；若新 Draft 在旧 Apply 提交后取得 Authority，旧 Apply 的 settle/mark
  只能按原 recovery source/hash 做 CAS，失配即无操作，不会污染新 Candidate；
- reconcile 会识别 `pending` 但缺少 recovery source/hash 的历史坏 marker，
  重新按实际 Draft 投影并清除永久重试状态；
- Candidate graph proof、source-only proof 和 graph `uniqueItems` 共用迭代式、
  JSON 类型严格的等价/规范化实现，不受 Python recursion limit 影响；
- raw Workflow HTTP seam 使用有限、非递归 JSON decoder，拒绝非有限数字，
  包括位于未来/忽略字段中的非法 token；
- HTTP response、Workflow store 和 Candidate hash 使用同一非递归 JSON
  codec，不再修改 `sys.setrecursionlimit()`，同时保留 10,000 层上限；
- diagnostic range 绑定输入 Draft，source-map range 绑定
  `normalized_python_source`；范围按 Python AST 的 1-based UTF-8 byte column
  校验，只把 `CRLF`/`CR`/`LF` 当物理换行，并接受尾换行后的 EOF 位置；
- diagnostic severity 去除外围空白，`" error "` 不能再绕过 Candidate
  阻断；
- `WorkflowService` 不再公开 Store；composition、启动恢复和 source monitor
  只依赖 Service 的领域方法，持久实现边界重新收口；
- Round 12 的“合法 Candidate”fixture 从实际越界的 source-map 改为真实合法
  范围；测试目的和断言没有弱化。

明确未做：

- 没有修改 Backend 源码、schema、migration、docs、tests、branch 或 commit；
- 没有新增 Backend 能力、proxy/fallback 或 `/api/v1/edge/*` 依赖；
- 没有自行发明 D-030 duplicate UUID repair 的 wire 字段；
- 没有在本轮修改前端。

## 3. 代码与测试增量

| 指标 | 文件数 | 新增文件 | 新增行 | 删除行 | 净增 | 变动量 |
|---|---:|---:|---:|---:|---:|---:|
| Production `unilabos/` | 8 | 1 | 562 | 180 | 382 | 742 |
| Tests | 11 | 6 | 2,505 | 48 | 2,457 | 2,553 |

测试新增行与 production 新增行之比约为 `4.46:1`。新 production 文件是
`unilabos/workflow/json_codec.py`。测试文件数包括一处既有 Round 12 fixture
校正、6 份保留独立来源提交的 Round 14 测试文件，以及因 Store 私有化而改为
显式白盒访问的既有持久层测试。相对第一版报告，复审反馈新增了 162 行、
删除了 89 行 production，实现增长集中在事务 CAS、迭代比较、精确源码坐标和
Service 边界。相对第二候选，第二次复审修正新增 84 行、删除 39 行
production；独立测试新增 778 行、删除 2 行。

近五次修复循环的 production 变化如下：

| 循环 | 涉及文件 | 新增文件 | 新增行 | 删除行 | 净增 |
|---|---:|---:|---:|---:|---:|
| Round 10 | 2 | 0 | 210 | 32 | 178 |
| Round 11 | 2 | 0 | 45 | 35 | 10 |
| Round 12 | 5 | 0 | 301 | 20 | 281 |
| Round 13 | 3 | 0 | 300 | 31 | 269 |
| Round 14 | 8 | 1 | 562 | 180 | 382 |

Round 14 的当前净增比第一版报告增加 118 行，并横跨 HTTP、DTO、Service、
Store、composition 和 monitor。原因不是增加业务功能，而是第一次事务修正把
外部 Authority 检查错误地带入 Store 锁内，第二次复审又证明提交后恢复动作也
必须绑定 Apply generation；这些差异同时移除了进程全局 workaround、下沉统一
JSON 边界并收回泄漏的 Store seam。

## 4. 当前验证证据

| 门禁 | 结果 |
|---|---|
| Round 14 六份独立测试 | `30 passed` |
| Workflow 子树与 Phase 01 独立 app tests | `460 passed` |
| 完整仓库 `tests/` | `857 passed, 3 skipped` |
| 10,000/10,001 层 JSON 控制 | 10,000 接受并往返；10,001 拒绝 |
| 随机 JSON codec 对照 | 1,000 个标准库对照样本往返通过 |
| Ruff `E/F/I/B`、format、`git diff --check` | 通过 |

3 个 skip 均为既有联网进程级慢测，需显式设置
`UNILAB_NETWORKING_TEST=1` 才启用；本轮没有新增 skip、xfail 或 baseline
豁免。

## 5. 问题趋势判断

实现缺陷总体在减少，但两次终审都证明上一候选尚未收敛：

- Round 14 的 7 类入口问题中，6 类已完整关闭；
- 第 7 类中的 severity fail-closed 缺陷已关闭，但 D-030 repair payload 的字段
  和嵌套结构仍是 1 个明确设计缺口；
- 首次三方终审新发现 4 类问题，补充红测新增 9 个失败；其中 3 类是原风险的更深
  边界（事务线性化、递归深度、AST 坐标），1 类是模块 seam 泄漏，而不是新增
  业务范围；
- 第二次三方终审的新发现从 4 类降为 2 类，且三方独立结论完全重合：Store
  锁序与 post-commit marker 归属。它们都属于 Apply 生命周期协调，没有增加
  DTO、路由或产品能力范围；
- 新发现已从普通 CRUD/响应形状转向 Authority 线性化、结构深度、精确字符编码
  与依赖方向，说明 happy path 已较稳定，剩余问题数量更少但验证成本更高；
- 当前需要 562 行新增 production 代码和 180 行删除代码，表明仍有架构性
  workaround 与边界清理，不能只凭最初用例数量下降判断稳定；
- 补充红测现已全部转绿，Workflow 子树由 `447` 增至 `460` 个通过用例，说明
  新问题已被转化为可重复回归资产，而不是继续漂移的口头风险；
- 只有固定当前最终 SHA 后的三方独立评审不再发现新的 blocking 类别，才能把
  Phase 01 core 判为收敛。

因此当前趋势是：**问题类别从首次复审的 4 类降到第二次的 2 类，且已集中到同一
Apply 生命周期，数量和范围都在收敛；但连续两次精确评审仍发现 blocker，只有
新候选三方全绿才可判定进入可合并平台期。**

## 6. 下一步策略调整

1. 固定包含本报告的候选 SHA，在干净 worktree 重跑整仓测试和 lint/diff 门禁，
   再由原三名 reviewer 分别复审决策/合同、模块设计、事务/恢复/安全，明确确认
   首次 4 类和第二次 2 类 blocker 的处置，尤其验证 Store 事务内无外部回调、
   post-commit CAS 失配无副作用以及坏 marker 可恢复。任何代码修复都会使相关
   评审失效并触发受影响测试、全量门禁和再次复审。
2. 如果终审没有新增 blocker，停止继续堆叠 Phase 01 review round，按门禁把
   `migration/01-backend-contract` 合入
   `integration/workflow-task-runtime`；未经用户授权不 push。
3. D-030 的 duplicate UUID repair wire DTO 在 Phase 02 production compiler 或
   前端 quick-fix 接入前单独冻结。它不得由测试或实现猜测字段名。
4. Phase 01 合并后立即创建独立前端分支
   `migration/08a-workflow-contract-seam`，先迁移 `packages/services` 的最终
   Workflow Graph/Task/Job/Authoring DTO、严格 envelope 和全局 SSE transport；
   不在该切片启用产品 Authoring UI、Run controls 或浏览器文件写回。
5. 第一阶段 FE–OS 联调使用真实 FE service adapter、真实 OS FastAPI composition
   和真实 `workflow.db`，验收 Graph revision CAS、Task snapshot、Job
   预创建和严格 envelope。持久 Authoring 的真实浏览器联调必须等待 02G 的
   production compiler、package source registration 和 watcher；fake compiler
   不能作为产品 E2E 证据。
6. 前端仓库的旧 Canonical/Run/per-run-WebSocket `AGENTS.md` 约束与目标合同冲突。
   前端分支第一项变更必须先校正规则，并把旧代码登记为迁移源；禁止为绕过未就绪
   能力修改 Backend。

## 7. 前端覆盖结论

Round 14 本身**没有覆盖前端实现，也没有开展 FE–OS 联调**。这是有意的门禁
选择：当前 OS SHA 尚未完成终审和 integration 合并，且 production composition
尚未注入真实 Authoring compiler/source discovery。

最早可启动的前端工作已经明确为独立的 service-contract seam；真实 Phase 01
shared-contract 联调在本轮通过并合并后触发，Authoring activation E2E 则在 02G
后触发。整个过程中 Backend 保持只读。
