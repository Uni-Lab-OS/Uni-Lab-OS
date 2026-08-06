# Round 02C：Template Catalog 修复确认

日期：2026-08-01

同一唯一 reviewer：`round02c_review`

总基线：`6845aee037e876f3ffd0eb2a146bbbec548ea381`

原评审候选：`1d84efc77f6cd87622e4663c383ce168c5b586fa`

新固定候选：`aca11d64508b482d7800340cbdd8c0e8da058efd`

结论：**P-B01、P-B02、P-B03 均 accepted-fixed；最终 Standards blocking 0、
Standards non-blocking 1，Spec blocking 0、Spec non-blocking 1。允许合并新固定候选
`aca11d6`。**

## 1. 固定点与独立验证

- `git rev-parse` 确认评审 worktree HEAD 和新候选均为 `aca11d6`，总基线可解析，
  merge-base 仍为 `6845aee`；开始复核时 worktree 干净。
- 顺序检查 `git diff 1d84efc..aca11d6` 和完整
  `git diff 6845aee...aca11d6`。修复范围是原报告、3 个独立回归、
  `catalog.py` 定向修复及 Store helper 私有化；没有 Backend、Frontend、HTTP、
  Registry discovery 或 compiler 实现变化。
- reviewer 独立运行新回归对旧 production：先显式 import
  `/home/gaojing/.worktrees/uni-lab-os-02c-review/unilabos/workflow/catalog.py` 和
  `store.py`，再加载新回归文件，结果为 **3 failed**；三个失败分别精确命中
  Unicode business identity、无 authority 条件 SQL 和裸 `ValueError`。
- reviewer 独立运行同一回归与原合同对新 production：**52 passed**；独立运行
  `tests/workflow`：**696 passed**。
- reviewer 独立运行本轮 4 个相关 Python 文件 `ruff check --select E,F,I`、
  `ruff format --check` 和完整候选 `git diff --check`：全部通过。
- 主执行在报告专用 clean worktree 固定同一 SHA 的门禁记录为：目标 52/52、
  `tests/workflow` 696、`tests/registry + tests/workflow` 1111、完整 tests 1500
  （1497 passed、3 skipped）；新增/修改文件 lint、format、diff-check 通过。
  全目录 936/448 项仍是已登记旧债，本次没有扩散。

## 2. Finding disposition

### P-B01 — accepted-fixed

- 修复：`unilabos/workflow/catalog.py:674-675` 的唯一业务键规范化改为
  `value.strip().lower()`，不再使用更强的 Unicode `casefold()`。
- 回归：`tests/workflow/test_template_catalog_snapshot_review_regressions.py` 的
  134-157 行同时覆盖 Node 的 `Straße/STRASSE` 和 Handle 的 `Maße/MASSE`；旧候选
  RED，新候选保留两个 Node UUID 和两个 Handle UUID。
- 判定：精确满足冻结的 `lower(trim())` 身份语义，保留原始展示文本，没有改变
  UUID 生命周期或 fingerprint 排序。

### P-B02 — accepted-fixed

- 修复：Node historical/collision/existing/UPDATE 位于
  `unilabos/workflow/catalog.py:317-374`，Handle 对应路径位于 `411-468`；每条
  UUID SELECT/UPDATE 的 WHERE 都显式包含 `authority_id = ?` 或 collision 专用的
  `authority_id <> ?`。原有 active lookup、soft-delete、omission 和 Catalog read
  也继续显式按 authority 限定。
- 回归：`test_template_uuid_persistence_sql_is_explicitly_authority_scoped`
  （回归文件 `160-187`）用 SQLite trace 同时观察 Node/Handle SELECT 与 UPDATE；
  旧候选发现 10 条 unscoped statement，新候选为 0。
- 判定：当前 Catalog replace persistence 不再依赖无 scope UUID 查询；跨 authority
  UUID collision 仍返回稳定 Node/Handle UUID path，没有放松严格隔离。

### P-B03 — accepted-fixed

- 修复：`unilabos/workflow/catalog.py:819-910` 完整检查 active Node/Handle 的
  authority、deleted marker、UUID/parent、required/optional semantic string、
  `io_type`、`required` 和 JSON object 列；`811-815` 将最终 canonical codec
  `TypeError/ValueError` 兜底收敛为有限的
  `TemplateCatalogMismatch("/authority/catalog")`。
- 回归：回归文件 `190-217` 将 Node `display_name` 直接破坏为 SQLite BLOB；旧候选
  泄漏 `ValueError("bytes is not a JSON value")`，新候选返回稳定
  `template_catalog_mismatch` 和安全 Node path。
- 判定：具体持久标量损坏优先给出字段 path，无法预见的 codec 类型仍 fail closed；
  没有 SQLite、绝对路径或原始值泄漏。

### S-NB01 — accepted-fixed

- `WorkflowStore.read_template_catalog()` 已改名为私有
  `_read_template_catalog_rows()`（`unilabos/workflow/store.py:353-402`），仅由
  `TemplateCatalog` implementation 在已取得 Catalog guard 的路径调用。
- 冻结的 caller interface 继续只有 guarded immutable snapshot；未新增 repository
  port 或公共浅 interface。该非阻塞 deep-module 建议接受关闭。

### S-NB02 — remains non-blocking

- Node/Handle 字段知识仍分布于 allowlist、规范化、SQL tuple、持久校验和 semantic
  projection；本次为关闭 P-B03 又增加了显式 scalar 校验。
- 这是 possible Duplicated Code / Shotgun Surgery 的维护性判断，不是当前行为缺陷；
  52 个 public-seam tests 已逐字段覆盖 active contract/fingerprint。继续登记为
  Standards non-blocking，后续可用私有 field ledger 收拢，但不得为压行数引入动态
  SQL 或扩大 public interface。

### P-NB01 — remains non-blocking

- `WorkflowStore` 仍在 `_SCHEMA` 中直接创建 active unique index，没有为包含历史
  重复 business key 的旧数据库提供专门审计/迁移 diagnostic。
- 本次修复没有恶化该路径，也没有证据表明已发布 migration Store 会合法生成这种
  重复 Catalog；继续作为 Spec non-blocking 登记。不得自动任选、合并或复活冲突
  identity。

## 3. 回归与边界检查

- `Catalog -> Store` 锁顺序未变：修复只改 SQL predicate、纯规范化/校验与私有 helper
  命名；没有从 Store transaction callback 反向进入 Catalog。
- replace 的单个 `BEGIN IMMEDIATE`、omission soft-delete、事务内 fingerprint 与
  metadata 原子写入未变；collision failure 仍在同一事务回滚。
- snapshot 仍 detached、deep immutable，并在 context 退出前持有同 Store 共享
  guard；codec fallback 不吞 body 异常，也不改变 cleanup 行为。
- local/backend UUID 生命周期、authority partition、deterministic fingerprint、
  unavailable/stale/mismatch code/path 均保持原 49 个合同全绿。
- P0-4 停止线未动：没有合成 implicit ResourceSlot output、`ready` 或 heuristic
  Handle；没有网络同步、Registry 读取、Frontend 或 Backend 改动。
- 没有发现新的 Standards 或 Spec finding。

## 4. 最终判定

新固定候选 `aca11d64508b482d7800340cbdd8c0e8da058efd` 的所有 blocking 已关闭，
RED→GREEN 证据和完整门禁成立，**允许非 squash 合并到
`integration/workflow-task-runtime`**。S-NB02 与 P-NB01 保持登记，不阻止 Round
02C 合并。
