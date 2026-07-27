# F003 OS 本地工作流桥

> 本目录保留 F003 最初“两套前端、三面 bridge”的需求与验收档案。当前运行时边界以本文和
> `unilabos/app/local_bridge/README.md` 为准；历史文档中的 Cloud panel WS 不再代表现状。

## 当前结论

`local_bridge` 让 `uni-lab-fe` 通过统一 `unilab/v1` 接口把完整工作流 DAG 交给 OS，
并把 OS 的权威运行状态投影回前端。它不复制 scheduler、debugger、workflow store 或
物料事实源。

2026-07-26 起：

- 旧 Cloud panel `/ws/workflow/{uuid}`、端口 8891、`workflow_ws.py` 及其测试均已删除，
  不保留兼容入口，也不得重新引入。
- bridge 只保留两个网络面：OS schedule WS `:8890` 与统一 HTTP/WS API `:8014`。
- `uni-lab-fe` 内部工作流引擎和 `@unilab/services` 是唯一前端调用面；Cloud 工作流
  画布不再迁移。
- `/api/run`、`/api/runtime/local/*` 仅是尚未清理的 legacy HTTP 兼容路由，新组件不得使用。

## 与 F002 的关系

F002 负责整张 DAG 的本地执行：`task_dag` 下行，`DagExecutor`/`TaskDagRunner` 本地走图，
`job_status` 上行。F003 只负责把统一 v1 API 映射到这条既有路径，不改变执行语义：

```text
uni-lab-fe
  -> :8014 unilab/v1
  -> LocalApiState / authoritative runtime
  -> :8890 schedule session
  -> OS DagExecutor
```

物料读取复用同一条 schedule session，但事实源不是 bridge：`unilab -g/--graph` 启动时
建立 OS 的当前 `ResourceTreeSet`，OS 内部可继续修改它；bridge 只缓存其只读快照，并在
每次 material GET 前主动向 OS 刷新。

## 文档职责

| 文件 | 作用 |
|---|---|
| `requirement.md` | F003 原始需求档案，包含已退役的 Cloud panel 验收目标 |
| `interface-design.md` | 原三面协议设计档案；首页退役声明优先于旧章节 |
| `feature-list.json` | 历史任务与当时验证证据，不作为当前接口清单 |
| `progress.md` / `checklist.md` | 历史实现与验收记录 |
| [local_bridge README](../../../unilabos/app/local_bridge/README.md) | 当前启动方式、端口和文件职责 |
| [Material API](../../../unilabos/app/local_bridge/MATERIAL_API.md) | 当前物料内存权威与 OS/backend 对照 |
| [runtime README](../../../unilabos/runtime/README.md) | 当前运行投影和事件 journal |
| [scheduler README](../../../unilabos/scheduler/README.md) | executor、断点和单步语义 |

## 不能做

- 不能恢复 8891、`workflow_ws.py` 或 `/ws/workflow/{uuid}`。
- 不能由 bridge 或前端裁剪 DAG 来实现起始点；OS 必须收到完整图并权威产生 `skipped`。
- 不能在资源申请或设备入队后才命中断点。
- 不能把 WebSocket/HTTP 成功当作节点成功。
- 不能让 offline 模式复制或简化 executor/debugger。
- 不能让 bridge 从 graph 文件反复读取“当前物料”；运行时权威只能是 OS 内存中的
  `ResourceTreeSet`。
- 不能用系统 Python；一律使用 `unilab` Python 3.11 环境。
