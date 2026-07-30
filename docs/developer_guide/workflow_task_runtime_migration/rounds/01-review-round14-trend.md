# Phase 01 Core：Round 14 趋势与策略报告

状态：**实现与本地测试门已通过，独立终审尚未完成，禁止合并**。

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

## 2. 实现结果

实现 commit：
`ad71e7c`（`fix(workflow): close round 14 authority races`）。

本轮完成：

- Apply 在打开 SQLite 事务前再次读取实际 Draft bytes 和当前 Catalog
  fingerprint；编译窗口内任一 Authority 变化均以稳定 `409` 拒绝，且图、
  revision、Applied Source、Candidate 和事件无副作用；
- Candidate graph proof 改为递归、JSON 类型严格的等价比较；
- raw Workflow HTTP seam 使用有限、非递归 JSON decoder，拒绝非有限数字，
  包括位于未来/忽略字段中的非法 token；
- HTTP response、Workflow store 和 Candidate hash 使用同一非递归 JSON
  codec，不再修改 `sys.setrecursionlimit()`，同时保留 10,000 层上限；
- diagnostic range 绑定输入 Draft，source-map range 绑定
  `normalized_python_source`，Draft 保存和 Apply revalidation 都 fail closed；
- diagnostic severity 去除外围空白，`" error "` 不能再绕过 Candidate
  阻断；
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
| Production `unilabos/` | 5 | 1 | 361 | 97 | 264 | 458 |
| Tests | 3 | 2 | 906 | 3 | 903 | 909 |

测试新增行与 production 新增行之比约为 `2.51:1`。新 production 文件是
`unilabos/workflow/json_codec.py`。测试文件数包括一处既有 Round 12 fixture
校正；两份新的独立 Round 14 测试文件分别保留其来源提交。

近五次修复循环的 production 变化如下：

| 循环 | 涉及文件 | 新增文件 | 新增行 | 删除行 | 净增 |
|---|---:|---:|---:|---:|---:|
| Round 10 | 2 | 0 | 210 | 32 | 178 |
| Round 11 | 2 | 0 | 45 | 35 | 10 |
| Round 12 | 5 | 0 | 301 | 20 | 281 |
| Round 13 | 3 | 0 | 300 | 31 | 269 |
| Round 14 | 5 | 1 | 361 | 97 | 264 |

Round 14 的净增没有显著扩大，但变动重新横跨 HTTP、DTO、Service 和 Store。
原因不是增加业务功能，而是移除一个进程全局 workaround，并把统一的 JSON
边界下沉为可复用模块。

## 4. 当前验证证据

| 门禁 | 结果 |
|---|---|
| Round 14 两份独立测试 | `17 passed` |
| Round 12～14、风险回归与公共 API 组合 | `179 passed` |
| Workflow 子树与 Phase 01 独立 app tests | `447 passed` |
| 完整仓库 `tests/` | `844 passed, 3 skipped` |
| 10,000/10,001 层 JSON 控制 | 10,000 接受并往返；10,001 拒绝 |
| 随机 JSON codec 对照 | 1,000 个标准库对照样本往返通过 |
| Ruff `E/F/I/B`、format、`git diff --check` | 通过 |

3 个 skip 均为既有联网进程级慢测，需显式设置
`UNILAB_NETWORKING_TEST=1` 才启用；本轮没有新增 skip、xfail 或 baseline
豁免。

## 5. 问题趋势判断

实现缺陷正在减少，但尚不能宣布审查已经收敛：

- Round 14 的 7 类入口问题中，6 类已完整关闭；
- 第 7 类中的 severity fail-closed 缺陷已关闭，但 D-030 repair payload 的字段
  和嵌套结构仍是 1 个明确设计缺口；
- 新发现已从普通 CRUD/响应形状转向 Authority 时间窗口、类型系统和进程全局
  状态，说明 happy path 已较稳定，剩余风险更少但更深、更跨层；
- 本轮需要 361 行新增 production 代码和 97 行删除代码，表明仍存在架构性
  workaround 清理，不能仅凭用例数量下降判断已经稳定；
- 只有固定当前最终 SHA 后的三方独立评审不再发现新的 blocking 类别，才能把
  Phase 01 core 判为收敛。

因此当前趋势是：**已知实现问题显著减少，问题发现深度继续增加；数量进入收敛，
但终审前仍未到可合并平台期。**

## 6. 下一步策略调整

1. 固定包含本报告的候选 SHA，分别进行决策/合同、仓库规范与模块设计、回归/
   事务/恢复/安全三方独立评审。任何 production 修复都会使相关评审失效并触发
   受影响测试、全量门禁和复审。
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
