# Round 02F：package source lifecycle 独立评审

## 1. 固定对象与结论

- 基线：`4875cc1ac753ad551f9273a6fa0e84126b67fd89`
- 被评审且已测试的候选：`b08ea30c7ee2a9419cc82eed3450df5b06291e5f`
- 比较命令：`git diff 4875cc1...b08ea30`
- 候选提交：`f169cc0`（设计）、`7af9159`（独立测试）、`b08ea30`（production）
- reviewer：`round02f_review`；未参与 02F 实现或独立测试编写。
- 评审视角：regression/security + module design；Standards 与 Spec 两轴分别记录。

结论：**不允许精确候选 `b08ea30` 合并**。Standards 有 3 个 blocking、1 个
non-blocking；Spec 有 3 个 blocking、1 个 non-blocking。三个 blocking 分别位于：

1. 不可信 manifest/source 的路径固定、资源边界与稳定错误收敛；
2. 跨既有 registration 的全量预检和持久注册原子性；
3. composition 的发布顺序及失败停机对 Store/lease 的安全保持。

正常 closed manifest、显式 registration、missing/delete/rename/recovered、watcher
debounce/same-hash、单 Authority 正常停机等路径已经通过；但是下述最小 probe 已证明
blocking 是候选真实行为，不是测试之外的理论推测。本轮没有修改 Frontend 或 Backend，
也没有越过 02G compiler/Catalog production composition 停止线。

## 2. 验证证据

| 检查 | 结果 |
|---|---|
| 02F 两个新测试 + Phase 01A4 shutdown + Phase 01A single-authority | `59 passed, 2 warnings in 7.90s` |
| startup-scan gap、monitor transient retry、Service close failure 回归 | `4 passed, 1 warning in 1.21s` |
| Ruff `E,F,I`（本轮 production/tests） | 通过 |
| Ruff format check（本轮 production/tests） | 通过 |
| `git diff --check 4875cc1...b08ea30` | 通过 |

warning 是既有 FastAPI TestClient/httpx 弃用提示和既有 escape-sequence 提示，不是本轮
失败。

另执行五组不修改仓库的临时工作区 probe：

1. 先持久注册 `A -> workflows/a.py`，再让 manifest 顺序声明合法的
   `B -> workflows/b.py` 和与 A 冲突的 `C -> workflows/a.py`。composition 最终返回
   `WorkflowConflict invalid_input`，但数据库仍永久留下 A、B 两条 registration；本轮
   所称“注册前关闭全部身份”并未成立。
2. 在 `_contains_symlink(selected_root)` 返回 false 后、`resolve(strict=True)` 前，把
   selected root 换成指向 outside package 的 symlink。loader 成功返回
   `package_root=/tmp/.../outside/alpha_lab` 及 outside manifest 的声明，证明最初显式选择
   的 root 没有被 FD 固定。
3. source 标量包含 YAML `\0` 时，loader 泄漏裸 `ValueError: embedded null byte`，不是
   `SourceDeclarationError`；1000 层无 alias 的 sequence 泄漏裸 `RecursionError`。
4. 阻塞 `recover_registered_sources()` 时，另一个线程可通过
   `get_workflow_service()` 取得尚未完成 startup reconciliation 的 Service。
5. 注入“start 部分成功后失败、stop 又报告 monitor 仍存活”，composition 用 stop 错误
   覆盖原始 start 错误，仍清空全局、关闭 Store 并释放 lease；同一进程随后立即重新打开
   同一 workspace 成功。

## 3. Standards 轴

### 3.1 Blocking

#### S-B01：不可信 declaration 的 filesystem/parser seam 不是 fail-closed

**disposition：rejected-with-evidence。**

- 规则：`AGENTS.md:923-927` 要求只在显式 editable package root 下解析并拒绝 symlink、
  traversal 和非普通文件；02F 设计 `:50-56, 107-113` 要求不可信 YAML/路径错误以稳定
  `SourceDeclarationError` fail closed。
- 位置：`source_discovery.py:96-155, 178-180, 202-266`。
- 证据：
  - `_contains_symlink()` 是 pathname 逐段检查，随后另一次 `resolve()`；两者之间可替换
    root。上述 probe 已让 loader 接受最初 selected root 之外的 package。
  - `_regular_file_bytes()` 先 `lstat()`，再按完整 pathname `open()`；没有固定父目录 FD，
    `O_NOFOLLOW` 只保护最后一段。若 regular file 在两步之间被换成 FIFO，缺少
    `O_NONBLOCK` 的 `open()` 还可能无限阻塞，之后的 `fstat()` 无法补救。
  - manifest 和每个 source 都使用无 byte budget 的 `stream.read()`；YAML 被完整 parse
    两次且无 depth/entry/scalar budget。alias bomb 已正确拒绝，但深层普通容器仍可令
    parser 抛出裸 `RecursionError`。
  - NUL path 由 `lstat()` 抛出的 `ValueError` 未收敛；它绕过稳定 machine code。
- 影响：一次本地 package/Git 竞态或畸形 declaration 可以重定向显式 package 身份、
  卡死启动、耗尽内存/栈，或让配置错误逃逸为内部异常。OS 不能据此声称 closed/safe
  declaration。
- 最小修复证据：从显式 root 开始用 directory FD/openat 固定目录身份，所有目录和文件
  均 `O_NOFOLLOW`，文件 open 必须避免 FIFO 阻塞并以 `fstat` 验证；在同一固定身份上
  完成 manifest/source 验证。为 manifest 和 Draft 读取冻结明确 byte/depth/entry/scalar
  budget，并把 NUL、过深/过大、rename/symlink/FIFO race 统一收敛为稳定
  `SourceDeclarationError`。原 test-author 需增加上述 RED，而不能只测预先存在的 symlink
  或 FIFO。

#### S-B02：registration preflight 只按 workflow UUID 看既有状态，导致 partial mutation

**disposition：rejected-with-evidence。**

- 规则：02F 设计 `:80-82, 107-112, 126-130` 要求全部 manifest/path/跨 package identity
  检查先于任一注册，manifest 不可信时整体 fail closed，monitor 不观察半注册集合。
- 位置：`composition.py:42-97`。
- 证据：候选会检查新 declarations 之间的 UUID/path/URI，但把既有 registration 仅构造
  为 `workflow_uuid -> row`。它没有建立既有 `(package_root, relative_path)` 与
  `source_uri` 反向索引。因此前面的合法新声明先各自提交，后面的 path/URI 冲突才由
  SQLite unique index 发现。probe 已得到失败后的持久集合 A+B，而不是原集合 A。
- 影响：一个整体无效的显式 package 配置会在启动失败后改变 SQLite source identity；
  下次没有该 manifest 的启动也会恢复/监视遗留的 B。这是持久 partial configuration，
  不是可忽略的内存中失败。
- 最小修复证据：一个 deep registration module 必须先关闭新旧两侧 workflow/path/URI
  （以及同一 logical package id 到 physical root）的完整 identity，再在一个 Store
  transaction 内注册整批或保证精确 rollback。原 test-author 至少增加“先有 A，再声明
  B 后 C 冲突，失败后仍只有 A”的 RED，并覆盖跨 manifest package/path/URI identity。

#### S-B03：startup failure 重新引入了“monitor 未停仍释放 Authority lease”

**disposition：rejected-with-evidence。**

- 规则：`AGENTS.md:913-917` 要求一个 working_dir 只有一个 Authority；02F 设计
  `:89-98` 要求 register -> reconcile -> monitor，失败不能留下半启动 Authority，且
  必须先确认 monitor 停止、再关闭 Store/释放 lease。Phase 01A5 已明确关闭过相同
  lifecycle blocker：stop 或 close 失败时保留组合根与租约以便重试。
- 位置：`composition.py:184-210`。
- 证据：全局 `_service` 在 `recover_registered_sources()` 前已经发布；probe 能读取未恢复
  Service。更严重的是 except 使用嵌套 `finally`：`new_monitor.stop()` 抛错仍必然执行
  `new_service.close()`、清空全局和 `_release_workspace_lease()`；stop 错误还覆盖触发
  cleanup 的原始错误。probe 证明 stop 明确报告仍存活后，同 workspace 可立即重开。
- 影响：旧 monitor 可继续访问已关闭 Store，同时新的 Authority 已持有同一 workspace；
  这正是历史 gate 已禁止的双 Authority/关闭后使用竞态。发布未恢复 Service也允许调用方
  读取半启动状态。
- 最小修复证据：至少在 reconciliation 完成后才发布可取得的 Service；若 start 已部分
  启动，stop 未确认或 Store close 未确认，则必须保留 Service/monitor/lease 的可重试
  ownership，绝不能释放 workspace。保留主失败并记录 cleanup failure，或以结构化异常
  同时保留两者，不能静默遮蔽。新增 recover gate 可见性、partial-start+stop-failure、
  startup-close-failure 和第二进程 lease 拒绝 RED，并保留既有 startup-scan gap 行为。

### 3.2 Non-blocking

#### S-NB01：source discovery 的实现没有形成设计所称的 deep module

**disposition：non-blocking-follow-up。**

- 设计 `:58-74` 只给出两个小 Interface，并称 declaration 为内部 immutable data；当前
  `source_discovery.py:293-299` 却公开两个 declaration dataclass，composition 又在
  `:42-97` 解包内部字段并复制一套 registration loop。指定的
  `register_editable_package_sources()` 只被测试使用，production composition 不调用它。
- 判断：这是 **Duplicated Code / Speculative Generality** 的 judgement-call smell。
  删除这个 public registration 函数不会把复杂度推回 production caller，说明它当前尚未
  获得 leverage；反过来，新旧 identity 规则已经因两条路径而不同，S-B02 是 locality
  不足的现实后果。
- 后续：修复 S-B02 时让 source discovery/registration module 隐藏 declaration shape 并
  拥有单 package 与多 package 的共同 preflight/batch 行为；composition 只负责 lease、
  recovery、monitor 的顺序。该项不单独阻塞，不能用保留现状来关闭 S-B02。

## 4. Spec 轴

### 4.1 Blocking

#### P-B01：closed/safe manifest、containment race 和稳定 declaration error 未实现

**disposition：rejected-with-evidence。**

- 规范：02F 设计 `:40-56, 105-113, 119-124`。
- 实际：普通 alias/tag/duplicate key、静态 symlink/FIFO 已拒绝；但是显式 root 的
  check/use race 可转向 outside，FIFO replacement 可阻塞，NUL/深容器分别泄漏
  `ValueError`/`RecursionError`，文件与 YAML 也没有显式资源 budget。
- 验收：需要 S-B01 所列 race/resource/error RED 全绿，并证明错误不包含外部文件内容。

#### P-B02：跨既有 source identity 的 fail-closed registration 未实现

**disposition：rejected-with-evidence。**

- 规范：02F 设计 `:80-82, 107-112, 126-130`。
- 实际：两个全新 manifest 的重复 workflow UUID 能在注册前拒绝，但既有反向 path/URI
  不在 preflight 中；已实证一个失败 composition 持久新增 B。
- 验收：完整 existing/new identity matrix 和 batch all-or-none 测试必须证明失败前后
  registration 集合逐字段相同。

#### P-B03：startup ordering/failure cleanup 未满足冻结 lifecycle

**disposition：rejected-with-evidence。**

- 规范：02F 设计 `:84-103, 126-128` 与既有 Phase 01 monitor/store shutdown 合同。
- 实际：register -> reconcile -> monitor 的正常 trace 通过，但 Service 在 reconcile 前
  已公开；cleanup 在 stop 明确失败后仍关闭 Store/释放 lease。新测试只覆盖 manifest
  失败（此时 monitor 尚未创建），没有制造部分 start、stop/close failure 或半发布。
- 验收：S-B03 所列 lifecycle RED、原 Phase 01A4/A5 跨进程合同和完整 gate 都必须通过。

### 4.2 Non-blocking

#### P-NB01：设计标为 internal 的 declaration shape 成为 production coupling

**disposition：non-blocking-follow-up。**

设计 `:67-74` 明确把 declaration 数据列为内部；候选却从 module 导出并让 composition
依赖其字段。当前不改变 wire/runtime 语义，因此不单独阻塞；应随 P-B02 的 batch
registration 修复一起收回内部，避免未来 source identity 字段变更要求 shotgun surgery。

## 5. 已确认通过的范围

1. manifest 基本 closed shape、canonical non-nil UUID、source 的固定三段形式、未知字段、
   duplicate key、alias、explicit tag、多文档和非法 UTF-8 正常 fail closed；不扫描未声明
   `.py`，不从 Python 猜 UUID。
2. composition 正常路径按传入顺序注册两个显式 package，随后 recovery，再启动唯一
   monitor；不同 working_dir/compiler/package-root 配置不能热切换，相同对象/配置幂等。
3. 声明不暗建 Workflow、不直接调用 Store 私有接口；missing source 不被创建，启动状态
   为 `draft_missing`。
4. watcher 对稳定 signature 去抖、burst coalesce、processed signature 去重；same-hash
   rewrite 不产生事件；delete/rename 只使 canonical path missing，restore 发
   `cause=recovered`，Applied Workflow 保留。
5. 既有 monitor transient retry、startup scan 与 monitor start 间的外部改动、reset stop
   timeout、Service close failure、跨进程 lease 和单 token Apply 合同仍通过。
6. diff 没有 compiler/Catalog production wiring、Draft PUT/Apply wire 变化、Task/Job、
   Scheduler/device、Frontend 或 Backend 修改。

## 6. 独立测试覆盖评价

独立测试提供了 52 个 02F case，正常合同覆盖面良好，也没有删除、skip、xfail 或弱化
既有测试；但下列恰好是当前 blocker 的漏测：

- containment race 只在 discovery 完成后替换 source root，并依赖 Service 二次检查；没有
  在 selected manifest root 的 check/resolve/open 之间换 symlink/rename，也没有 regular
  -> FIFO race；
- YAML safety 只测 alias/tag/multi-doc/encoding，没有 byte/depth/entry/scalar budget、NUL
  path 或稳定 machine code 的精确值；`_assert_declaration_error` 只要求 code 非空；
- cross-manifest test 只覆盖两个全新声明的重复 workflow UUID，没有既有 registration 的
  反向 path/URI/package identity，也没有验证失败后 DB 集合不变；
- failure cleanup 只在 manifest parsing 阶段失败，此时 monitor 未启动；没有 partial start、
  stop failure、close failure、lease retention 或 recover 前不可见性。

原 test-author 应先为三项 blocking 补 RED；不能通过放宽、删除或 xfail 现有测试关闭门禁。

## 7. 复审与合并门

当前 `b08ea30` **不得合并**。修复后必须：

1. 由原 02F test-author 提交 S/P-B01～B03 的独立 RED；
2. production fix 不接入 02G、Frontend 或 Backend；
3. 形成新的精确候选 SHA，重跑 02F 目标、Phase 01 lifecycle/security 累积、完整
   `tests/`、configured Ruff/format 和 `git diff --check`；
4. 由同一 `round02f_review` reviewer 逐项给出
   `accepted-fixed` 或 `rejected-with-evidence`；
5. Standards 与 Spec blocking 均为 0 后，才允许非 squash 本地合入
   `integration/workflow-task-runtime`。

最终双轴计数：

- Standards：Blocking `3`；Non-blocking `1`。
- Spec：Blocking `3`；Non-blocking `1`。
- 合并结论：**不允许**。
