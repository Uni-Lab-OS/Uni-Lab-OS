# Round 02C：authority-scoped Template Catalog 趋势与策略报告

日期：2026-08-01

实现分支：`migration/02c-template-catalog`

基线：`6845aee037e876f3ffd0eb2a146bbbec548ea381`

固定 production/test 候选：`aca11d64508b482d7800340cbdd8c0e8da058efd`

状态：**目标、累计、完整测试与本轮质量门全绿；唯一独立 reviewer 最终
Standards/Spec 均为 0 blocking，允许非 squash 本地合并。**

## 1. 本轮交付

02C 新增一个对 caller 只有两个主要操作的深 Module：

```text
显式 local import / Backend sync adapter
                  │
                  ▼
     TemplateCatalog.replace(authority, aggregate)
                  │
      Catalog guard -> SQLite transaction
                  │
       ┌──────────┴──────────┐
       ▼                     ▼
NodeTemplate stable UUID  HandleTemplate stable UUID
       └──────────┬──────────┘
                  ▼
 TemplateCatalog.snapshot(authority)
 immutable rows + deterministic fingerprint
```

它完成：

- `authority_id + kind` 的严格 partition，无 fallback；
- local Node/Handle UUIDv4 首次分配、active 业务键复用、重启保持、删除重发换
  UUID；
- Backend Node/Handle UUID 精确保留、禁止业务键/parent 改绑、删除后恢复同一上游
  身份；
- 完整 aggregate replace、omission 软删除和同一 SQLite 事务回滚；
- `lower(trim())` Node/Handle 业务键，保留原始展示文本；
- versioned canonical JSON + SHA-256 deterministic fingerprint；
- 成功空 Catalog 与 unavailable 的持久区分；
- detached、深不可变、持续持有共享 guard 的 compiler read snapshot；
- `unavailable`、`mismatch`、`stale/conflict` 稳定错误；
- 所有模板 UUID SELECT/UPDATE 显式带 `authority_id`；
- 持久 UUID、业务键、parent、JSON 和全部 semantic scalar 损坏 fail-closed 为稳定
  mismatch；
- compiler read 不触发 Registry、网络、同步或 importer side effect。

本轮没有实现 Registry/Action 合同到模板字段的完整 projection，没有合成 implicit
ResourceSlot/`ready`/heuristic Handle，也没有实现 Compiler、HTTP、Draft/Apply、SSE、
Frontend 或 Backend。

## 2. 独立 TDD 与评审 provenance

| 角色 | subagent | 独立提交/报告 | 结果 |
|---|---|---|---|
| Test author | `round02c_test` | 原提交 `06e47d7`，合入为 `21db5d8` | 49 RED，统一缺少 public facade |
| Reviewer | `round02c_review` | 原报告 `7c89e69`，合入为 `e22809b` | Standards 0B/2NB；Spec 3B/1NB |
| Finding tests | 同一 test author | 原提交 `47fb007`，合入为 `ed9f389` | P-B01～P-B03 各 1 个精确 RED |
| Same reviewer confirm | 同一 reviewer | 原报告 `95c2a81`，合入为 `548afd8` | 3 blocking 与 S-NB01 accepted-fixed |

初始 RED：

```text
tests/workflow/test_template_catalog_snapshot.py
49 failed
统一首因：缺少 unilabos.workflow.catalog production facade
```

评审 finding RED：

```text
tests/workflow/test_template_catalog_snapshot_review_regressions.py
3 failed
P-B01：casefold 将 lower(trim) 下不同的 Unicode 名错误判重
P-B02：10 条 UUID SELECT/UPDATE 的 WHERE 缺 authority_id
P-B03：BLOB display_name 泄漏裸 codec ValueError
```

同一 reviewer 在新候选上重新验证：旧候选 3 failed，新候选原合同加 finding
`52 passed`，并独立跑过 `tests/workflow` 的 696 cases。

## 3. 代码与测试规模

相对 `6845aee` 的 production/test 净变化：

| 类别 | 文件数 | 新增 | 删除 | 净增 |
|---|---:|---:|---:|---:|
| Production | 2 | 1152 | 0 | 1152 |
| Tests | 2 | 1068 | 0 | 1068 |
| 合计 | 4 | 2220 | 0 | 2220 |

Production 中 `catalog.py` 为 1065 行，`store.py` 增加 87 行。行数增长主要来自两套
身份生命周期、完整 replace SQL、持久行 fail-closed 校验、fingerprint 和无递归深
冻结，不是新增 caller surface；公开 facade 仍只有 `replace/snapshot` 与四类稳定
domain error。

与 02B 相比，production 从 452 行上升到 1152 行，测试从 856 行上升到 1068 行。
这是从纯 AST/值解析跨入持久身份、事务和并发 guard 后的复杂度阶跃。reviewer 确认
987 行初始 Module 本身不是冗余证据，但指出字段知识散落在 5～6 个内部位置，见
S-NB02。

## 4. 最终门禁

专用 clean worktree
`/home/gaojing/.worktrees/uni-lab-os-02c-final` 上的固定候选 `aca11d6`：

```text
目标（原合同 + finding）：             52 passed
tests/workflow：                       696 passed
tests/registry + tests/workflow：      1111 passed
完整 tests/：                          1497 passed, 3 skipped
新增 Catalog + 两个 tests 完整 Ruff：  passed
本轮 4 个 Python 文件 E/F/I：          passed
Ruff format --check：                  4 files already formatted
git diff 6845aee..aca11d6 --check：    passed
```

3 个 skip 和 warning 均为既有可选依赖、FastAPI lifespan、escape sequence 等
baseline。全计划目录完整 Ruff 仍为 936 个既有错误，E/F/I 子集仍为 448 个；本轮
曾在 `store.py` 新签名中短暂引入 8 个 typing-modernization 错误，已在固定候选前
清除，最终计数与 02B 基线完全相同。

最终门禁之所以在专用 worktree 重跑，是因为原共享 worktree 被外部进程切换到
`docs/phase-02h-cross-repo-plan`。那 4 份外部迁移文档修改未被暂存、覆盖或带入
02C；不能把可能经历中途 branch switch 的测试冒充固定 SHA 证据。

## 5. 问题趋势

| Round | 初次 review blocking | 初次 non-blocking | 最终 blocking | 最终 non-blocking |
|---|---:|---:|---:|---:|
| 02B1 | 7 | 1 | 0 | 1 |
| 02B2 | 2 | 1 | 0 | 1 |
| 02B3 | 4 | 1 | 0 | 0 |
| 02B completion | 2 | 1 | 0 | 0 |
| 02C | 3 | 3 | 0 | 2 |

问题发现数没有单调下降：02C 初次 blocking 从 02B completion 的 2 上升到 3，
non-blocking 从 1 上升到 3。新增问题均来自新的持久边界，而不是重新打开产品设计：

- 1 个精确规范词义错误：`casefold` 强于冻结的 `lower`；
- 1 个 authority SQL 不变量遗漏；
- 1 个 SQLite 动态类型损坏的 fail-closed 漏洞；
- 2 个内部维护/legacy migration 风险。

因此跨 Round 的“发现数”暂时上升，但**问题面仍在收敛**：没有新增 grill、没有
扩张 P0-4、三个正确性 blocking 均形成独立 RED 后关闭；最终只剩两个已定界、不会
影响当前 Catalog Interface 正确性的 non-blocking。

## 6. 遗留问题与重要性

### S-NB02：字段 ledger 重复

重要性：**中等，不阻塞 02C 合并；在扩展 Catalog projection 前必须处理。**

Node/Handle 字段知识目前分散在 allowlist、normalize、SQL tuple、persisted validation、
semantic fingerprint 和 snapshot projection。当前字段逐项测试完整，立即大改成动态
schema 反而会提高 SQL 风险；但 P0-4 关闭并开始增加字段时必须先建立一个私有声明式
field ledger，避免漏字段导致 fingerprint 漂移。

### P-NB01：旧脏库创建 unique index 的启动诊断

重要性：**中高运营风险，但不阻塞当前 Interface；必须在 02G 持久组合/真实升级前
关闭。**

旧 `workflow.db` 若已有人为或实验性重复 active 业务键，初始化新 partial unique
index 会抛裸 SQLite IntegrityError。此前没有 production importer，合法发布路径
产生此状态的概率低，因此 reviewer 没有升级为 blocking；但真实工作区升级前必须
增加 legacy schema audit 和有限、可操作的 migration diagnostic，禁止静默选行。

S-NB01 已随 blocking 修复关闭：原始 Store Catalog rows helper 已降为私有 seam，
caller 只能依赖 immutable guarded `TemplateCatalog.snapshot()`。

## 7. 策略调整

1. 后续规范使用 `lower`、`nullable`、`exact UUID` 等精确词时，测试必须增加一个
   “更强但错误的近邻语义”反例，防止善意扩大规范。
2. 后续 authority-scoped persistence Round 固定增加 SQL trace probe，验证每条身份
   SELECT/UPDATE/DELETE 的 predicate，而不只验证 happy-path 结果。
3. 02D/02G 的持久输入读取固定增加 SQLite dynamic-type corruption matrix；codec、
   ORM 或类型标注不能代替读边界 fail-closed。
4. 从下一 Round 起实现、最终测试、review 分别使用专用 worktree；不再把会被其他
   任务切换 branch 的共享 worktree 当成固定 SHA 门禁环境。
5. 02D 只消费 02C immutable snapshot，实现 production Authoring engine；不得在
   compiler read 时调用 `replace()`、Registry 或网络。
6. S-NB02 在 P0-4 完整 projection 扩展前处理；P-NB01 在 02G 真实持久组合前处理，
   两者写入后续 Round 风险清单而不是遗忘。

## 8. 前端、Backend 与 Wayfinder

- Frontend：**未覆盖、未修改**；
- Backend：**未覆盖、未修改**；
- FE-OS 联调：**尚未触发**；
- 触发判断：02C 只有内部 Catalog Interface，前端尚无可调用的 transform/generate
  HTTP；应在 02D production engine 与 02E pure Authoring HTTP Interface 可合并后，
  另开前端分支实现单编辑权并启动 FE-OS contract 联调；
- Wayfinder：本轮没有新增产品语义，只落实已冻结 D-032/D-042 和 P0-4 停止线；
  未把本地工程状态冒充远端 issue 同步。

## 9. 合并结论与下一步

```text
Standards blocking:      0
Standards non-blocking:  1（S-NB02）
Spec blocking:           0
Spec non-blocking:       1（P-NB01）
```

Round 02C 允许非 squash 本地合入 `integration/workflow-task-runtime`。合并后在
integration 专用 clean worktree 再运行目标与完整 tests；通过后直接创建
`migration/02d-authoring-engine`，进入 Round 02D，不等待额外确认。
