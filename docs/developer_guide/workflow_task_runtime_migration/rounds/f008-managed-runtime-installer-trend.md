# F008：桌面内置 Runtime 与一键离线安装趋势报告

日期：2026-08-04

实现分支：`feat/managed-runtime-supervisor`

integration 基线：`25be15ef3c21cfbd3c7c054f9b651f40f31e2ac4`

最终 production/test 候选：`aca69a249175c4ba18fd98218845b3f14e69d07d`

配套 FE 候选：`e6996c3bb697f9933c8f8c8b6147e42c47a970a5`

状态：**OS 内置 Runtime、受管 Supervisor、PLC 模拟器独立进程与四平台
Constructor 流程已实现；Linux 一键产物已完成冷安装和 PLC 真实进程验收，完整测试、
四平台配置门禁和同一独立 reviewer 精确 SHA 复审通过。Windows 与两种 macOS 发布物由
原生 runner matrix 生成，不能用 Linux 的跨平台 render 冒充原生安装验收。**

## 1. 交付与停止线

本轮让桌面安装包携带私有 Uni-Lab-OS Runtime。最终用户不需要 Git、Conda 或 OS
源码；Electron 首次启动 Edge 时负责校验、安装并通过 loopback Supervisor 启停 Runtime。

Supervisor 提供 bearer-token 保护的 loopback HTTP 生命周期接口，并保持以下边界：

- Edge worker 与 PLC simulator 是两个独立子进程，可分别或同时启动；
- 启停、清理和状态持久化在同一 Supervisor authority 中串行，防止生命周期竞态；
- Windows 清理必须证明目标进程树已经退出，不能把 `taskkill` 失败伪装为成功；
- 状态文件采用 fail-closed 语义，损坏或替换失败不会丢失运行权威；
- PLC simulator 支持源码入口和已打包可执行文件，均由 Electron 按钮启动；
- 设备包签名无效或未签名时由桌面端明确警告并记录，是否继续由用户决定。

OS 自身组网能力不在本轮实现范围；本轮只保留可由后续内部实现接入的 Runtime 边界。

## 2. 独立 RED、实现与审查 provenance

| 阶段 | 角色 | 提交 | 结果 |
|---|---|---|---|
| 独立 tests-only RED | `/root/managed_runtime_test_author` | 原始 `297398afcc9f34dd062e72ff6d4656ffdf7ab6d5`；集成 `3afa4b07ee36301e75e84bce2eee687af9632963` | `1 failed`，原因是 `unilabos.managed_runtime` 尚不存在；patch-id `6ef474...` 保持一致 |
| 首个 production 候选 | 主代理 | `2e6b3bf22f33acdf60a52f37c7f1d6dca23e1024` | 实现 Supervisor、Constructor 和安装入口 |
| fail-closed 修复 | 主代理 | `85776129`、`19b65376` | 修复状态替换与 Windows 进程树清理失败路径 |
| 离线求解修复 | 主代理 | `3c545f4d`、`7707de30` | Unix 改用 micromamba；固定 Uni-Lab 兼容 PyLabRobot fork |
| Windows bootstrap 修复 | 主代理 | `aca69a249175c4ba18fd98218845b3f14e69d07d` | Windows 固定 conda-standalone 26.3.2.post1；PyLabRobot 关键签名进入 recipe test |
| 独立 reviewer | `/root/managed_runtime_reviewer` | `7707de30` | 找到 Windows 使用 micromamba 的 blocking，结论 NOT APPROVED |
| 同一 reviewer 最终复核 | `/root/managed_runtime_reviewer` | `aca69a249175c4ba18fd98218845b3f14e69d07d` | APPROVED；0 blocking；确认四个 subdir 执行器可解、Windows native path 与签名 recipe test |

独立测试提交先于 production；没有删除、skip、xfail 或弱化其行为断言。本轮只使用同一名
reviewer，最终精确 SHA 的复核结论在下方门禁记录中冻结。

## 3. Constructor 与本地 channel 结论

支持的用户输入与 Conda subdir 映射：

| 输入 | subdir | 原生 runner | bootstrapper |
|---|---|---|---|
| `linux` / `linux-64` | `linux-64` | Linux x86_64 | micromamba 2.8.1 |
| `osx` / `osx-64` | `osx-64` | macOS Intel | micromamba 2.8.1 |
| `osx-arm64` | `osx-arm64` | macOS Apple Silicon | micromamba 2.8.1 |
| `win-64` | `win-64` | Windows x86_64 | conda-standalone 26.3.2.post1 |

统一打包脚本先构建 `rfc8785`、`msgcenterpy`、兼容 PyLabRobot fork 和当前 OS 源码包，
并把 platform 与 `noarch` 放在同一个 channel 根目录。Constructor 的 channel 首项固定为
该本地根，因此同一 checkout 是安装包唯一 OS 来源。

Constructor 3.16.1 明确拒绝用 micromamba 生成 Windows installer。另一方面，默认的
conda-standalone 26.5.2 会把 `cached-property/cached_property`、
`typing-extensions/typing_extensions` 和 `email-validator/email_validator` 过渡包判断为
重复 explicit package。最终策略是 Unix 使用 micromamba，Windows 固定到已用同一完整
payload 冷安装验证过的 conda-standalone 26.3.2.post1。

PyLabRobot 固定到 Uni-Lab 实际开发依赖 commit
`1317a3eec20cb28757283b1de0edcd2e1b2f252e`，source SHA-256 为
`4dac1abc3fd4cf3e84141dbd39e18f486749328ff044156fab86011a54bf5649`，Conda build number
为 `1`。recipe 自动断言 `VolumeTracker.add_liquid` 同时包含 `liquid_or_volume` 和
`volume` 参数，防止未来只通过 import 而破坏设备 API。

## 4. 最终门禁

精确候选 `aca69a249175c4ba18fd98218845b3f14e69d07d`：

```text
正式 tests/：                         2577 passed, 5 skipped, 68 warnings
Supervisor focused：                  12 passed, 1 skipped
四平台 Constructor render/schema：   passed
修改 Python 文件 Ruff check：         passed
修改 Python 文件 Ruff format：        passed
PyLabRobot recipe build + signature： passed
git diff --check：                    passed
独立 reviewer：                       APPROVED，0 blocking
```

仓库根直接运行无范围的 `pytest` 会额外收集两个历史硬件示例目录，并分别命中缺少
`Coil.data_type` 与 `cameraUSB` 相对导入错误；正式门禁入口是仓库既有的 `pytest tests`。
上述两个基线示例错误不在本轮授权范围，也没有被隐藏为 skip。

本地还用 conda-standalone 26.3.2.post1 对最终 channel 生成完整代理安装器并冷安装，确认
安装后的 Runtime 包含 `conda 26.7.0`，且 `unilabos`、`rclpy`、`rfc8785`、
`msgcenterpy`、PyLabRobot 兼容签名、`unilab --help` 与
`unilab-supervisor --help` 均通过。

## 5. 一键打包与 PLC 模拟验收

Uni-Lab-Core 的 `pnpm package:unified` 已在 Linux 原生环境完成完整链路：本地 Conda
channel → Runtime Constructor → Electron AppImage。最终 Runtime 安装器约 603 MB，
AppImage 约 664 MiB；安装器包清单选择
`pylabrobot-0.2.1-pyh4616a5c_1.conda`。

冷安装后的私有 Runtime 由 Supervisor 启动真实进程验收：

- PLC-Sim 源码作为独立 simulator 进程启动，health 返回 `ok=true`；
- Edge 使用正式 `ros` backend 启动，scheduler 返回 `ready`；
- 设备列表包含 `host_node` 与 `plc_reference_device`；
- `plc_reference_device` online，暴露 `read_reference` action；
- worker 与 simulator 同时运行；停止 worker 后 simulator 继续运行；再停止 simulator 后
  两者均回到 idle；Supervisor 最后干净退出。

这证明领域内设备包不仅可以被安装，还能通过桌面受管 Runtime 启动并被 OS 的设备目录
发现。PLC simulator 仍是单独进程和单独启动动作，没有被错误地合入 Runtime 进程。

## 6. 发布边界

四个平台都使用原生 GitHub runner 构建、静默安装和 smoke test。当前 Linux 主机只能对
四平台做 render/schema，对 Linux 做实际构建、冷安装和 PLC E2E；Windows、macOS Intel、
macOS Apple Silicon 的安装结果必须以各自 CI job 为准。开发模式允许未签名产物并明确
提示，production 模式要求各平台签名材料存在，否则 workflow fail closed。

本报告是 ledger-only 变更；经过完整行为门禁和精确审查的 production/test 候选仍是
`aca69a249175c4ba18fd98218845b3f14e69d07d`。
