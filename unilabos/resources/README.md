# OS 资源与当前物料状态

本目录维护 OS 的设备/资源图语义。运行时只有一份可变权威：
`ResourceTreeSet`。它既是设备动作和资源解析使用的对象，也是前端只读物料图的来源。

## 启动与所有权

```text
unilab -g/--graph <graph.json>
  -> app/main.py 解析一次 graph 文件
  -> 创建 ResourceTreeSet
  -> OS 内部设备、调度和状态更新继续修改同一个对象
  -> CurrentMaterialState 持有该对象的引用
```

graph 文件是启动输入，不是运行时数据库。启动后不得通过定时读取文件、文件时间戳或
bridge 自己的副本覆盖 OS 当前状态。

`CurrentMaterialState` 位于 `material_state.py`，职责只有：

- 绑定当前 `ResourceTreeSet`，不深拷贝出第二棵可写图。
- 按请求将当前节点序列化为 `unilab/material-snapshot-v1`。
- 过滤 `host_node` 等运行时内部节点，并把 parent 规范化为来源节点 ID。
- 为只读投影计算 revision；它不是持久化层，也不提供前端写入口。

OS 内部更新节点后，读取下一份 snapshot 就会看到新状态。主动推送用于降低延迟，但一致性
不依赖每一次推送都成功：前端 material GET 会经 local bridge 再向 OS 请求当前快照。

## 调用链

```text
uni-lab-fe GET /api/v1/materials
  -> local_bridge.local_api
  -> ScheduleSession.request_material_snapshot()
  -> :8890 query_material_snapshot
  -> WebSocketClient MessageProcessor
  -> CurrentMaterialState.snapshot()
  -> :8890 material_snapshot
  -> MaterialGraphCatalog.replace_snapshot()
  -> 只读 MaterialAggregate HTTP 响应
```

离线调试时可以运行 `local_bridge --offline -g <graph.json>`。此时 bridge 也只在启动时读取
一次文件，然后维护内存快照；真实 OS 模式禁止 bridge 自己接受 graph 文件，因为
`unilab -g` 才是图的所有者。

## 修改约束

- 所有 OS 内部修改必须落在当前 `ResourceTreeSet`，不能另建 material store 分叉。
- 前端和 local bridge 对物料保持只读；Site 的增删不从前端发起。
- 高频 joint/pose 状态不要写回静态相对位置，也不要让 React Flow 查询随其高频重渲染。
- 快照 schema 变化时，必须同步更新 `local_bridge/material_api.py`、services 契约测试和
  [Material API 对照](../app/local_bridge/MATERIAL_API.md)。
