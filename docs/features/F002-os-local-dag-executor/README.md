# F002 OS 本地 DAG 执行器

F002 把整张 `TaskDag` 的依赖执行、资源 admission、逐节点结果、取消、持久化与恢复放到
OS 本地。它是 F003 local bridge 和统一 v1 runtime 的执行基础。

## 从哪里开始看

- [requirement.md](requirement.md)：用户故事、验收条件与调度不变量。
- [interface-design.md](interface-design.md)：冻结的 `task_dag`、`job_status`、`cancel_task`
  线协议和 executor 设计。
- [feature-list.json](feature-list.json)：子功能及状态。
- [progress.md](progress.md)：实现过程和已知环境事实。
- [checklist.md](checklist.md)：验收证据。
- [scheduler README](../../../unilabos/scheduler/README.md)：当前 executor/debugger 代码指引。
- [runtime README](../../../unilabos/runtime/README.md)：journal、run/node/event 与恢复指引。

## 当前原则

- 整图一次编译/下发，控制节点和边不能丢失。
- `DagWalk` 是依赖偏序事实源；`DagExecutor` 只负责异步 admission 与执行。
- 同设备/资源互斥由资源层负责，不能揉进走图算法。
- 逐节点终态来自真实设备执行路径和 journal，不从传输 ack 推断。
- 失败、取消、断线和重启必须收敛到可审计状态。
- debugger 在资源 admission 前工作；起始点是完整图上的运行配置。

## 与后续统一接口的关系

F002 的 `task_dag`/`job_status` 是 OS 与旧 backend/bridge 的冻结 wire contract。
新前端不直接使用该 wire，而是通过 F003 提供的 `/api/v1/workflows/*`、
`/api/v1/authoring/*` 和 `/api/v1/runtime/*` 访问同一个执行器。

不要在 F002 中加入前端专用状态、颜色、代码编辑或 backend HTTP 路由；这些分别属于
前端投影、authoring 层和 local bridge。

## 验证

```bash
UNILAB_PY=/home/changjunhan/.micromamba/envs/unilab/bin/python
"$UNILAB_PY" -m pytest tests/scheduler
"$UNILAB_PY" -m pytest tests/runtime
```

所有命令都在 `unilab` Python 3.11 环境执行。
