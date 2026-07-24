# W1–W2 Quick Debug Alpha 软件门禁报告

日期：2026-07-22  
范围：单站 local OS + SQLite journal + 共享 Cloud 前端；不包含 Go、PG、Layer B、排程算法、真实 PLC/设备 smoke 或生产发布。

## 结论口径

当前实现具备可重复的软件 Quick Debug Alpha：同一 OS Runtime 编译、锁定并执行通用 Workflow；共享 Kernel Workbench 只切 RuntimeClient 通信配置；真实 pTLC Operation 作为普通设备包与 Profile 接入。不得据此声明“pTLC 底座生产替换完成”“真机可发布”或“资源约束甘特已实现”。

2026-07-22 Code Panel 纠偏已完成：真实 Operation YAML 导入/执行测试仍是 migration baseline；迁移后作者源是 `ptlc_station/(workflows|scripts)/*.py`，由同包 decorator Registry 和 Profile `python_ast_v1` importer 编译到 OS Canonical/DAG。YAML 只保留 path/blob 对照，不进入 Code Panel。总体仍保持 VERIFYING，因为文件原子保存/SQLite 索引、panel-runtime 完整抽取和真机 smoke 尚未关闭。

## 权威验收源

- `02_develop/develop_execute.yaml`：权威 Git blob `0be22c…`，14 个 operation。
- `02_develop/develop_prepare.yaml`：权威 Git blob `4c8b1f…`，12 个 operation，含两个 `finally` cleanup。
- 嵌套脚本 `develop_standby.yaml`、`rail_move_safe.yaml` 同样按记录的 Git blob 校验并递归解析。
- `UI-Upper/recipes/*.yaml` 只作旧 Recipe compatibility coverage，不替代上述真实 Operation 验收。

## 2026-07-22 新鲜门禁

| 仓库 / Harness | 命令摘要 | 结果 |
|---|---|---|
| Uni-Lab-OS | `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 … pytest -q tests/app tests/registry tests/runtime tests/workflow tests/scheduler tests/devices/ptlc` | 353 passed |
| Uni-Lab-Templates | `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 … pytest -q` | 18 passed |
| pTLC legacy harness | `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 … pytest UI-Upper/tests -q` | 73 passed |
| Cloud Kernel unit | 六个 `src/kernel` unit files | 28 passed |
| Cloud static | `yarn typecheck`；`yarn lint` | 两项 exit 0 |
| Cloud mock E2E | `yarn test:e2e:kernel:mock` | 16 passed；未豁免 console/page/request error 均为 0 |
| Cloud real Runtime E2E | `yarn test:e2e:kernel:real` | 3 passed |
| pTLC package artifact | `pip wheel --no-deps packages/ptlc_station` | wheel 构建成功 |
| Python Code Panel migration | Templates Python workflow → decorator Registry → Profile AST importer → Canonical/TaskDag → Runtime sourceArtifact → browser screenshot | 通过；YAML 仅作 migration provenance |

测试作者是独立 subagent；生产实现完成后，主代理也独立复跑了上述核心门禁。最终关闭前仍要求另一名独立 code-review subagent 给出无 Critical/Important blocker 的结论，并在其反馈后再次全量复跑。

## 端到端证据

截图目录：`Uni-Lab-Cloud/docs/features/F004-quick-debug-alpha/screenshots/`

- `whole-workflow-runtime.png`：同一 Runtime 会话执行整图。
- `action-debug-runtime.png`：共享页面单 action 调试。
- `real-develop-execute-dag.png`：真实 14-operation 工作流导入后的 DAG。
- `real-develop-prepare-cleanup.png`：真实失败路径仍运行两个 `finally` cleanup。
- `python-code-view-runtime.png`：展示设备包 `workflows/develop_execute.py` 的 Python URI、内容 hash 与规范化源码；不是原 YAML。

五张截图已以原始分辨率人工检查；`python-code-view-runtime.png` 同时展示 Canonical 参数表单。Mock 参数表单用例还会单独截屏校验，但 real golden 原子发布只保留上述五张权威证据。

## 已验证边界

- OS core、compiler、scheduler、runtime、bridge 不含 pTLC 专用 API；pTLC 是可独立构建的装饰器设备包 + 声明式 Profile。
- Physical action 来自 `DeviceTemplate/@action` 注册合同；控制节点来自 OS；Human gate 复用 `host_node.manual_confirm`。
- `if` 的未知结果在节点完成后由 OS 求值；未选分支零 dispatch；join 只等待 active branch；结果通过 `ConditionalBinding/Phi` 汇合。
- 本轮只支持可在编译期有限展开的循环；range 与总节点数有硬上限，malformed/overflow source 稳定返回 400 且零派发。不支持运行时不定次 `while/repeat`，也未实现 step/step-over/breakpoint/from-node debugger。
- Estimated timeline 明确 `is_resource_constrained=false`；Observed 只投影真实 RunEvent。

## 未关闭项与非门禁异常

- 真机单样品 smoke 未执行；属于设备验收，不得由 fake PLC/E2E 代替。
- 工作流文件原子保存 → compile/hash → SQLite index/draft 的 authoring persistence 切片尚未实现；当前导入与执行以本地权威文件为源。
- 全量 Cloud production build 曾在默认 4 GB heap 下 OOM；本报告只声明 Kernel unit/typecheck/lint/E2E。
- 仓库根裸 `pytest -q` 会误收集生产目录内连接真实 Modbus/相机的脚本。G2 使用 `pytest.ini` testpaths/显式测试目录；这些硬件脚本应另建 hardware harness，不能混入 hermetic unit gate。
