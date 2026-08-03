# F006R1 本地领域 Edge 启动修复趋势报告

日期：2026-08-03

状态：**OS、FE 与真实 Electron 联调候选均已完成本地门禁；OS exact-SHA review
为 Standards `0B/0NB`、Spec `0B/0NB`。当前未 merge、未 push、未更新 Core
submodule pin，不等于团队发布或 Accepted。**

## 1. 基线与边界

本轮严格从用户指定的两个 integration 最新远端基线新开分支：

| 仓库 | 基线 | 本轮分支 | 受测候选 |
|---|---|---|---|
| OS | `integration/workflow-task-runtime@5095948e49def24ac30d3db21a3fc758b4314efc` | `migration/f006r1-local-debugger-startup` | `c550da665906abaf9568365aaa1d990f4d092aa8` |
| FE | `integration/fe-os-migration@113e5519b71e2d7dd4f86de2bd36e7afd439f735` | `fix/local-debugger-integration-startup` | `c1eef9fc3e7e4596f978c0e45ff8106eed1c2f02` |
| SZLab fixture | `main` 的已跟踪 `deployment/graphs/szlab-local-debug.json` | 未修改源码 | 9 个 live devices |

本轮只修复领域 workspace Graph 进入 OS composition 后的 definition/resource 激活，
以及 Electron local runtime 对真实 Edge endpoint/readiness 的消费。没有新增公共 Run、
Bridge、Profile、第二个 DeviceActionManager 或前端 runtime authority；没有使用未跟踪的
`copy.json`，也不等待真实 PLC 或物理设备。

## 2. OS 实现

### 2.1 Graph identity

- Package Material projection 以 canonical FQID 为稳定定义身份，同时仅为全局唯一的
  local id 发布窄 legacy alias；fingerprint 只包含 canonical mapping；
- `resolve_definition_identity()` 成为 canonical / unique short alias / ambiguous 的唯一
  判定 seam；Registry 与 Material projection 共用，不各自实现一套匹配规则；
- exact FQID 优先，唯一 short id 可解析；两个 package 共享 short id 时抛出
  `DefinitionIdentityAmbiguous`，不得回退 generic resource；
- Graph Material class 与 Site `content_type` 使用相同 fail-closed 规则。

### 2.2 Package resource activation

- `DeviceClassCreator` 先在 `lab_registry.resource_type_registry` 解析 Graph child；
- Package entry 使用 Catalog 提供的 `class.module` 导入真实 factory，仅把 factory
  signature 接受的 Graph config 传入，并由 Graph name 覆盖实例 name；
- factory 返回的完整 Resource/sites/model/category 被加入原
  `DeviceNodeResourceTracker`，UUID/extra 继续由既有 tracker 设置；
- 只有确认不存在 Package owner 的旧 resource 才走原 generic converter；歧义不捕获；
- 没有给 `warehouse` 增加全局 `TYPE_MAP`，避免把领域资源类型硬编码进 OS。

## 3. FE 实现与原 UI 复用

FE 候选由三个可审阅提交组成：

1. `b3e9902c0f74cc1f6cc912b334593011caa26da8`：把 Electron launcher 文案改为
   “领域侧（以 sz_lab 为例）”，删除旧 Bridge 展示，修复停止竞态，保留原
   `ConnectionBar`、设备页与日志抽屉；
2. `ff2f026153eb24475a05ad0d8c25507e4cfc0182`：local runtime 只有在
   `GET /api/v1/health`、`GET /api/v1/workflow-node-templates` 和
   `GET /api/v1/devices` 均有效时才宣告 ready；
3. `c1eef9fc3e7e4596f978c0e45ff8106eed1c2f02`：把 local-python 默认 HTTP/WS
   endpoint 与 Electron launcher 统一到 `127.0.0.1:18003`，避免 ready 后切换
   backend 导致 UI remount；E2E 在 ready 窗口把 console error、page error 和
   HTTP 4xx/5xx 作为失败。

产品 UI 没有重写设备列表、设备头、Action 卡、参数表单或运行结果面板。设备数据仍由
原 `LaboratoryService` 和原设备页面消费 `GET /api/v1/devices`；本轮只更换 launcher
装配、endpoint 与 readiness 条件。

## 4. 测试 provenance 与 review

本轮唯一独立 test-author 为 `/root/f006r1_red_tests`。原 tests-only RED：

- `79c1661182a090317bf39a6a909a506c91778ba9`：`3 failed, 1 passed`；
- `be6722bdcebd315c572d0e982dd6afa99079b69e`：只修正 ROS 合法 fixture id，
  RED 仍稳定落在 package resource factory 缺口；
- 迁移分支以 `35a63128`、`611b6fb0` 保留 cherry-pick provenance，没有 squash、
  skip 或 xfail。

唯一独立 reviewer 为 `/root/f006r1_exact_review`，轮换视角为 module-design，同时完成
Standards/Spec 双轴复核。首个精确候选
`1af65103b72cf072988425d5ecb6aa7402b4fbea` 为 Standards `0B/2NB`、Spec
`1B/1NB`：runtime 歧义 short alias 会回退 generic resource。修复后精确候选
`c550da665906abaf9568365aaa1d990f4d092aa8` 由同一 reviewer 确认：

- Standards：`0B/0NB`；
- Spec：`0B/0NB`；
- 结论：`ACCEPT`。

## 5. 门禁结果

### OS

| 门禁 | 结果 |
|---|---:|
| F006 identity/factory acceptance | `6 passed` |
| Package/Resource focused | `78 passed, 1 warning` |
| 完整 `pytest -q tests` | `2537 passed, 4 skipped, 68 warnings` |
| changed critical Ruff、imports、format | passed |
| changed production/test `compileall` | passed |
| `git diff 5095948e..c550da66 --check` | passed |
| `python -m unilabos --check_mode --skip_env_check` | exit `0` |

直接运行无路径约束的 `pytest -q` 会误收集 `unilabos/` 内 Modbus/Camera 硬件示例，
分别触发真实 `192.168.3.2:502` 连接和非包导入；正式全仓门使用受维护的 `tests/`
目录。`check_mode` 在当前 Linux 环境仍报告既有 `pywinauto`/DISPLAY 可选依赖告警，
并会机械重写 registry YAML；命令 exit 0，生成 diff 已回退且候选工作树保持干净。

### FE

| 门禁 | 结果 |
|---|---:|
| Services typecheck / tests | passed / `102 passed` |
| Kernel Web typecheck / tests | passed / `38 passed` |
| Desktop typecheck / tests | passed / `18 passed` |
| Desktop production build | passed |
| icons / installer checks | passed |

## 6. 真实 Electron + OS + SZLab E2E

最终 E2E 使用 FE `c1eef9f`、OS `c550da66` 和已跟踪
`szlab-local-debug.json`，完成：

1. Electron 启动，local-python 默认 endpoint 为 `http://127.0.0.1:18003`；
2. 通过前端按钮启动真实 OS/HostNode；
3. health、Workflow Template Catalog、live device catalog 均 ready；
4. 原设备 UI 显示 9 台设备；
5. 停止后端口释放；
6. 同一 UI 再次启动并再次显示 9 台设备；
7. 再次停止；ready 窗口 console/page error 与 HTTP 4xx/5xx 均为 0。

结果：`1 passed`，28.1 秒；`GET /api/v1/health` 返回
`{"status":"ok","scheduler":"ready"}`，`GET /api/v1/devices` 返回
`device-catalog/v1`、9 items；产出 10 张原始截图和两份 JSON 响应。

SZLab PLC driver 在 `auto_connect=false` 时仍会轮询 PLC 传感器并输出
`NoneType.send_request` 日志；这不影响 HostNode、HTTP ready、设备目录或两次启停，
也不是本轮 OS/FE launcher 的 fatal 条件。该领域 driver 噪声需要在 SZLab owning repo
单独处理，不在本轮越界修改。

## 7. 发布停止线

当前候选只允许进入用户判断：

- 尚未把 OS 分支 non-squash merge 到 `integration/workflow-task-runtime`；
- 尚未把 FE 分支 non-squash merge 到 `integration/fe-os-migration`；
- 尚未 push OS/FE 候选；
- 尚未提交 Core submodule pin 或团队 immutable evidence；
- 因此 Core #147/#151 只能记录本地 testing progress，不得标记 Accepted。

