# Round 02F：package source lifecycle 最终修复确认

## 1. 固定对象与结论

- 基线：`4875cc1ac753ad551f9273a6fa0e84126b67fd89`
- 上次被拒绝候选：`a6eecfbb1dfb2a06871a048a28583637b45a2fd2`
- 第二批 finding tests：`4a493fbe09d664921d87e769176ca610531fd2ff`
- 本次 production fix：`f70b13795528fee4a73f1beb063364cee552c91c`
- reviewer：`round02f_review`；与前两次相同，未参与实现或测试编写。

review branch 依次 cherry-pick 第二批 RED 与 production fix 后的 HEAD 是 `1a29564`；
`git diff f70b137 HEAD` 为空，且 production/test scoped diff 也为空，因此本次读取、测试和
probe 的树与精确候选 `f70b137` 等价。

结论：**暂不允许 `f70b137` 合并。** 上次剩余 S/P-B01 的两个主要行为已经关闭：

1. loader 验证的 package source-root identity 已带入 registration，并在同一 Store batch
   transaction 内写前固定、写后/commit 前复核；不一致会整批 rollback，Service 不公开；
2. 8 MiB source budget 已覆盖真实 Authoring GET/reconcile 读取与 Draft PUT 输入，超限为
   稳定 `invalid_input`，不修改 authoring record，也不产生 event。

但是 production fix 没有把同一 budget 传进 Draft CAS 的旧文件读取与 hash helper。
外部进程可在第一次有界 `_read_source()` 之后、`_compare_and_replace()` 读取目标 FD 之前
把文件扩大；候选最终虽返回正确的 `draft_hash_conflict`，却已把超限文件以
`byte_limit=None` 完整读入内存。该行为由最小 probe 实证，仍属于本轮明确要求检查的
“相关 write/hash seam”，故 S/P-B01 仍有一个 blocking。

最终计数：Standards Blocking `1`、Non-blocking `0`；Spec Blocking `1`、
Non-blocking `0`。B02、B03、NB01 没有回归。没有 Frontend 或 Backend 变化，也没有越过
02G production composition 停止线。

## 2. reviewer 验证

| 检查 | 结果 |
|---|---|
| 02F 原目标 + 全部 finding cases + Phase 01A4/A5 + startup-scan/retry | `74 passed, 1 warning in 8.47s` |
| Ruff `E,F,I`（本轮相关 production/tests） | 通过 |
| Ruff format check（同上） | 通过 |
| `git diff --check 4875cc1...f70b137` | 通过 |
| review HEAD 与 `f70b137` production/test tree | 完全等价 |

warning 是既有 FastAPI TestClient/httpx 弃用提示，不是本轮失败。

reviewer 逐项阅读了第二批 RED、`source_discovery.py` 的 identity pin/transaction proof、
`service.py` 的真实 read/write/CAS/hash 路径，并复核 Store batch 与 composition ready/failed
ownership；没有只认可测试输出。

## 3. S/P-B01 disposition

### 3.1 loader→registration directory identity

**accepted-fixed。**

- `_EditablePackageManifest` 保存 loader 实际打开的 package source-root
  `(st_dev, st_ino)`，不把 caller path 本身当作 identity proof。
- `_pinned_package_roots()` 在 preflight 后重新逐段 `O_DIRECTORY | O_NOFOLLOW` 打开当前
  root，必须与 loader identity 相同，并持有 FD 直到 registration 结束。
- production Service 的 `editable_source_registration_batch()` 打开 SQLite transaction；
  全部既有单-source registration 写入后，`_assert_pinned_package_roots()` 在 transaction
  尚未 commit 时再次打开 pathname，与持有 FD 比较。失败异常穿出 context，Store 整批
  rollback。
- finding test 以 Linux `renameat2(RENAME_EXCHANGE)` 在 loader 返回后把 selected root
  原子换成另一个普通、非 symlink directory。结果为
  `SourceDeclarationError invalid_package_root`，registration 前后均为 `[]`，
  `get_workflow_service()` 为 `None`。

这关闭了前次 probe 的普通目录 generation replacement；symlink/FIFO、loader 内 rename、
path/URI/package collision tests 也继续全绿。

### 3.2 真实 GET/reconcile 与 event/record

**accepted-fixed。**

- `AUTHORING_SOURCE_BYTE_LIMIT = 8 MiB` 成为 discovery 与 Service 共用的 source contract；
  discovery 不再维护第二个不同常量。
- `_read_source()` 通过 `_read_regular_fd(..., byte_limit=...)` 同时检查 `fstat.st_size` 和
  动态增长，GET、reconcile、Apply 前读取均不能返回或编译超限内容。
- Draft PUT 在写文件前对精确 UTF-8 bytes 检查同一上限。
- 新测试先建立 `draft_missing` baseline，再外部写入 `8 MiB + 1`。GET 与 reconcile 都
  稳定返回 `WorkflowError invalid_input / 提交内容格式不正确`；authoring record 与调用前
  逐字段相同，event 仍为 `[]`。

### 3.3 CAS write/hash budget

**rejected-with-evidence，仍 blocking。**

代码证据：

- `save_draft()` 的第一次 `_read_source()` 有 8 MiB limit；
- 但 `_compare_and_replace()` 在 `service.py:1392` 调用
  `_read_regular_fd(target_descriptor)`，没有传 `byte_limit`；
- `_hash_regular_fd()` 在 `:1579-1580` 也固定调用无 limit 的 `_read_regular_fd()`，其
  三个发布前后 hash caller 都没有显式 source budget。

只读 probe 通过公开 `save_draft()` 制造真实 CAS race：baseline 为 4 bytes，第一次
`_read_source()` 后、原 `_atomic_write()` 前把 canonical source 写成 `8 MiB + 1`。记录到
两次 helper 调用：

```text
(byte_limit=8388608, returned=4)
(byte_limit=None, returned=8388609)
```

最终 response path 是稳定 `draft_hash_conflict`，且没有错误写入；但安全 budget 已被第二次
无界读取绕过。若外部文件远大于 8 MiB，这条 CAS 冲突路径仍可造成与文件大小线性增长的
内存占用。

最小关闭方式：原 test-author 增加一个 CAS race RED，证明第二次 target-FD 读取有界且
超限仍映射为 `draft_hash_conflict`（因为 caller 观察后文件已变化），record/event/source
均不被错误提交。production 应：

1. 给 CAS target old-source read 传同一 `AUTHORING_SOURCE_BYTE_LIMIT`；
2. 让 `_hash_regular_fd`/相关发布校验显式接受并使用同一 limit，或用能证明相同上限的
   bounded streaming hash；
3. 防御性限制内部 write/restore content，避免未来 caller 绕过 `save_draft()` 的入口检查；
4. 保留当前 Draft hash conflict、lease、backup artifact 和 CAS rollback 合同。

这不是新增设计要求，而是本次任务明确要求复核的“相关 write/hash seam”，也是前次报告
要求把 source budget 深化到全部真实 Draft read/hash/write seam 的最后一个缺口。

## 4. 上轮 finding 回归

### S/P-B02

**保持 accepted-fixed。** existing/new workflow/path/URI/package identity preflight、真实
Store transaction 和 mid-batch rollback 没有变化；全部相关 tests 继续通过。

### S/P-B03

**保持 accepted-fixed。** recovery 前 Service 不可见、recovery 后 monitor-start gap、
partial start 的 stop/close failure、failed ownership、lease retention 与 retry cleanup
全部继续通过 Phase 01A4/A5 和 finding tests。

### S/P-NB01

**保持 accepted-fixed。** composition 仍只依赖
`register_editable_package_sources(...)` deep Interface；identity pin、全量 preflight 与
batch proof 都留在 source-discovery module，internal declaration 没有重新暴露。

## 5. 最终合并门

当前 `f70b137` **不得合并**。下一候选只需：

1. 由原 test-author 增加一个 CAS target-growth RED；
2. production 将 8 MiB budget 贯穿 CAS old-source read、hash 与防御性 write/restore seam；
3. 重跑 02F/finding/Phase lifecycle 累积、完整 tests、Ruff、format 与 diff-check；
4. 由同一 reviewer 对新精确 SHA 确认最后一个 S/P-B01；
5. 不修改 02G、Frontend 或 Backend。

最终双轴计数：

- Standards：Blocking `1`；Non-blocking `0`。
- Spec：Blocking `1`；Non-blocking `0`。
- 合并结论：**不允许**。
