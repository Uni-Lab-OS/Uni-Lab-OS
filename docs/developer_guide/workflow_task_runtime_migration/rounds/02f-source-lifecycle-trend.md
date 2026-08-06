# Round 02F：package source lifecycle 趋势与策略报告

日期：2026-08-01

实现分支：`migration/02f-source-lifecycle`

基线：`4875cc1`

固定 production/test 候选：`375f433`

状态：**本轮目标、Workflow 累积与完整门禁全绿；同一独立 reviewer 最终确认
Standards/Spec 均为 0 blocking、0 non-blocking，允许非 squash 本地合并。**

## 1. 本轮交付

02F 把显式 editable package 声明接到 Phase 01 已有的持久 Authoring Authority：

```text
package.yaml
  -> closed manifest loader
  -> 全量 identity preflight
  -> 单一 Store transaction 注册
  -> 启动 reconcile
  -> Draft monitor
```

交付内容：

- 只读取显式配置的 package root，不扫描未知 `.py`，也不暗中创建 Workflow；
- `package.yaml` 使用 closed YAML：拒绝 alias、tag、重复 key、未知字段和多文档，并冻结
  1 MiB manifest、32 层深度、1024 个 workflow entry、1 MiB scalar 预算；
- source 必须是 `<package>/workflows/<name>.py`，由 directory FD、`O_NOFOLLOW`、regular-file
  检查和 8 MiB 预算保护；
- loader 保存实际 source-root `(st_dev, st_ino)`，注册前 pin FD，并在同一 SQLite
  transaction 内写后、commit 前再次证明 pathname 仍指向同一目录；
- Workflow UUID、physical path、`package://` URI 和 package identity 在新声明与既有持久
  registration 之间全量预检，任一冲突整批回滚；
- 真实 GET、reconcile、Draft PUT、CAS target read、temporary/published hash 及内部
  write/restore 共用同一 8 MiB source contract；
- 超限外部 Draft 的 GET/reconcile 稳定为 `invalid_input`；CAS 窗口内增长稳定为
  `draft_hash_conflict`，均不污染 Authoring record、durable event 或临时 artifact；
- composition 遵守 `register -> recover -> monitor start -> publish ready`，部分启动、stop
  或 close 失败时保留内部 ownership 与进程 lease，允许 reset 重试清理。

本轮没有接入 02G compiler/Catalog production composition，没有修改 Frontend、Backend、
Task/Job、Scheduler 或设备执行路径。

## 2. 独立 TDD 与评审 provenance

| 阶段 | 唯一 subagent | 提交 | 结果 |
|---|---|---|---|
| 原始合同测试 | `round02f_test` | `7af9159` | 52 项：48 RED、4 个既有 watcher guard GREEN |
| 首次双轴评审 | `round02f_review` | `5668537` | Standards 3B/1NB；Spec 3B/1NB |
| 首批 finding tests | 同一 test-author | `d5309f1` | 15 项精确 RED，覆盖 B01～B03 |
| 首次修复确认 | 同一 reviewer | `7f92d75` | B02、B03、NB01 关闭；B01 剩 2 个 seam |
| identity/runtime-read tests | 同一 test-author | `da14627` | 2 RED：普通目录 exchange、注册后超限 Draft |
| 第二次修复确认 | 同一 reviewer | `f0311de` | identity 与 GET/reconcile 关闭；CAS budget 仍 1B |
| CAS-race test | 同一 test-author | `7bc4c7f` | 1 RED：成功/冲突路径仍出现 `byte_limit=None` |
| 最终修复确认 | 同一 reviewer | `0fa49d0` | 75 项独立目标全绿；双轴均为 0B/0NB |

全程只有一个 test-author 和一个 reviewer；finding 与复核均由原角色继续，两个 subagent
没有并发运行，也没有让实现者修改独立测试来制造 GREEN。

## 3. 实现与测试规模

相对 `4875cc1...375f433` 的 production/test 净变化：

| 类别 | 文件数 | 新增 | 删除 | 净增 |
|---|---:|---:|---:|---:|
| Production | 4 | 857 | 90 | 767 |
| Tests | 3 | 1864 | 0 | 1864 |
| 合计 | 7 | 2721 | 90 | 2631 |

Production 分布：

- `source_discovery.py`：+540，新建深 Module，集中 closed manifest、FD identity、预算、
  全量 preflight 与原子注册；
- `composition.py`：+133/−34，增加 package root identity、恢复/monitor 顺序、ready 可见性
  与可重试 cleanup ownership；
- `store.py`：+102/−34，在既有单-source Interface 下增加共享 registration transaction，
  不向上层暴露 Store 私有写法；
- `service.py`：+82/−22，复用同一 source budget，并把 CAS read/hash/write seam 全部收紧。

测试/production 新增行比约为 `2.18`。这个比例显著高于 02E 的 `1.11`，原因不是产品
功能膨胀，而是 02F 同时跨越不可信 YAML、Linux 文件身份、SQLite 原子性、进程 lease、
启动失败清理和外部编辑竞态六类高风险边界。后续切片不能把这个比例当作常态。

## 4. 最终门禁

精确候选 `375f433`：

```text
02F + finding + Phase 01 lifecycle 目标： 78 passed
独立 reviewer 精确目标：                 75 passed
tests/workflow：                          839 passed
完整 tests/：                             1684 passed, 3 skipped
修改文件 Ruff E/F/I：                     passed
Ruff format --check：                     passed
git diff --check：                        passed
```

3 个 skip 与 18 个 warning 均来自既有 optional dependency、pytest collection、FastAPI
lifespan/TestClient 提示；本轮没有新增 warning。最终 reviewer 报告是候选之后的 doc-only
提交，不改变被测试和评审的 production/test SHA。

## 5. 问题趋势

| Round | 初次 review blocking | 初次 non-blocking | 最终 blocking | 最终 non-blocking |
|---|---:|---:|---:|---:|
| 02B1 | 7 | 1 | 0 | 1 |
| 02B2 | 2 | 1 | 0 | 1 |
| 02B3 | 4 | 1 | 0 | 0 |
| 02B completion | 2 | 1 | 0 | 0 |
| 02C | 3 | 3 | 0 | 2 |
| 02D | 6 | 2 | 0 | 1 |
| 02E | 1 | 1 | 0 | 1 |
| 02F | 3 | 1 | 0 | 0 |

02F 的初次 blocking 从 02E 的 1 组回升到 3 组，但没有新增产品 grill：三组分别是
文件/解析信任边界、既有 registration 反向 identity、启动/清理 ownership。首次修复后
B02、B03 与 NB01 关闭，问题收敛到一个 B01；后续两次复核把 B01 从
`loader→registration + runtime read` 收敛到 `CAS read/hash`，最后归零。

因此整体不是不断发现新的产品问题，而是在一个高风险基础设施边界上逐层补齐同一不变量：

```text
3 组根因 -> 1 组根因（2 个 seam） -> 1 组根因（1 个 seam） -> 0
```

发现数量在中途增加，是 reviewer 把“8 MiB”从 loader 追到真实 GET，再追到 CAS
竞态窗口的深度检查；它没有扩大用户交互、DTO 或持久模型。最终 0B/0NB，说明 02F 已
完成收敛，不需要把安全探针继续无限外推。

## 6. 遗留问题与重要性

### 02F 自身

**无遗留 blocking 或 non-blocking。** source lifecycle、identity、transaction、budget 与
composition cleanup 均已有独立行为证据。

### 02G production composition

重要性：**高；是 OS server 暴露真实 persistent Authoring 的计划内下一切片。**

02F 只注册和恢复 source；真实 server 尚未把 02D engine、Catalog provider、02E pure
Interface 与 persistent Draft/Apply authority 组合到同一进程根。完成 02G 前，FE 可以用
focused Interface 做 contract 测试，但不能宣称真实 OS 端到端已完成。

### 02E S-NB01：Candidate 验证重复

重要性：**中等；必须在 02G 内关闭。**

pure HTTP 已使用 `candidate_validation.py`，persistent Service 仍有近似验证。02G 组合时应
共用 closed Candidate semantic，Service 只保留 hydration、事务与稳定错误码，避免形成
第三套 validator。

## 7. 策略调整

1. 02G 的原始测试必须同时覆盖 focused router、production app composition 和 persistent
   Service 三个真实 consumer，首轮就检查共同 Candidate validator，避免评审后再逐 seam
   追同一不变量。
2. 对“共享预算/identity/token”类合同，测试矩阵按完整数据路径列出所有 read、hash、write、
   publish consumer；不再只以入口或最终错误码作为充分证明。
3. 维持深 Module：02G composition 只选择并连接 Authority、compiler、Catalog 和 router，
   不把 source discovery、Candidate semantic 或 Store transaction 逻辑搬进进程根。
4. 02G 合并后立即启动独立 Frontend Round；FE 使用单编辑权模式，明确显示“代码可编辑/
   画布投影”或“画布可编辑/代码投影”，不实现双向并发编辑。
5. FE-OS 联调必须连接真实 OS production app，不用 route mock 宣称 E2E；Frontend 单开
   分支，Backend 保持只读。

## 8. Frontend、Backend 与合并判断

- Frontend：**02F 未修改**；
- Backend：**未修改**；
- FE-OS：source lifecycle 已就绪，但真实 production composition 留给 02G；
- 最终评审：Standards 0B/0NB，Spec 0B/0NB；
- 合并：允许非 squash 本地合入 `integration/workflow-task-runtime`，不 push；
- 下一步：从最新 integration 新建 02G 工程 Round；02G 合并后自动进入独立 FE Round。
