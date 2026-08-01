# Round O1：ROS2 trace observability 趋势与策略报告

日期：2026-08-01

实现分支：`migration/o1-ros2-trace-observability`

integration 基线：`856cc0f`

最终 production/test 候选：`5505e4e8c64f07c3fe33848db8eff80154e27f2e`

状态：**Phoenix OSS、Electron OTLP gateway 与
Scheduler → Host → ROS2 Action → 设备 execute/driver 的 trace 链路已打通；两轮 blocking
finding 已全部关闭，最终独立审查 Standards/Spec 均为 0 blocking、0 non-blocking。**

## 1. 本轮交付

- Uni-Lab-OS 管理只监听 loopback 的 Phoenix OSS，使用独立 SQLite；Electron 只通过
  OS 的 OTLP ingest、trace list/detail Interface 上报和查询；
- Scheduler dispatch 创建 W3C carrier，Host 为 goal send/response/result/cancel 建立 span；
- JSON Action 通过受控系统参数传播 carrier，原生 Action 通过有确认、有限时、有限容量和
  TTL 的 ROS service side-channel 传播；
- 设备 ActionServer 创建 execute span，sync ThreadPool 与 async driver 都继承父 context，
  驱动继续调用另一设备时 native/JSON 链路继续传播；
- `ros_goal_uuid` 只负责 side-channel lookup，`WorkflowNodeJob/WorkflowTask` UUID 保持领域
  身份，嵌套调用不会再把随机 ROS goal 标成 WorkflowNodeJob；
- 只有有效 carrier 才使用 Host 专用登记线程池。Action server 等待有 5 秒上限，失败走
  普通 Job failure 回报；shutdown/reset 取消排队并阻止停机后发送物理 goal；
- span 属性只接受低基数标量并过滤敏感 key；异常只导出 `error.type` 与 ERROR 状态，
  不导出 message/stacktrace/动作参数；
- Phoenix 17.5 禁用 sandbox 后仍无条件预取 CPython WASM 的启动缺陷，以本地非空 sentinel
  规避；真实实例启动从偶发 60 秒超时稳定到约 6.4 秒且不需要外网。

## 2. RED、实现与审查 provenance

| 阶段 | 角色 | 提交 | 结果 |
|---|---|---|---|
| Phoenix 基线接入 | 主代理 | 原分支 `5b80450`；本轮等价提交 `cc6ace4` | Phoenix process/SQLite、OS API、Electron OTLP/query seam |
| 独立 tests-only RED | `o1_ros2_trace_tests` | 原始 `de8f0d49381ccb9d081dba0ff03255326a6ad1eb`；cherry-pick `0ffcd28` | 5 failed、1 passed；失败均为缺少 ROS2 trace 传播的预期 RED |
| 首个 production 候选 | 主代理 | `763c9dc99c8ce27d1729d6ac8be003c1428ed481` | 独立测试转绿；首轮完整门禁 2034 passed、4 skipped |
| 首轮精确 SHA 审查 | `o1_ros2_trace_review` | `763c9dc99c8ce27d1729d6ac8be003c1428ed481` | Standards 0B/2NB；Spec 5B/0NB |
| 首轮 finding 修复 | 主代理 | `0caf2d0a9223a9e176f676de406f5acd79a2a614` | focused 31 passed、1 skipped；完整门禁 2045 passed、4 skipped |
| 同一 reviewer 二次复核 | `o1_ros2_trace_review` | `0caf2d0a9223a9e176f676de406f5acd79a2a614` | Standards 0B/2NB；Spec 2B/1NB |
| 最终 finding 修复 | 主代理 | `5505e4e8c64f07c3fe33848db8eff80154e27f2e` | focused 35 passed、1 skipped；完整门禁 2049 passed、4 skipped |
| 同一 reviewer 最终复核 | `o1_ros2_trace_review` | `5505e4e8c64f07c3fe33848db8eff80154e27f2e` | Standards 0B/0NB；Spec 0B/0NB；PASS |

独立测试提交没有被 squash；没有删除、skip、xfail 或弱化独立测试。实现与 finding 回归
均在独立 test commit 之后追加，保留了测试作者 provenance。

## 3. Finding 收敛

首轮五个 blocking：

1. Scheduler 的 hex UUID 与设备端标准 UUID key 不匹配：map key 改为 canonical UUID，
   payload 仍保留业务 ID；
2. OpenTelemetry 默认 exception event 泄露 message/stacktrace：关闭默认异常采集，只设置
   安全 `error.type` 与 ERROR；
3. 嵌套 native sync/async Action 未传播：新增同一 ROS goal UUID 的登记与发送，并覆盖
   sync/async；
4. cancel span 无父 context、过早结束且无结果：增加 cancel request/response 父子 span，
   覆盖 accepted/rejected/error；
5. Host side-channel 阻塞 Scheduler 唯一 worker：移到专用有限线程池，登记失败仍发送并在
   后台发送失败时回报 Job failure。

二次复核剩余两个 blocking 与一个 non-blocking：

1. 随机 nested goal 被误标为 WorkflowNodeJob：wire contract 拆分 `ros_goal_uuid` 与真实
   Workflow 身份，并通过 Python context 继承到 sync/async nested call；目标 execute 和
   driver span 回归同时断言三类 UUID；
2. 后台 ActionServer wait 无界、no-op tracing 改变时序、executor 未停机：仅有效 carrier
   defer，等待改为 5 秒，shutdown/reset cancel pending，登记后与 wait 后再次检查停机；
3. callback error span 可能保持 UNSET：含安全 `error.type` 的 span 显式设为 ERROR。

## 4. 最终门禁

精确候选 `5505e4e8c64f07c3fe33848db8eff80154e27f2e`：

```text
独立 RED：                    5 failed, 1 passed（实现前）
Observability + Scheduler：   35 passed, 1 skipped
完整 tests/：                 2049 passed, 4 skipped, 38 warnings
真实 Phoenix 冒烟（连续）：   1 passed in 6.35s；1 passed in 6.40s
修改文件 Ruff E/F/I：        passed
Ruff format --check：        passed
修改模块 py_compile：         passed
git diff --check：           passed
独立 reviewer：              Standards 0B/0NB；Spec 0B/0NB
```

关键命令：

```bash
/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q \
  tests/observability tests/app/test_edge_scheduler_backend.py
/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q tests
UNILABOS_PHOENIX_EXECUTABLE=/tmp/unilabos-phoenix-trace-x4fRN7/bin/phoenix \
  /tmp/unilabos-phoenix-trace-x4fRN7/bin/python -m pytest -q \
  tests/observability/test_phoenix_integration.py
```

reviewer 独立重复得到 focused 35 passed、1 skipped；完整 2049 passed、4 skipped、39
warnings；真实 Phoenix 1 passed in 6.34s。warning 数量差异来自既有可选 SOCKS 提示是否
在当次运行触发，不是新增失败或 warning 类别。

全目录 `python -m compileall -q unilabos` 会命中 integration 基线已有的
`unilabos/devices/cytomat/cytomat.py:4` 未闭合括号；正式静态门对所有修改 Python 模块
执行并通过。裸仓根 `pytest -q` 还会收集硬件示例并尝试连接
`192.168.3.2:502`、导入未安装的 `cameraUSB`；仓库 `AGENTS.md` 规定的完整门为
`pytest tests`，本轮已执行并通过。

两个大型 legacy ROS 文件既有 Ruff F diagnostics 在候选前后均为
`base_device_node.py=18`、`host_node.py=18`，本轮没有新增 F diagnostics。

## 5. E2E 与可视证据

本轮是后端 runtime/ROS2/trace 链路，没有浏览器可见页面，因此没有 UI 截图。真实 E2E
证据由 `tests/observability/test_phoenix_integration.py` 提供：启动独立 Phoenix 进程和
SQLite，接收 Electron OTLP protobuf，导出 OS runtime span，经 OS API 查询两类 trace，
并验证包含密码字样的异常消息未进入 Phoenix、`RuntimeError` 类型仍可用于排障。

ROS2 边界由独立与 review regression 覆盖：Scheduler dispatch、local/remote JSON、native
side-channel、hex UUID、嵌套 sync/async、target execute/driver、cancel、TTL/overflow、
离线 ActionServer、no-op tracing 与 shutdown-after-registration。

## 6. 交付状态

本轮 production/test SHA 已通过独立审查。当前只完成实现分支上的本地提交和 ledger，
没有合并到 `integration/workflow-task-runtime`，也没有 push；后续合并或推送需要用户明确
授权。
