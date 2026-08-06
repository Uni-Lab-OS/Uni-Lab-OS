# F007 Edge/runtime safety 趋势报告

日期：2026-08-03

状态：**OS、FE 与真实 Electron 联调候选已通过完整门禁和 exact-SHA review，
并已在隔离发布分支完成 non-squash integration merge 与合并态复验；本报告随
integration 发布。尚未更新 Core gitlink、私有 OS `dev` 或进入 Accepted。**

## 1. 范围与基线

| 仓库 | integration 基线 | exact-reviewed 候选 | non-squash integration merge |
|---|---|---|---|
| OS | `integration/workflow-task-runtime@25be15efb272667bc4398241cf119299ba02fd2f` | `migration/f007-edge-runtime-safety@77c05f8f37d56c7179b1c7024337a43bb14d749f` | `336a075f16c92410c3a70eca0671c1b11a397f9b` |
| FE | `integration/fe-os-migration@a453b30868f530456c3fa7a06d878567be863d6e` | `feat/ui-optimization-szlab-material-display@4a5a941ff3bc15636b2cba95b6b3da98743e25f9` | `8907e608ba58b1b0e4fb53d2e12d4514d422a966` |
| SZLab fixture | `feat/szlab-material-display` | 未修改源码 | 已跟踪 `deployment/graphs/szlab-local-debug.json` |

本轮关闭五项：Edge 启动后 Action 目录上报、0 Action 设备点击崩溃、Action/Workflow
终止后的设备锁结算、无领域设备包启动 Edge、日志结构化渲染。没有新增公共路由、
前端 HostNode/lock authority、第二套设备 UI 或真实物理设备门禁。

## 2. 实现结果

### 2.1 OS

- 无 Package Workspace 时仍建立 `os-local` Graph Authority；跳过依赖
  PackageCatalog projection 的 ResourceTreeSet inventory bootstrap；
- 普通 Workflow cancel 通过正式 dispatcher port 下发 Host cancel；Job 在物理终态前
  保持 `cancel_requested`，终态持久化完成后才允许 backend 释放设备锁；
- completion callback 的完整“读快照→写终态”和 cancel sweep 的“复读→写 unknown”
  由同一 settlement `RLock` 串行；确定性交错回归覆盖 completion 已读旧快照、sweep
  同时收到 cancel false 的窗口；
- cancel 或 dispatch transport outcome 无法确认时进入 `execution_unknown` 并保留围栏；
- framework-owned Published Workflow gate 同时验证 `schema.default` 与
  `goal_default` 的存在性和值，漂移投影 fail closed。

### 2.2 FE 与原 UI 复用

- 未重写设备页、设备列表、Action 卡、参数表单、ConnectionBar 或本地启动器；0 Action
  修复仅纠正空选择与 run state 的 null 判定，空设备仍可选择和刷新；
- 领域项目目录改为可选。有目录时 readiness 必须看到非 `host_node` Action；无目录时
  使用 OS 内置 config，只等待合法 `device-catalog/v1`，不依赖设备包或 Workflow
  Template Catalog；
- health、device、Action catalog 请求支持 AbortSignal；停止 Edge 前先断开并 abort
  renderer 请求，再关闭本地端口；
- Electron 托管 Edge idle 时不预探测关闭端口，但保留外部/自定义 backend 的显式连接；
  renderer reload 可从 main 的 ready snapshot 恢复连接与设备目录；
- 原日志抽屉改为时间、级别、来源、正文四列结构化行；完整清除 ESC、7/8-bit
  CSI/OSC/DCS/SOS/PM/APC 与其余 C1 控制字符，不执行日志内容。

## 3. 测试 provenance 与 review

唯一独立 test-author 为 `/root/f007_red_tests`；tests-only RED 以非 squash 提交
`daac505f707a7ab0dc1fdd4327754943953f757e` 保留在迁移分支，未删除、弱化、skip
或 xfail。

唯一独立 reviewer 为 `/root/f007_exact_review`。首轮对 OS `a49c8e98`、FE
`0b035ba1` 提出 settlement 交错、E2E 错误账本、Electron 外部连接/reload、ANSI
single ESC/C1 和 default 一致性等 finding。修复后对最终组合：

- OS `77c05f8f37d56c7179b1c7024337a43bb14d749f`；
- FE `4a5a941ff3bc15636b2cba95b6b3da98743e25f9`；

确认 Standards `0B/0NB`、Spec `0B/0NB`，结论 `APPROVED`。

## 4. 候选与 integration 最终门禁

### OS

| 门禁 | 结果 |
|---|---:|
| cancellation/terminal/unknown 定向 | `4 passed, 18 deselected` |
| Published Workflow contract 定向 | `34 passed` |
| 候选完整 `python -m pytest -q tests/` | `2569 passed, 4 skipped, 68 warnings` |
| integration merge 完整 `python -m pytest -q tests/` | `2569 passed, 4 skipped, 69 warnings` |
| changed files Ruff format | passed |
| changed files Ruff fatal rules `E9,F63,F7,F82` | passed |
| `git diff 25be15ef..77c05f8f --check` | passed |

### FE

| 门禁 | 结果 |
|---|---:|
| 候选完整 `pnpm test` | `417 passed` |
| integration merge 完整 `pnpm test` | `419 passed` |
| `pnpm typecheck` | passed |
| `pnpm build:web` | passed |
| `pnpm build:desktop` | passed |
| integration merge 真实 Electron：领域包 + 无领域包 | `2 passed`，42.8 秒 |
| integration merge 0 Action 设备点击/刷新 | `1 passed`，17.0 秒 |

真实 Electron 用例不清空、不按文本过滤 console/page errors；覆盖 ready 后 renderer
reload、两次启停、9 台领域设备、OS-only HostNode、health、Action 目录和格式化日志。
最终本地产出 19 张截图。个人本地截图路径只用于本轮人工判断，不作为团队 immutable
evidence；团队证据需在后续 Core pin/evidence gate 中以 commit 或 CI URL 固定。

## 5. Integration 发布边界与停止线

- OS merge commit 的两个父提交为 integration 基线 `25be15ef` 与候选文档 HEAD
  `c3419752`；FE merge commit 的两个父提交为 integration 基线 `a453b308` 与候选
  `4a5a941f`，两个 merge 均无冲突且未 squash；
- 本报告只批准发布 OS `integration/workflow-task-runtime` 与 FE
  `integration/fe-os-migration`，不是私有 OS `dev` release 或 Core submodule pin；
- 未修改 public `deepmodeling/Uni-Lab-OS:dev`；
- 不将 F007 局部 safety 修复解释为 R2 sole-coordinator、full D1 或 Accepted；
- Core gitlink、私有 OS `dev` 与 Feishu acceptance 仍需后续独立判断。
