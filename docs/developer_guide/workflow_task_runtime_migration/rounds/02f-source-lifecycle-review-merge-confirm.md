# Round 02F：package source lifecycle 最终合并确认

## 1. 固定对象与最终结论

- 基线：`4875cc1ac753ad551f9273a6fa0e84126b67fd89`
- 上次仍被拒绝的 production 候选：
  `f70b13795528fee4a73f1beb063364cee552c91c`
- 最后一批 finding tests：
  `73c44cab8a49be74716107112a1ca20e0b3dda6f`
- 本次精确 production 候选：
  `375f433ac3de53675cedada74885b29cb01bf251`
- reviewer：`round02f_review`；与前几次相同，未参与 production 或 finding tests 编写。

review branch 依次 cherry-pick 最后一批 tests 与 production 后的 HEAD 是 `a7985ea`。
`git diff 375f433 HEAD -- unilabos tests` 为空，因此本报告读取和验证的 production/test
树与精确候选 `375f433` 完全等价。

结论：**允许精确候选 `375f433` 合并。** 上次剩余的 S/P-B01 已关闭；identity、B02、
B03、NB01 没有回归。本轮没有 Frontend 或 Backend 变化，也没有越过 02G production
composition 停止线。

最终双轴计数：

- Standards：Blocking `0`；Non-blocking `0`。
- Spec：Blocking `0`；Non-blocking `0`。

## 2. 最后一个 S/P-B01 的关闭证据

### 2.1 成功 CAS 的所有真实 source read/hash 都显式受限

`WorkflowService._read_regular_fd()` 的 `byte_limit` 已改为无默认值的必填 keyword；
`_hash_regular_fd()` 同样要求调用方显式给出预算。逐项检查全部 production caller：

1. canonical target 的旧内容读取显式传入 `AUTHORING_SOURCE_BYTE_LIMIT`；
2. temporary source 首次 hash 显式传入同一上限；
3. replace 后、lease 释放前的 published source hash 显式传入同一上限；
4. lease 释放后的最终 published source hash 仍显式传入同一上限；
5. 常规 Authoring source GET/reconcile 读取继续显式使用同一上限。

finding test 同时记录成功保存与 target-growth 冲突路径中每一次真实 helper 读取，所有记录的
limit 都精确等于 `8 * 1024 * 1024`。必填参数也使未来新增 caller 无法静默退化到无界读取。

### 2.2 target-growth 不读取超限正文，并稳定返回 Draft 冲突

finding test 在第一次有界 source 读取之后、真正 atomic CAS 之前，把 canonical source 原地
扩大到 `8 MiB + 1`。CAS 打开 target FD 后，`_read_regular_fd()` 先以 `fstat` 检查
`st_size`；发现大于上限即抛出 `invalid_input`，发生在 `lseek`/`os.read` 之前，因此不会把
超限正文读入内存。若文件在 `fstat` 后动态增长，读取循环也只允许累计到 `limit + 1` bytes
用于发现越界，不会随外部文件大小继续增长。

CAS seam 将该内部 `invalid_input` 稳定映射为外部 `draft_hash_conflict`。该语义是正确的：
调用方最初观察到的 Draft 已在 CAS 前被外部 authority 改变，而不是客户端提交格式突然失效。

### 2.3 冲突路径不污染 source、record、event 或 artifact

finding test 逐项确认 target-growth 冲突后：

- canonical source 的大小与 SHA-256 仍精确等于外部写入的 `8 MiB + 1` 内容；
- authoring record 与冲突前逐字段一致；
- event 列表与冲突前一致；
- source 目录只剩 canonical 文件，没有遗留 `.tmp` 或 `.cas` artifact。

因此本修复没有用历史内容覆盖外部 authority，也没有产生错误的数据库或 SSE 事实。

### 2.4 内部 write/restore seam 也有防御性上限

`_atomic_write()` 在创建 temporary 文件前拒绝大于 8 MiB 的 content；
`_write_regular_fd()` 与 `_restore_regular_fd()` 的 `byte_limit` 也成为必填参数，并在写入前
检查 content 长度。当前即使上层 `save_draft()` 已做入口校验，内部 helper 也不能被未来 caller
无意用作绕过点。

## 3. 先前 finding 的回归确认

### identity / S/P-B01 其余部分

保持 **accepted-fixed**。loader 获取的 package source-root identity 继续贯穿 registration；
Store batch 写入前以及 commit 前的 identity 复核、失败整批 rollback、Service 不提前公开均未
改变。外部大文件的 GET/reconcile 仍稳定 `invalid_input`，且不污染 record/event。

### S/P-B02

保持 **accepted-fixed**。workflow/path/URI/package identity 全量 preflight、真实 Store
transaction 与 mid-batch rollback 没有被最后一次 CAS 修改触碰，累计 finding tests 全绿。

### S/P-B03

保持 **accepted-fixed**。recovery 前 Service 不可见、monitor-start gap、partial start 的
stop/close failure、failed ownership、lease retention 与 retry cleanup 均继续通过 Phase
01A4/A5 及专项回归。

### S/P-NB01

保持 **accepted-fixed**。composition 仍只依赖
`register_editable_package_sources(...)` deep Interface；source-discovery module 继续独占
identity pin、全量 preflight 与 batch proof。

## 4. 最终验证门

| 检查 | 结果 |
|---|---|
| 02F 原目标 + 全部 finding cases + Phase 01A4/A5 + startup-scan/retry | `75 passed, 1 warning in 8.56s` |
| Ruff `E,F,I`（4 个相关 production 文件、3 个相关测试文件） | 通过 |
| Ruff format check（同上） | 通过，`7 files already formatted` |
| `git diff --check 4875cc1...375f433` | 通过 |
| review HEAD 与 `375f433` production/test tree | 完全等价 |
| 主代理精确候选完整测试门 | `1684 passed, 3 skipped, 18 warnings` |

专项门的 warning 是既有 FastAPI TestClient/httpx 弃用提示，不是本轮失败；完整门的 warnings
也没有测试失败。完整门数字由主代理在精确候选上执行并提供，本 reviewer 独立执行了上表其余
各门，并逐项阅读最后 tests 与 production diff。

## 5. 合并与后续边界

精确候选 `375f433` 已满足 02F 合并条件。允许合并范围为：

1. 精确 production/tests 候选 `375f433`；
2. 02F 设计、趋势、测试与 reviewer 的 documentation-only commits；
3. 按 round ledger 记录后执行非 squash 的本地合并。

不要把 02G、Frontend 或 Backend 的新实现夹带进 02F 合并。02F 合并后，这些内容应继续在
各自独立工程 Round 与独立分支上推进。
