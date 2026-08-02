# Phase 01A：合同一致性独立评审趋势与策略报告

状态：**发现 1 个 blocking，候选禁止合并。**

评审分支：`review/01a-contract-spec`

固定比较点：`2a394737ec7a36f8710e0af27472953451c308bc`

评审精确 HEAD：`fe25167860c5cdd5795177757a3af91c8edfdcc3`

Production 候选：`3728551a47436841f3c2b34b6c9283c318f0f63e`

Backend 继续保持只读。本轮没有修改 Backend、Production、Tests 或前端，也没有
开展 FE–OS 联调。

## 1. 本轮方法

本轮遵守“一轮只给一个 subagent”：

- 唯一 reviewer：`/root/01a_contract_reviewer`；
- reviewer 未参与设计、实现和独立合同测试；
- 只做决策/Spec 合同轴；
- Standards/模块设计轴留到后续独立评审轮；
- reviewer 只读检查精确 SHA，没有修改文件或提交；
- 检查范围包括主规格、AGENTS 强制合同、五个变更 Production 文件、新增合同、
  保留测试以及被删除测试的语义。

代码评审流程通常并行执行 Spec 与 Standards 两轴；本轮以用户的单 subagent
约束为更高优先级，因此没有并行启动第二名 reviewer。

## 2. Standards

本轮未执行。下一独立评审轮再使用一个未参与当前工作的 subagent 检查模块设计与
仓库标准，不能把本轮 Spec 结果替代为 Standards 结论。

## 3. Spec

### 3.1 Blocking：Draft 与 SQLite 之间没有共同原子边界

位置：

- `unilabos/workflow/service.py:1029-1055`；
- `unilabos/workflow/store.py:1177-1309`。

Service 在最后一次读取并关闭 Draft 后才进入 Store。Store 事务只重新校验 SQLite
中的 Candidate、revision 和 observed Draft hash，不再观察实际文件。coding-agent、
Git 或编辑器可以在最后读取与事务提交之间替换 Draft，Apply 仍然成功。

reviewer 已用临时目录复现：

- Apply 返回成功，Workflow revision 变为 2；
- Applied Source 是 `result=build()`；
- 实际 Draft 已变为 `external=wins()`；
- 返回 aggregate 状态为 `applied_source_stale`。

这与当前文字合同冲突：

- 主规格 §4 第 89–100 行要求校验已物化 Draft 后提交，并要求 Applied Source 与
  第 5 步 Draft bytes 完全相同；
- `AGENTS.md` 的 Python Authoring Persistence 要求 Applied Source 和已物化 Draft
  在提交时一致。

现有风险测试只覆盖“重新编译阻塞期间”发生的文件修改；独立合同
`tests/workflow/test_phase01a_single_authority_contract.py:234-278` 只证明 Apply
不写文件，没有覆盖“最后重读之后、SQLite 事务之前”的窗口。

结论：相对当前 Spec，这是 blocking。

### 3.2 Non-blocking：工作区租约生命周期覆盖不完整

位置：

- `tests/workflow/test_phase01a_single_authority_contract.py:307-352`；
- `unilabos/workflow/composition.py:77-84,135-158`。

独立合同覆盖了一个 Authority 管理多个 Workflow，以及第二进程被拒绝，但未冻结：

- 同进程同目录重复装配返回同一 Service；
- 同进程切换目录被拒绝；
- reset 后释放租约，后续进程可以装配。

reviewer 目视确认当前实现符合主规格 §5，因此这是测试覆盖缺口，不是已证实的实现
错误。

### 3.3 通过项

reviewer 未发现 scope creep，并确认以下合同在精确 SHA 上符合：

- Apply strict DTO 只接收一个 `candidate_hash`；
- Candidate、Catalog、Draft hash 和 Workflow revision proof 由服务端持有并复核；
- Apply 提交后不写 Draft；
- 工作区租约在打开数据库前获取；
- 一个 Authority 可以管理多个 Workflow；
- 已删除的多 Authority/writeback 行为没有从其他模块重新出现。

Spec 轴合计：**1 个 blocking、1 个 non-blocking。**

## 4. 本轮代码与测试变化

本轮是只读评审轮：

| 范围 | 变动文件 | 新增行 | 删除行 | 净变化 |
|---|---:|---:|---:|---:|
| Production `unilabos/` | 0 | 0 | 0 | 0 |
| Tests | 0 | 0 | 0 | 0 |

本报告是本轮唯一仓库变更，不计入实现代码统计。

## 5. 趋势判断

上一实现轮报告的“已知实现 blocker 0”是在尚无独立评审证据时的暂时状态。本轮独立
评审把趋势更新为：

| 阶段 | Blocking | Non-blocking |
|---|---:|---:|
| 重设计独立红测入口 | 5 | 0 |
| 实现门禁结束 | 0 | 0 |
| 第一轮独立 Spec 评审 | 1 | 1 |

问题数从 0 回升到 1，不代表问题面重新扩张。新增 blocker 集中在一个明确边界：
不受 OS 锁约束的外部 Draft 写入与 SQLite Apply 的先后关系。旧 writeback
generation、marker schema 和多 Authority 初始化问题没有复发；评审也没有发现
scope creep。

因此趋势应判断为：**总体复杂度仍在下降，但“文件与数据库必须在同一墙钟提交瞬间
一致”的合同仍包含一个无法由普通 SQLite 事务独自提供的跨资源原子性要求。**

## 6. 策略调整

不重新引入 post-commit writeback、marker、generation 或多进程 Store 协调。下一轮
先重新冻结这一处并发语义，再决定是否需要 Production 修改：

1. 新开修正分支，只启用一个独立 test-author subagent；
2. 添加精确覆盖“最后 Draft 校验后、SQLite 事务前发生外部替换”的红测；
3. 将 Apply 的线性化点明确为最后一次受每 Workflow 锁保护的 Draft 校验：
   - 该时刻 Draft 必须与 Candidate 完全一致；
   - 之后发生的外部写入定义为一次新的 dirty edit；
   - Apply 可以提交刚才验证的不可变 Candidate；
   - 返回 aggregate 必须显示新 Draft 与 `applied_source_stale`，且不得覆盖外部修改；
4. 同轮补齐同目录复用、切换目录拒绝、reset 释放租约的合同测试；
5. 若现有实现通过新合同，只修改合同文字和测试；若不通过，再做最小 Production
   修正；
6. 完整门禁转绿后生成新候选，旧 `fe25167` 评审结论不自动继承；
7. 新候选继续按一轮一个 subagent 顺序执行 Spec、模块设计、回归/安全评审。

这个调整把一致性定义为可线性化的操作顺序，而不是要求不受 OS 控制的文件系统写入
与 SQLite 具备不存在的共同事务。它保留外部 coding-agent/Git 编辑能力，也避免把
刚删除的 writeback 复杂度重新引入。

## 7. 本轮门禁

| 门禁 | 结果 |
|---|---|
| 独立 Spec reviewer | 1 blocking，1 non-blocking |
| 完整仓库 `tests/` | `833 passed, 3 skipped` |
| `git diff --check` | 通过 |

本轮没有修改 Production 或 Tests，因此没有新增 lint/format 检查范围。3 个 skip
均为既有条件测试；本轮没有新增 skip、xfail 或测试删除。

## 8. 前端覆盖结论

本轮没有前端变更。前端独立分支和 FE–OS 联调仍需等待 OS Authoring 候选完成修正、
顺序评审和合并。Backend 不得修改。
