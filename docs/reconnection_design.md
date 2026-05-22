# 断线重连功能设计文档

## 需求背景

当 edge 端与后端断开连接后重新连接时,需要同步断线期间的状态变化:
- edge 端在启动时会发送 `host_node_ready` 消息给后端
- 后端需要返回断线之前下发给该 edge 的任务列表
- edge 端需要检查当前实际状态,并将状态同步给后端

## 消息流程

```
Edge                          Backend
  |                              |
  |---> host_node_ready -------->|
  |     (设备列表、动作列表)       |
  |                              |
  |<--- host_node_ready_response-|
  |     (断线前的任务列表)         |
  |                              |
  |--- 检查本地状态 ---|          |
  |                              |
  |---> reconnection_sync ------>|
  |     (实际状态同步)            |
```

## 数据结构

### host_node_ready_response 消息格式

```json
{
  "action": "host_node_ready_response",
  "data": {
    "pending_jobs": [
      {
        "job_id": "uuid",
        "task_id": "uuid", 
        "device_id": "device_name",
        "action_name": "action_name",
        "status": "started|ready|queue",
        "timestamp": 1234567890.0
      }
    ]
  }
}
```

### reconnection_sync 消息格式

```json
{
  "action": "reconnection_sync",
  "data": {
    "synced_jobs": [
      {
        "job_id": "uuid",
        "actual_status": "completed|failed|not_found",
        "result": {},
        "error": "error message if failed"
      }
    ],
    "active_jobs": [
      {
        "job_id": "uuid",
        "device_id": "device_name",
        "action_name": "action_name",
        "status": "started"
      }
    ]
  }
}
```

## 实现方案

### 1. 消息处理 (ws_client.py)

在 `MessageProcessor` 类中添加:

```python
async def _handle_host_ready_response(self, data: Dict[str, Any]):
    """
    处理 host_node_ready 的响应
    
    后端返回断线前下发的任务列表,需要:
    1. 检查这些任务在 edge 端的实际状态
    2. 将实际状态同步给后端
    """
    pending_jobs = data.get("pending_jobs", [])
    
    if not pending_jobs:
        logger.info("[Reconnection] No pending jobs from backend")
        return
    
    logger.info(f"[Reconnection] Received {len(pending_jobs)} pending jobs from backend")
    
    # 收集实际状态
    synced_jobs = []
    active_jobs = []
    
    for job_info in pending_jobs:
        job_id = job_info.get("job_id")
        device_id = job_info.get("device_id")
        action_name = job_info.get("action_name")
        backend_status = job_info.get("status")
        
        # 检查本地状态
        actual_status = await self._check_job_actual_status(job_id, device_id, action_name)
        
        if actual_status["status"] == "completed" or actual_status["status"] == "failed":
            # 任务已完成,同步结果
            synced_jobs.append({
                "job_id": job_id,
                "actual_status": actual_status["status"],
                "result": actual_status.get("result", {}),
                "error": actual_status.get("error", "")
            })
        elif actual_status["status"] == "running":
            # 任务仍在运行
            active_jobs.append({
                "job_id": job_id,
                "device_id": device_id,
                "action_name": action_name,
                "status": "started"
            })
        else:
            # 任务不存在,可能已被清理
            synced_jobs.append({
                "job_id": job_id,
                "actual_status": "not_found",
                "error": "Job not found in edge"
            })
    
    # 发送同步消息
    self.send_message({
        "action": "reconnection_sync",
        "data": {
            "synced_jobs": synced_jobs,
            "active_jobs": active_jobs
        }
    })
    
    logger.info(f"[Reconnection] Synced {len(synced_jobs)} completed jobs, {len(active_jobs)} active jobs")
```

### 2. 状态检查 (ws_client.py)

```python
async def _check_job_actual_status(
    self, job_id: str, device_id: str, action_name: str
) -> Dict[str, Any]:
    """
    检查任务的实际状态
    
    返回:
    - status: "completed" | "failed" | "running" | "not_found"
    - result: 任务结果 (如果已完成)
    - error: 错误信息 (如果失败)
    """
    # 1. 检查 DeviceActionManager 中的任务状态
    job_info = self.device_manager.get_job_info(job_id)
    if job_info:
        if job_info.status == JobStatus.STARTED:
            return {"status": "running"}
        elif job_info.status == JobStatus.QUEUE or job_info.status == JobStatus.READY:
            return {"status": "running"}  # 排队中也算运行中
    
    # 2. 检查 HostNode 中的 ROS2 goal 状态
    host_node = HostNode.get_instance(0)
    if host_node:
        goal_status = host_node.get_goal_status(job_id)
        if goal_status:
            if goal_status["is_active"]:
                return {"status": "running"}
            elif goal_status["is_succeeded"]:
                return {
                    "status": "completed",
                    "result": goal_status.get("result", {})
                }
            elif goal_status["is_failed"]:
                return {
                    "status": "failed",
                    "error": goal_status.get("error", "Unknown error")
                }
    
    # 3. 检查本地存储的任务结果 (如果有持久化)
    from unilabos.app.web.controller import get_job_result
    stored_result = get_job_result(job_id)
    if stored_result:
        return {
            "status": stored_result["status"],
            "result": stored_result.get("result", {}),
            "error": stored_result.get("error", "")
        }
    
    # 4. 任务不存在
    return {"status": "not_found"}
```

### 3. HostNode 扩展 (host_node.py)

在 `HostNode` 类中添加方法:

```python
def get_goal_status(self, job_id: str) -> Optional[Dict[str, Any]]:
    """
    获取指定 job_id 的 ROS2 goal 状态
    
    返回:
    - is_active: 是否正在执行
    - is_succeeded: 是否成功完成
    - is_failed: 是否失败
    - result: 结果数据 (如果已完成)
    - error: 错误信息 (如果失败)
    """
    with self._goal_handles_lock:
        if job_id in self._goal_handles:
            goal_handle = self._goal_handles[job_id]
            status = goal_handle.status
            
            return {
                "is_active": status in [GoalStatus.STATUS_EXECUTING, GoalStatus.STATUS_ACCEPTED],
                "is_succeeded": status == GoalStatus.STATUS_SUCCEEDED,
                "is_failed": status in [GoalStatus.STATUS_ABORTED, GoalStatus.STATUS_CANCELED],
                "result": self._goal_results.get(job_id, {}),
                "error": self._goal_errors.get(job_id, "")
            }
    
    return None
```

## 状态处理逻辑

### 场景 1: 任务已完成但后端未收到

- Edge 检测到任务已完成
- 通过 `reconnection_sync` 发送完整的结果给后端
- 后端更新任务状态

### 场景 2: 任务仍在执行

- Edge 检测到任务仍在运行
- 通过 `reconnection_sync` 告知后端任务状态为 `started`
- 后端保持任务状态,等待后续的 `job_status` 更新

### 场景 3: 任务不存在

- Edge 在本地找不到该任务
- 可能原因:
  - 任务从未到达 edge
  - 任务已完成并被清理
  - Edge 重启导致内存状态丢失
- 通过 `reconnection_sync` 告知后端 `not_found`
- 后端决定是否重新下发或标记为失败

### 场景 4: 任务在队列中

- Edge 检测到任务在排队
- 通过 `reconnection_sync` 告知后端任务状态为 `queue`
- 后端保持任务状态,等待执行

## 注意事项

1. **时序问题**: `host_node_ready_response` 可能在 edge 启动后立即到达,此时某些组件可能尚未完全初始化
   - 解决: 在处理前检查 HostNode 是否就绪

2. **并发问题**: 在检查状态时,任务状态可能正在变化
   - 解决: 使用锁保护关键数据结构的访问

3. **结果持久化**: 如果 edge 重启,内存中的任务状态会丢失
   - 当前方案: 依赖 `get_job_result` 从本地存储读取
   - 未来优化: 考虑将关键任务状态持久化到文件

4. **大量任务**: 如果断线时间较长,可能有大量待同步任务
   - 解决: 分批处理,避免单次消息过大

## 测试场景

1. **正常重连**: edge 断线后立即重连,任务仍在执行
2. **延迟重连**: edge 断线较长时间后重连,部分任务已完成
3. **重启重连**: edge 进程重启后重连,内存状态丢失
4. **多任务重连**: 断线期间有多个设备的多个任务
5. **并发重连**: 重连时有新任务下发

## 实现步骤

1. ✅ 在 `MessageProcessor._process_message` 中添加 `host_node_ready_response` 处理分支
2. ⬜ 实现 `_handle_host_ready_response` 方法
3. ⬜ 实现 `_check_job_actual_status` 方法
4. ⬜ 在 `HostNode` 中添加 `get_goal_status` 方法
5. ⬜ 添加必要的数据结构 (如 `_goal_results`, `_goal_errors`)
6. ⬜ 编写单元测试
7. ⬜ 集成测试

## 后续优化

1. 添加任务状态持久化,支持进程重启后恢复
2. 优化大量任务的同步性能
3. 添加状态同步的重试机制
4. 支持增量状态同步 (只同步变化的部分)
