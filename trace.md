# Trace: Uni-Lab-OS

### EARS — Session Start (2026-05-27 14:01)
<!-- concepts: device-exception-handling, user-actions, skip-operation -->
- Task: 在设备异常处理的默认操作中添加"跳过"选项
- Why: 用户需要在设备异常时能够跳过失败的动作继续执行后续步骤，而不是只能重试或终止整个任务

**完成情况**：
1. ✅ `DeviceException` 基类 `_default_actions()` 添加 skip（retry → skip → abort）
2. ✅ `base_device_node.py` 未预期异常处理添加 skip
3. ✅ `TimeoutException` 添加 skip（retry → skip → manual_fix → abort）
4. ✅ `ModbusConnectionError` 添加 skip（retry → skip → manual_fix → abort）
5. ✅ `ResourceConflictError` 已有 skip，无需修改
6. ✅ `ParameterError` 和 `EmergencyStopError` 不适合 skip，保持原样

**修改文件**：
- `unilabos/devices/exceptions.py` - 3 处修改
- `unilabos/ros/nodes/base_device_node.py` - 1 处修改

**验证**：所有异常类型的 suggested_actions 都正确包含 skip 选项。

### EARS — Progress (2026-05-18 14:30)
<!-- concepts: editable-install, conda-env-shadowing -->
踩坑：conda `unilab` env 里有 pip 装的 unilabos 旧版本（site-packages），GitHub repo 的修改不会被加载。必须在 repo 根目录 `pip install -e .` 覆盖。否则跑的还是云端 bohrium 地址，本地异常处理代码完全不生效。

### EARS — Fix (2026-05-18 16:12)
<!-- concepts: silent-except, factory-naming -->
踩坑：`_handle_device_exception` 中 `_get_ws_client()` 始终返回 None，导致设备异常报警从未上行、前端一直 loading。根因是 `_get_ws_client()` 写的是 `from unilabos.app.communication import CommunicationFactory`，但实际类名是 `CommunicationClientFactory`。`ImportError` 被裸 `except Exception: return None` 静默吞掉。教训：禁止 `except Exception` 兜底返回 None —— 让真实错误自然抛出才能定位问题，符合用户 CLAUDE.md 中"严格控制 try/except"原则。

### EARS — Fix (2026-05-21 17:36)
<!-- concepts: asyncio, 线程安全, Future, call_soon_threadsafe -->
**问题**：用户点击异常弹窗的"重试/终止任务"按钮后，Edge 侧永远不执行对应操作。

**根本原因**：跨线程 Future 唤醒失败
- `_wait_for_user_decision` 在 ROS2 设备节点的事件循环中创建 Future 并 await
- WebSocket 客户端在**另一个线程/事件循环**中接收决策消息
- `handle_user_decision` 直接调用 `fut.set_result(decision)` 无法跨线程唤醒 Future
- 导致 `await asyncio.wait_for(fut)` 永远阻塞

**解决方案**：
```python
# 错误：直接 set_result（同线程才有效）
fut.set_result(decision)

# 正确：使用 call_soon_threadsafe 跨线程唤醒
fut.get_loop().call_soon_threadsafe(fut.set_result, decision)
```

**调试过程**：添加详细日志，发现 `fut.set_result()` 被调用但 `await` 没有返回，识别出经典的 asyncio 跨线程问题。

**修改文件**：`unilabos/ros/nodes/base_device_node.py:1681` - `handle_user_decision` 方法

**知识点**：asyncio.Future 必须在创建它的事件循环中设置结果，跨线程必须用 `call_soon_threadsafe`。

### EARS — Fix (2026-05-28 11:27)
<!-- concepts: status-propagation, workflow-display, edge-backend-frontend -->
**问题**：用户点击"跳过"操作后，工作流中的 action 块仍然显示绿色边框（success），而不是灰色边框（skipped）。

**根本原因**：跳过状态在多个层级被覆盖或未正确传递
1. **Edge 侧**：`base_device_node.py:1587` 正确返回 `{"status": "skipped"}`
2. **HostNode 层**：`host_node.py:943` 硬编码 `status = "success"`，忽略了 `result_data` 中的 status 字段
3. **WebSocket 层**：`ws_client.py:1513` 只处理 `["success", "failed"]`，不包含 `"skipped"`，导致任务未被标记为完成

**解决方案**：
```python
# 1. host_node.py:943 - 优先读取 result_data 中的 status
result_data = convert_from_ros_msg(result_msg)
status = result_data.get("status", "success")  # 之前硬编码为 "success"

# 2. ws_client.py:1513 - 将 skipped 加入最终状态列表
if status in ["success", "failed", "skipped"]:  # 之前只有 success 和 failed
    self.queue_processor.handle_job_completed(item.job_id, status)
```

**修改文件**：
- `unilabos/ros/nodes/presets/host_node.py:943`
- `unilabos/app/ws_client.py:1513`
- `Uni-Lab-Cloud/web/src/app/@enterprise/(new)/laboratory/[uuid]/workflow/[workflowid]/components/basic-node.tsx:153`（前端样式已修改）

**知识点**：状态传递链路中任何一层的硬编码或遗漏都会导致状态丢失。需要追踪完整的数据流：Edge → HostNode → WebSocket → Backend → Frontend。


### EARS — Session Start (2026-07-06 15:42)
<!-- concepts: exception-handling, project-structure, module-organization -->
- Task: 评估 unilabos/devices/exceptions.py 放在项目中的合适位置
- Why: 用户希望重构异常处理相关文件的存放位置，让代码结构更合理，便于后续开发


### EARS — Progress (2026-07-06 15:50)
<!-- concepts: exception-handling, module-organization, refactoring -->
把 `unilabos/devices/exceptions.py` (框架级异常, 220 行) 合并到 `unilabos/utils/exception.py` (原本只有 DeviceClassInvalid 一个类), 让框架级异常和加载期异常 (DeviceClassInvalid) 集中管理。已删除旧文件, 正在批量改 6 处 import。发现 DeviceClassInvalid 在 host_node.py:650 和 initialize_device.py:31/33/36 有 4 处实际使用点, 保留而非删除。方案理由: devices/ 目录本是驱动层, 框架级异常放这里违反分层, utils/ 才是框架基础设施的正确归属。


### EARS — Progress (2026-07-07 16:32)
<!-- concepts: exception-handling, exec-loop-refactor, decision-loop -->
- Step 0 主体完成：`base_device_node.py` 三个 helper (`_execute_with_exception_handling` / `_handle_device_exception` / `_wait_for_user_decision`) 已删除，压平为一个 `_run_action_with_decision_loop` 方法（for-except 循环 + alarm 上报 + Future 等待 + 决策分派 一体化）
- 关键设计决定：
  - `except Exception` 统一捕获，判类型时用 `isinstance(exc, DeviceException)` 分派 alarm 序列化格式（DeviceException 走 `to_alarm_dict()`，其他 Exception 走内联兜底 dict）
  - **不再 wrap 成 DeviceException**：普通 Exception 直接进入决策流，`raise`（裸抛）保留原始 traceback
  - handler 抛错不 catch —— 让它自然冒泡回 for-except 顶端（删除了原本手抄的 `next_decision` 分派逻辑）
  - `handle_user_decision` (public ws 回调) 和 `_get_ws_client` 保留不动
- 待处理：line 1872 / 2349 两处调用点需改名（`_execute_with_exception_handling` → `_run_action_with_decision_loop`），`tests/test_exception_handling.py` 的 FakeNode 需同步压平

### EARS — Progress (2026-07-07 16:32)
<!-- concepts: exception-handling, exec-loop-refactor -->
- Step 0 base_device_node.py 4 处 edit 全部完成，非 thrashing 而是有序推进：
  1. helper 三合一压平（1555-1725 → `_run_action_with_decision_loop`）
  2. 传统 ROS action server 调用点改名（原 line 1872）
  3. JSON command 调用点改名（原 line 2349）
  4. 注释里 `_execute_with_exception_handling` 提及改名（原 line 1835）
- grep 确认全库无残留 `_execute_with_exception_handling` / `_handle_device_exception` / `_wait_for_user_decision`（tests 里还有 FakeNode 自己的定义，那是独立复刻，下一步同步）

### EARS — Progress (2026-07-07 16:42)
<!-- concepts: exception-handling, error-policy, registry-propagation, ws-protocol -->
按 `action-timeout-reporting_9983aa0d.plan.md` 推进 Step 1-5:
- Step 1: 新建 `unilabos/registry/action_policy.py`(ErrorPolicy / ErrorPolicyOption TypedDict + normalize_error_policy 剔除 callable handler + collect_option_handlers 保留 handler 映射)
- Step 2: `@action` 加 `execution_timeout` / `error_policy`,写入 `_action_registry_meta`;handler 单独挂 `_action_error_policy` 到 wrapper(callable 不进 registry cache)
- Step 3: AST scanner `_CACHE_VERSION` 升到 4;action_args setdefault 补齐;error_policy.options 里的 handler 字段被剔除(AST 阶段 handler 会被解析为 import 字符串)
- Step 4: registry.py 新增 `_apply_exception_meta` 辅助函数,统一给 UniLabJsonCommand 和 ROS Action 两条构建路径注入 timeout/execution_timeout/exception_handling/default_on_user_timeout/error_policy
- Step 5(进行中): ws_client 协议迁移 `device_exception_decision` -> `job_error_decision`
- 29 个测试仍全绿

### EARS — Progress (2026-07-07 16:52)
<!-- concepts: exception-handling, decision-loop, error-policy, ws-protocol, suc-type -->
Step 5-7 完成:
- Step 5: ws_client 协议 `device_exception_alarm` -> `job_error_decision_required` (上行), `device_exception_decision` -> `job_error_decision` (下行);同步更新 base_device_node 调用点和 test mocks
- Step 6: `_run_action_with_decision_loop` 扩参 (error_policy + option_handlers),从 policy 读 max_retries/decision_timeout_seconds/default_on_decision_timeout/allow_retry/allow_skip/options;alarm 的 category/severity 也走 policy 兜底。两条调用点(传统 ROS action + JSON command)都传入 `normalize_error_policy(ACTION._action_error_policy)` 与 `collect_option_handlers(...)`。
- 新增 4 个测试覆盖:max_retries 覆盖、default_on_decision_timeout=skip 场景、policy.options + option_handlers 装配 handler、allow_skip=False 时 skip 语义仍生效
- 关键教训:`DeviceException.__init__` 用 `suggested_actions or self._default_actions()`,`[]` 是 falsy,子类实例默认永远非空,想验证 fallback 分支必须用非 DeviceException
- Step 7 起步:`serialize_result_info` / `get_result_info_str` 加 `suc_type` 可选 kwarg,只有非空才写入 dict(保持旧字段兼容)
- 33 个测试全绿

### EARS — Progress (2026-07-07 17:54)
<!-- concepts: error-policy, fault-injection-driver, decision-loop -->
- fault_injection_device.py 新增 `raise_with_policy_options` @action + `shutdown` / `manual_check` 两个 @not_action fallback,专门覆盖 `error_policy.options` **声明式**决策路径(优先级 2,当前测试驱动此前 0 覆盖)
- 关键前提确认:`_run_action_with_decision_loop` 里 options 三级优先级 = 实例 suggested_actions > policy.options > 兜底。要触发 policy 路径,必须抛**非 DeviceException**(用 `ValueError` 干净),否则 `DeviceException._default_actions()` 永远非空,policy.options 被吞
- 覆盖了 `fallback_action` 字符串映射 + `then=continue`(fallback 结果直接作为 action 结果)与 `then=retry`(fallback 后重跑原 action)两种典型行为,搭配 `max_retries=5` / `decision_timeout_seconds=120` / `default_on_decision_timeout=abort` 三个 policy 级参数
- 用户澄清:此前误以为 `fault_injection_device.py` 是按 `action-timeout-reporting_9983aa0d.plan.md` 写的,实际是按 `02_decorator_based_plan.md`(DeviceException + UserAction 方案)。两套 API 并存,协议名相同 (`job_error_decision_required` / `job_error_decision`)
