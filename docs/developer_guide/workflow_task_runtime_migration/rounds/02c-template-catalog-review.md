# Round 02C：authority-scoped Template Catalog 独立评审

日期：2026-08-01

唯一 reviewer：`round02c_review`

固定基线：`6845aee037e876f3ffd0eb2a146bbbec548ea381`

固定候选：`1d84efc77f6cd87622e4663c383ce168c5b586fa`

结论：**暂不允许合并。Standards blocking 0 项、Standards non-blocking 2
项；Spec blocking 3 项、Spec non-blocking 1 项。** 三项 Spec blocking 关闭并重新
跑完门禁后，必须把新的精确候选交回同一 reviewer 复核；本报告不授权合并当前
候选。

## 1. 固定点与检查证据

- `git rev-parse` 确认 HEAD 与固定候选均为
  `1d84efc77f6cd87622e4663c383ce168c5b586fa`，merge-base 为固定基线
  `6845aee037e876f3ffd0eb2a146bbbec548ea381`；评审开始时 worktree 干净。
- `git log 6845aee..1d84efc --oneline` 依次为 seam 冻结、独立测试、production
  实现三个提交；`git diff 6845aee...1d84efc` 非空，共新增 4 个文件/改动文件、
  2,193 行。
- 完整阅读 `AGENTS.md`、`CONTEXT.md`、Round 02C 冻结设计、总计划 02C、
  D-032、D-042、D-100 和 `Catalog -> Store` 锁顺序/P0-4 停止线；逐行检查
  `store.py` 的全部差异、987 行 `catalog.py` 和 851 行独立测试。
- 只读验证：
  - `python -m pytest -q tests/workflow/test_template_catalog_snapshot.py`：
    `49 passed`；
  - 本轮三个 Python 文件 `ruff check --select E,F,I`：通过；
  - 本轮三个 Python 文件 `ruff format --check`：通过；
  - `git diff --check 6845aee...1d84efc`：通过。
- 仓库全规则 Ruff 对既有 `store.py` 仍报告迁移前已经存在的 typing-modernization
  baseline；本轮新增行没有新增 E/F/I 或格式错误，本报告不把 baseline 误计为
  Round 02C finding。
- 未修改 production、独立测试、Backend 或 Frontend；对抗性验证只使用临时
  SQLite 数据库。

## 2. Standards 轴

### 2.1 Blocking：0 项

已确认的硬规则：

- `TemplateCatalog.replace/snapshot` 都先取共享 Catalog guard，再进入 Store
  transaction/read；没有发现 `Store -> Catalog` 反向取锁；同一 Store 的多个 facade
  共用 `WorkflowStore._catalog_lock`。
- snapshot context 退出路径只有 guard 释放，不做 I/O、同步或业务检查，也不会吞掉
  context body 异常。
- production 注释/docstring 使用简体中文，新代码有类型标注；无 Backend、Frontend、
  HTTP、Registry discovery 或 compiler side effect 越界。
- 独立测试只通过冻结的 `TemplateCatalog` seam 验证行为，没有删除、skip、xfail 或
  弱化既有合同。

### 2.2 Non-blocking

#### S-NB01 — 原始 Store 读取 helper 扩大了 deep module 的可见 interface

- 类型：deep-module seam / possible Middle Man（判断性 smell，不是文档硬违规）。
- 位置：`unilabos/workflow/store.py:353-402`；调用点
  `unilabos/workflow/catalog.py:233-236,268-270`。
- 证据：`WorkflowStore.read_template_catalog()` 是无下划线的公开方法，返回可变的
  metadata/node/handle 原始字典，也不会自行取得 Catalog guard。它实际上只是给
  `TemplateCatalog` 实现使用的 SQLite facts helper，但方法形状允许未来调用者绕过
  immutable guarded snapshot。冻结设计把 caller interface 限定为
  `TemplateCatalog.snapshot()`。
- 影响：当前 diff 没有绕过调用者，因此不构成当前行为阻塞；但这个浅 interface
  会降低模块 locality，让后续 compiler/adapter 容易依赖未校验的持久形状。
- 最小修复：把它明确降为 `WorkflowStore` 的私有内部 seam（例如
  `_read_template_catalog_rows`），或将读取 SQL 收回 `TemplateCatalog` 的私有
  persistence implementation；不要另加一个公共 repository interface。

#### S-NB02 — Backend-shaped 字段知识重复，后续新增字段会触发 Shotgun Surgery

- 类型：possible Duplicated Code / Shotgun Surgery（判断性 smell）。
- 位置：`unilabos/workflow/catalog.py:22-61,519-580,650-689,787-844,859-894,
  927-938`。
- 证据：Node/Handle 字段集合分别散落在输入 allowlist、规范化、SQL tuple、持久行
  校验、fingerprint semantic projection 和 snapshot projection 中。同一字段的
  optional/JSON/contract 属性需要在 5 至 6 处保持一致。
- 影响：当前 02C 的字段覆盖经逐项参数化测试证明完整，因此不是当前正确性阻塞；
  后续 Backend-shaped 模型增删字段时，漏掉任一点就会产生 fingerprint 漂移或数据
  丢失。
- 最小修复：保留当前仅有 `replace/snapshot` 的深 interface，用一个私有、声明式的
  Node/Handle field ledger 驱动 allowlist、codec 和 semantic projection；SQL 列顺序
  可继续显式，以免用过度动态 SQL 换来更大风险。

### 2.3 Fowler baseline 其余检查

没有另行发现 Mysterious Name、Feature Envy、Data Clumps、Primitive Obsession、
Repeated Switches、Divergent Change、Speculative Generality、Message Chains、
Refused Bequest 的可报告实例。987 行本身不是冗余证据：`replace/snapshot` 两个主要
操作隐藏了 authority 身份、UUID 生命周期、事务、删除、fingerprint、不可变快照和
稳定错误，模块具有实际 depth；应压缩的是 S-NB02 的重复字段知识，不应把这些规则
重新摊回 caller 或拆成多个公共浅模块。

## 3. Spec 轴

### 3.1 Blocking

#### P-B01 — Unicode 业务键错误使用 `casefold()`，超出冻结的 `lower(trim())`

- 位置：`unilabos/workflow/catalog.py:293-307,372-387,467-493,646-647`；冻结规则：
  `02c-template-catalog-design.md:81-98`。
- 证据：唯一规范化函数 `_business_name()` 返回 `value.strip().casefold()`，但冻结的
  Node/Handle 业务键明确是 `lower(trim(...))`。临时数据库实测：
  `"Straße".lower() == "straße"`、`"STRASSE".lower() == "strasse"`，二者按冻结
  规则是两个身份；当前实现的两者 `casefold()` 都是 `"strasse"`，第二次 local
  replace 静默复用了第一次的 Node UUID。
- 影响：合法的 Unicode Action/Handle 名会被错误判成同一业务身份；一次 replace
  可拒绝本应合法的两个模板，分次 replace 则会静默改写展示/合同并复用错误 UUID。
- 最小修复：业务键严格改为 `strip().lower()`，为 Node 和 Handle 各补至少一个
  `lower != casefold` 的回归用例；保留原始展示文本。不要把 SQL `LOWER(TRIM(...))`
  与 Python 语义扩展成更强的 case folding。

#### P-B02 — 多条 UUID 查询/更新没有 authority 条件

- 位置：`unilabos/workflow/catalog.py:317-333,350-360,397-414,430-440`；冻结规则：
  `02c-template-catalog-design.md:75-79`。
- 证据：Node/Handle 的 historical/existing 查询只写 `WHERE uuid = ?`，对应 UPDATE
  也只写 `WHERE uuid = ?`。冻结 seam 明确要求“任何读写 SQL 都必须带
  `authority_id` 条件”。当前代码依赖全局 primary key 和前置 Python 比较来避免
  跨 authority 更新，不是 authority-scoped persistence。
- 影响：当前 schema 的全局 UUID primary key 暂时阻止了实际误改，但 authority
  partition 的关键不变量没有落实在每次持久访问中；未来 schema 迁移、直接行损坏
  或复用内部 helper 时会把跨 authority 耦合藏在全局 UUID 假设里。
- 最小修复：所有 existing/update SQL 同时匹配 `uuid + authority_id`；若还需识别
  跨 authority UUID 冲突，用显式 `authority_id <> ?` collision query 返回稳定
  `/node_templates/uuid` 或 `/handle_templates/uuid`，不要靠无 scope 查询或裸主键
  冲突。

#### P-B03 — 持久行类型损坏会泄漏裸 codec 异常，而不是稳定 mismatch

- 位置：`unilabos/workflow/catalog.py:768-784,787-844,859-903`；冻结规则：
  `02c-template-catalog-design.md:200-212`。
- 证据：`_validate_persisted_rows()` 只验证 UUID、业务键、JSON 列、parent、io_type
  和 required，没有验证 `display_name/type/node_type` 等 semantic 标量类型；随后
  `_semantic_node/_semantic_handle` 将原值交给 `encode_json()`。SQLite 动态类型允许
  这些 TEXT 列被直接写成 BLOB。临时数据库把 active Node 的 `display_name` 更新为
  `bytes` 后，`catalog.snapshot()` 实际抛出裸
  `ValueError("bytes is not a JSON value")`，没有 `code` 或安全 `path`。
- 影响：直接持久损坏、旧迁移或错误 importer 写入异常 SQLite 类型时，compiler
  看见不稳定实现异常；公共 diagnostic 泄漏 codec 细节，违反 fail-closed
  `template_catalog_mismatch` 合同。
- 最小修复：完整验证所有 fingerprint identity/contract 标量列并映射到有限静态
  path；同时在 snapshot fingerprint 重算入口兜底收敛 codec `ValueError/TypeError`
  为 `TemplateCatalogMismatch`。补 BLOB/非法标量 direct-corruption 回归测试，断言
  稳定 code/path 且不含 SQLite/codec 文本。

### 3.2 Non-blocking

#### P-NB01 — 旧数据库上创建 active unique index 缺少显式迁移诊断

- 位置：`unilabos/workflow/store.py:98-142,313-323`。
- 证据：基线已经存在两个模板表，但没有 active business-key unique index；候选在
  每次 Store 初始化的 `_SCHEMA` `executescript` 中直接创建 index。用基线形状创建
  两条 active `" Heat "`/`"hEaT"` legacy Node 后打开候选 `WorkflowStore`，实际在
  Catalog 可用前抛出裸 `sqlite3.IntegrityError: UNIQUE constraint failed`。独立测试
  全部从空库开始，没有覆盖 schema upgrade。
- 影响：目前没有证据表明已发布的 migration Store 会合法产生这种重复行，所以不
  把它升级为当前 Spec blocking；但一旦工作区数据库已有重复/脏 Catalog，升级会在
  OS 启动阶段失败且没有 authority/identity 定位。
- 最小修复：增加 legacy-schema upgrade 测试；在创建 index 前显式审计冲突并给出
  可操作、有限的 Catalog migration diagnostic。身份冲突不能自动任选一行或静默
  合并。

### 3.3 已确认实现完整的 Spec 项

- metadata 正确区分 unavailable 与成功空 Catalog；普通读写按 authority 查询且无
  fallback。
- Local 首次 UUIDv4、active business-key 复用、重启复用、删除后重发换 UUID；
  Backend 必须提供 UUID、保留上游身份、禁止业务键/parent 改绑、允许删除后恢复。
- replace 是完整快照，omission 软删除，Node 删除同时删除 Handle；事务内后段身份
  冲突会回滚前段更新，另一个 authority 不变。
- fingerprint 输入稳定排序，包含 authority identity/kind 和全部 active contract
  字段，排除时间戳/deleted_at；重复、乱序、重启保持确定性。
- snapshot 深不可变、detached，并在 context 内持续持有同 Store 共享 guard；并发
  replace 被阻塞，退出只释放 guard。
- unavailable/missing-parent/stale 的常规路径使用稳定 code/path；snapshot/read
  没有网络、Registry 或 importer side effect。
- P0-4 停止线保持：只持久显式 Handle，没有从 goal/result/runtime example/legacy
  metadata 合成 implicit ResourceSlot、`ready` 或 heuristic Handle。

## 4. 合并判定

当前候选 `1d84efc` **不允许合并**。必须关闭 P-B01、P-B02、P-B03，并为三项加入
回归测试；production 或测试有任何变化后重新运行目标、workflow、registry +
workflow、完整 tests、Ruff/format 和 `git diff --check`，再固定新候选交由同一
reviewer 确认。S-NB01、S-NB02、P-NB01 可登记后续，不阻止正确性修复候选合并。
