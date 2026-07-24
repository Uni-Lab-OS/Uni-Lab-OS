# 需求规格: Quick Debug Alpha（W1–W2）

> **Author: HUMAN-approved design** | 来源：主仓 `product_designs/workflow_and_protocol/2026-07-18-os-scheduler-locks-and-controlflow-dev-plan.md` 的 W1–W2，2026-07-21 已确认两周 Alpha 口径。

## 背景

单工站开发者需要在不启动 Go、PostgreSQL、Layer B 或排程算法的情况下，使用 Uni-Lab-OS、local SQLite 和共享云前端完成单点动作与工作流调试。实现必须扩展现有 TaskDag/local bridge，不能另建执行器。

工作流作者源采用本地文件优先：规范化 Python（必要时伴随 Canonical JSON/YAML）进入 Git；SQLite 保存文件索引、可恢复 draft 和运行 journal，不作为源码权威。

## 用户故事

```
As a 工站开发工程师,
I want to 在本地编译、运行、观察并恢复同一份 Python/DAG 工作流,
So that 我能以最短部署链路调试设备，并把相同定义无缝带入后续实验室排程。
```

## 验收标准

### AC-1: ActionContract v2 向后兼容

```
Given 未迁移的 v1 @action,
When registry 构建并序列化,
Then 输出保持兼容；启用 v2 contract 时可表达 typed data/material ports、资源 claim、时长和恢复策略，非法组合给出 typed error。
```

### AC-2: Python → Canonical → TaskDag

```
Given 普通 Python 函数调用、named output、常量和 RuntimeParameter,
When 编译 WorkflowRevision,
Then 生成稳定 content hash/source map，并能在 dispatch 前解析 NodeOutputRef；缺失或类型错误的结果不触发下游设备动作。
```

### AC-3: Layer A 实时安全

```
Given 并发节点声明 device/material/slot/tank claims,
When ready policy 尝试 admission,
Then acquire-all 全有或全无、顺序确定、同一资源不会双持有；超时/断线资源进入 unknown 且不会自动释放。
```

### AC-4: Quick Debug 运行与恢复

```
Given branch/skip、fork/join、静态 finite loop 和显式或单一路径 transport,
When 在 local bridge 提交并运行,
Then SQLite journal 原子保存节点终态、result、effect、outbox 和 runtime_epoch；进程重启不重复未知物理动作。
```

### AC-5: 时间线语义

```
Given 只有 DAG 与 estimated_duration_s,
When 请求时间线,
Then EstimatedTimeline 只计算拓扑 earliest-start 并固定 is_resource_constrained=false；ObservedGantt 只由真实 RunEvent 投影。
```

### AC-6: pTLC Alpha adapter

```
Given pTLC 单样品完整 Recipe 和 fake PLC,
When 通过 OS Action/Workflow path 执行,
Then spotting/before_photo/develop/scrape/collect macro 的关键 trace、资源与物料语义与 golden baseline 一致；旧 runtime 仍可在无 active run/handoff/unknown lock 时回退。
```

### AC-7: Harness 绿色门禁

```
Given W1–W2 实现完成,
When 执行仓库 verify loop,
Then import unilabos、相关 pytest、property-based 不变量测试和跨端 E2E 全部通过，且测试不连接真实硬件、不使用墙钟 sleep。
```

### AC-8: 文件优先的工作流持久化

```
Given 开发者从 Code/Graph 保存工作流,
When source 编译与校验成功,
Then 先原子替换规范化本地文件并得到 content hash，再更新 SQLite URI/hash/index；索引漂移时以文件重建，draft 不得执行。
```

## 涉及模块

- Registry：`unilabos/registry/`
- Workflow compiler：`unilabos/workflow/`
- TaskDag/runtime：`unilabos/scheduler/`、`unilabos/runtime/`
- Local bridge：`unilabos/app/local_bridge/`
- 通用 Profile/driver：`unilabos/runtime/profile_loader.py`、`unilabos/devices/generic_plc_macro.py`

## 正确性关注点

- 锁的不变量：任一时刻同一 claim 最多一个 holder；失败不留下部分锁。
- 编译确定性：相同语义输入产生相同执行 hash，布局变化不改变 hash。
- hermetic：fake transport、可控时钟、无真实 ROS/DDS/OPC-UA 依赖。
- 平台通用性：OS Runtime API、local bridge 和 TaskDag wire model 不得出现 pTLC 专用端点或请求类型；pTLC 产品层只能作为声明式 Profile 接入。Profile 可用 driver key 引用设备插件，但不能形成独立 Runtime、子应用或 API namespace；OS core 不得包含 `devices/ptlc` 生产包。

## 不做什么

- Layer B future reservation、SchedulerClient、PlanManager、排程算法。
- 多候选设备/slot/运输路线的自动选择。
- 旧 pTLC runtime 退役、全量 139 模板迁移、生产 soak。
- PTLC debugger 等价能力（step-over、breakpoint、run-to、任意节点安全起跑）和运行时不定次循环；journal resume 不得冒充 debugger。
