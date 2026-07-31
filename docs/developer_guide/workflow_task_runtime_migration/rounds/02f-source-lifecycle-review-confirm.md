# Round 02F：package source lifecycle 修复确认

## 1. 固定对象与结论

- 基线：`4875cc1ac753ad551f9273a6fa0e84126b67fd89`
- 首次被拒绝候选：`b08ea30c7ee2a9419cc82eed3450df5b06291e5f`
- 首次评审报告：`0f63eb8c5180b1b2f848cd781b10fee0ee09ac0a`
- finding tests：`d5309f1`
- production fix：`a6eecfbb1dfb2a06871a048a28583637b45a2fd2`
- reviewer：`round02f_review`；与首次评审相同，未参与实现或测试编写。

review branch 依次 cherry-pick finding tests 与 production fix 后的 HEAD 是 `4af399a`；
`git diff a6eecfb HEAD` 为空，且 `git diff --exit-code a6eecfb HEAD -- unilabos tests`
返回 0，因此本次实际读取、测试和 probe 的 production/test 树与精确候选 `a6eecfb`
完全等价。

最终结论：

- **S/P-B02 accepted-fixed**：existing/new physical path、source URI、package identity 已
  完整预检，真实 WorkflowService/Store batch 在一个 SQLite transaction 中 all-or-none；
- **S/P-B03 accepted-fixed**：recovery 前不可见，既有 monitor-start gap 合同保留，
  partial start 的 stop/close cleanup 失败会隐藏 Service、保留 ownership/lease 并可重试；
- **S/P-NB01 accepted-fixed**：composition 只依赖 source discovery 的一个 deep
  registration Interface，declaration 类型已收回 module 内部；
- **S/P-B01 仅部分修复，仍为 blocking**：loader 内的 dirfd、budget、stable error 和
  FIFO race 已关闭，但固定目录 identity 没有跨越 loader→registration handoff，8 MiB
  source budget 也没有进入注册后的 Authoring Draft/watcher 读取 seam。

因此 Standards 最终为 Blocking `1`、Non-blocking `0`；Spec 最终为 Blocking `1`、
Non-blocking `0`。**不允许 `a6eecfb` 合并**，即使主代理记录的完整 gate 已是
`1681 passed, 3 skipped`；测试全绿不能替代这两个可复现的 B01 trust-boundary 缺口。

本轮仍未修改 Frontend 或 Backend，也没有接入 02G compiler/Catalog production
composition。

## 2. reviewer 验证证据

| 检查 | 结果 |
|---|---|
| 02F 原目标 + 15 个 finding cases + Phase 01A4/A5 + startup-scan/retry | `72 passed, 1 warning in 8.42s` |
| Ruff `E,F,I`（四个 production 文件与三份 02F tests） | 通过 |
| Ruff format check（同上） | 通过 |
| `git diff --check 4875cc1...a6eecfb` | 通过 |
| review HEAD 与 `a6eecfb` production/test tree | 完全等价 |
| 主代理完整 gate | `1681 passed, 3 skipped` |

warning 是既有 FastAPI TestClient/httpx 弃用提示，不是本轮失败。

reviewer 完整阅读了 15 个参数化后 finding cases、`source_discovery.py`、
`composition.py`，以及 `service.py`/`store.py` 的完整 production fix 与其既有
Authoring/path/transaction collaborators；没有只认可测试输出。

另执行三个不修改仓库的最小 probe：

1. 在真实 batch 中让第二次 `register_editable_source()` 抛出
   `WorkflowError internal_error`，第一条已执行 upsert；退出后
   `list_registered_sources()` 仍为 `[]`，证明 Store transaction rollback 有效。
2. 注册完成后，由外部程序把 canonical Draft 写成 `8 * 1024 * 1024 + 1` bytes；
   `WorkflowService.get_authoring()` 没有拒绝，而是成功读取并返回完整 `8388609` bytes。
3. `load_editable_package_manifest()` 成功后、batch registration 前，把 selected root
   原目录 rename 保存，再把另一个普通、非 symlink package directory rename 到同一路径；
   registration 成功，持久绑定的新路径内容是 `replacement-root`，而不是 loader 实际
   验证过的 `validated-root`。

## 3. Standards disposition

### S-B01：declaration filesystem/parser seam 不是 fail-closed

**disposition：rejected-with-evidence（部分 accepted-fixed）。**

已确认关闭：

1. `source_discovery.py` 从显式 root 开始以逐段
   `O_DIRECTORY | O_NOFOLLOW` 的 directory FD 打开，并比较初始/打开 root 的
   `(st_dev, st_ino)`；finding tests 的 symlink 与普通 rename replacement 均返回
   `invalid_package_root`。
2. manifest/source 最终文件以 `openat + O_NOFOLLOW + O_NONBLOCK` 打开，再用 `fstat`
   要求 regular file；regular→FIFO race 在独立 spawn process 中有界返回
   `invalid_workflow_source`，没有阻塞。
3. loader 冻结 manifest `1 MiB`、source `8 MiB`、YAML depth `32`、workflow entry
   `1024`，并在事件层约束 node/scalar；alias/tag/duplicate key、多文档、invalid UTF-8、
   NUL、过深/过大均收敛为不泄漏内容的稳定 `SourceDeclarationError`。

仍未关闭：

1. **FD identity 在 registration 前丢失。** `_EditablePackageManifest` 只保存
   `package_root: Path`；loader 在返回前关闭全部 root/source-root FD。
   `register_editable_package_sources()` 随后把 pathname 交给
   `WorkflowService.register_editable_source()`，后者只证明“现在这个 pathname”安全，
   没有比较 loader 验证时的 `(st_dev, st_ino)`。上述 probe 已把同一路径换成另一个普通
   directory 并成功注册。候选防住了 check→open 的 symlink race，却没有把同一 identity
   证明带过 load→register seam。
2. **source budget 只存在于 discovery。** `source_discovery.py` 的 8 MiB 上限只验证启动
   时已经存在的 source；真正处理外部 Git/coding-agent 写入的
   `WorkflowService._read_source()` 仍在 `service.py:1199` 执行无界 `stream.read()`，
   `_read_regular_fd()`/`_hash_regular_fd()` 同样无界。watcher/reconcile、Authoring GET 和
   后续 CAS 因而可在注册后读取任意大文件。8388609-byte probe 已实证接受。

这两点都属于首次 S-B01 明确要求的“在同一固定身份上完成验证”和“为 manifest 与 Draft
读取冻结 budget”，不是新的无界审查范围。

关闭证据必须由原 test-author 增加两个 RED：

- 在 loader 返回后、首个 Service registration 前以另一个普通 directory 替换 root，
  要求整个 batch 失败且 registration 集合不变；修复必须把 FD identity/expected identity
  带到 Service 实际打开 package root 的位置，不能再做一个可竞态的 pathname 预检查；
- 先合法注册，再外部写入 `8 MiB + 1` Draft，经 monitor/reconcile 与 Authoring GET 均
  有界、稳定失败且不返回/编译超限内容；共享一个 source-byte contract，覆盖
  `_read_source`、CAS/hash 和写入入口，不能只在 discovery 复制常量。

### S-B02：existing registration preflight 与 partial mutation

**disposition：accepted-fixed。**

- `source_discovery._preflight_existing_identity()` 现在同时建立 existing workflow、
  `(package_root, relative_path)`、`source_uri` 和 `package_id -> package_root` 索引，并对
  全部 new rows 做相同集合检查；physical path、URI、package identity 三种碰撞 tests
  均证明失败前后 existing rows 逐字段相等。
- `WorkflowStore.source_registration_batch()` 让既有单-source Service Interface 共享一个
  `BEGIN IMMEDIATE` transaction；`register_source()` 在 owner thread 复用同一 connection，
  任一异常由外层 transaction rollback。额外 probe 证明第二次注册的非 identity 异常也
  会撤销第一次 upsert。
- Service 仍负责每个 Workflow 的存在性、path/relative shape 与 Store error mapping；
  discovery 没有直接写 Store，也没有暗建 Workflow。

首次 S/P-B02 关闭。

### S-B03：startup publication 与失败停机 ownership

**disposition：accepted-fixed。**

- `recover_registered_sources()` 完成后才 `_retain_runtime(..., ready=True)`；recovery gate
  finding test 证明期间 `get_workflow_service()` 为 `None`。
- retain 仍发生在 `monitor.start()` 前，既有 startup-scan gap test 继续证明该间隙的外部
  文件变化会由 monitor 从空 signature 集捕获，没有为了隐藏未恢复 Service 而丢事件。
- `_ready=False` 明确表示 failed-cleanup ownership；`get_workflow_service()` 不公开它，
  同进程 compose 要求先 cleanup。stop 失败时不 close Store，stop 或 close 任一失败时
  都不 clear/release lease。
- partial start + stop failure test 同时保留 START/STOP 两条异常证据，失败期间第二进程
  被 lease 拒绝，reset 第二次 stop 后才能打开；recovery + close failure 对主错误、清理
  错误、lease 和二次 close 的证明相同。Phase 01A4/A5 正常/失败停机合同仍绿。

首次 S/P-B03 关闭。

### S-NB01：source discovery 未形成 deep module

**disposition：accepted-fixed。**

- composition 现在只 import/call `register_editable_package_sources(...)`，不再 import、
  解包或复制 manifest/declaration 字段和 identity loop。
- declaration dataclass 与 registrar Protocol 均以下划线命名，`__all__` 只保留稳定错误和
  两个函数 Interface；多-package preflight、existing identity、batch capability 全部位于
  source-discovery module 内。
- 删除该 module 会把 manifest/path/identity/batch complexity 推回 composition，当前
  module 已通过一个小 Interface 为 caller 提供实际 leverage/locality，不再是首审中的
  pass-through。

首次 S/P-NB01 关闭；本次不保留新的 Standards non-blocking。

## 4. Spec disposition

### P-B01：closed/safe manifest、containment 与稳定 error

**disposition：rejected-with-evidence（部分 accepted-fixed）。**

02F 设计 `:50-56, 105-113, 119-124` 的 loader 内 closed YAML、静态/竞态 symlink、
regular UTF-8 file、FIFO、NUL 和显式 budget 均已有绿灯证据；但是设计同时要求 source
只绑定显式 loaded editable package root，并由 Service 处理后续外部 Draft 生命周期。

候选未证明 registration 使用 loader 验证的同一 directory identity，也未对注册后的
外部 Draft 维持同一 8 MiB budget。两个 probe 分别实证错误 package generation 被绑定、
超限 Draft 被完整返回。因此 P-B01 仍 blocking。

### P-B02：跨 existing identity 的 fail-closed batch

**disposition：accepted-fixed。**

三种 existing collision、全新 declarations 重复、missing Workflow 与额外 mid-batch
异常均在任何持久 partial registration 外失败；正常多 package 顺序仍 deterministic，
不扫描 `.py`、不暗建 Workflow。P-B02 关闭。

### P-B03：startup ordering/failure cleanup

**disposition：accepted-fixed。**

reconcile 前不可见、reconcile 后 monitor-start gap 捕获、partial start stop failure、startup
close failure、第二进程 lease retention、retry cleanup 及 Phase 01A4/A5 全部有公共 seam
证据。P-B03 关闭。

### P-NB01：internal declaration shape 泄漏

**disposition：accepted-fixed。**

composition 不再依赖 internal declaration，公开 surface 也不再导出这些 dataclass；修复
没有改变 wire DTO、Draft PUT/Apply 或 02G/Frontend/Backend 范围。P-NB01 关闭。

## 5. finding tests 评价

15 个参数化后 cases 没有弱化原测试，能可靠抓住：

- loader 内 selected-root symlink/rename identity race、regular→FIFO bounded failure；
- NUL、manifest/source bytes、YAML depth/workflow entry/scalar budgets 与内容不泄漏；
- existing physical path/source URI/package identity 的集合不变；
- recovery visibility；
- partial start/stop failure 与 recovery/close failure 的双异常证据、lease retention 和 retry。

仍缺的两个 case 与剩余 B01 精确对应：

1. root 在 **loader 返回后、registration 前** 换为另一个普通 directory；
2. source 在 **registration 完成后** 被外部程序扩大到 8 MiB 以上，再经过真实
   Service/monitor/GET seam。

原 test-author 应只补这两个 RED，现有 15 case 全部保留。

## 6. 下一次确认门

当前 `a6eecfb` **不得合并**。下一候选只需关闭剩余 S/P-B01：

1. 由原 02F test-author 增加上述两个独立 RED；
2. production 把 selected package identity 带过实际 registration open，并把 source budget
   深化到 WorkflowService 的全部真实 Draft read/hash/write seam；
3. 重跑 02F 目标、72 项 reviewer 累积、Phase 01 lifecycle/security、完整 tests、Ruff、
   format 与 diff-check；
4. 由同一 reviewer 对新精确 SHA 确认 B01；
5. 不得借机接入 02G、Frontend 或 Backend。

最终双轴计数：

- Standards：Blocking `1`；Non-blocking `0`。
- Spec：Blocking `1`；Non-blocking `0`。
- 合并结论：**不允许**。
