---
name: action-timeout-reporting
overview: 在 `@action` 元数据中新增执行超时与 `error_policy` 报错交互策略；运行时由 exec callback 捕获同步/异步异常和 traceback，立即向后端发送需要用户决策的消息，后端透传给既有前端 `device_exception` 弹窗。用户选择 retry/skip/custom 后通过新增回传协议回到 Edge：retry 由 Edge 重试当前 action，skip 返回成功但标记 `suc_type=user_bypass_error`，custom 则执行指定 fallback 函数，fallback 再失败时把原始错误与 fallback 错误一并彻底上报失败。
todos:
  - id: add-action-timeout-policy-meta
    content: 在 @action、AST scanner、registry/YAML 链路中透传 execution_timeout 与 error_policy，默认不启用破坏性行为
    status: pending
  - id: define-error-policy-typing
    content: 新增 ErrorPolicy TypedDict/type extension，指导用户声明 retry/skip/custom options/fallback
    status: pending
  - id: add-edge-decision-protocols
    content: 新增两个 Edge-WebSocket action 协议：报错决策请求上行与用户决策回传
    status: pending
  - id: implement-exec-decision-loop
    content: 在 _create_execute_callback 中实现异常收集、即时通知、等待决策、retry/skip/custom fallback 分支
    status: pending
  - id: preserve-return-info-contract
    content: 保持 return_info 兼容，skip 时 suc=true 且 suc_type=user_bypass_error，失败时合并原始与 fallback traceback
    status: pending
  - id: verify-behavior
    content: 补充最小测试或可运行验证，覆盖同步/异步 traceback、timeout、retry、skip、自定义 fallback
    status: pending
isProject: false
---

# Exec Callback 报错交互、重试/跳过与超时上报计划

## 目标

报错发生时，Edge 需要立刻把“需要用户处理的设备异常”发送到后端。后端保持透传，把消息推给前端，复用 Uni-Lab-Cloud 现成的 `device_exception` 弹窗/通知抽屉。用户在前端点击“重试 / 跳过 / 自定义操作”后，后端立刻把决策消息发回 Edge。

这次方案不是只在最终 `failed` 结果里塞建议，而是让 exec callback 在 action 仍处于错误处理阶段时进入一个可恢复决策循环：

1. action 抛错或超时。
2. Edge 收集 traceback、job id、task id、action/device 信息、可展示 options。
3. Edge 上行 `job_error_decision_required`。
4. 后端透传成前端已有的 `device_exception` 通知。
5. 前端展示 options，用户选择后提交。
6. 后端回传 `job_error_decision`。
7. Edge 根据选择执行 retry / skip / custom fallback。

## 现状判断

- `unilabos/ros/nodes/base_device_node.py` 的 `_create_execute_callback` 已经统一处理同步和异步 action。同步动作走 `ThreadPoolExecutor.submit(...)`，异步动作走 rclpy/async task；异常会进入 `execution_error`，最后写入 `return_info.error`。
- `unilabos/utils/type_check.py` 的 `serialize_result_info/get_result_info_str` 当前格式是 `{"error": str, "suc": bool, "return_value": any}`。后续需要兼容扩展 `suc_type`，不能破坏旧消费者。
- `unilabos/ros/nodes/presets/host_node.py` 会解析 `return_info.suc`，失败时把 job status 标为 `failed`，再通过 bridge 发给后端。
- `unilabos/app/ws_client.py` 的 `publish_job_status(...)` 当前发送 `action="job_status"`，包含 `job_id/task_id/device_id/action_name/status/return_info/timestamp`，适合继续作为最终结果通道。
- Uni-Lab-Cloud 的 `test` / `feat/lixinyu/dev` 前端已有 `device_exception`：
  - `useDeviceExceptionDialog.tsx` 监听 SSE，收到 `biz_type=device_exception` 自动弹窗。
  - `DeviceExceptionDrawer.tsx` 在通知抽屉展示异常详情和 action 按钮。
  - `useSubmitExceptionDecision.ts` 调 `POST /api/v1/edge/exception/decision`。
  - `types/device-exception.ts` 已定义 `suggested_actions`、`traceback`、`device_snapshot`、`ExceptionDecisionPayload`。
- 因此前端主体不需要重写；我们只要让后端把 Edge 上行消息转成 `device_exception` payload，并把前端 decision 回传给对应 Edge/job。

## 新增 @action 元数据

在 `unilabos/registry/decorators.py` 的 `@action(...)` 上新增两个可选参数：

```python
execution_timeout: Optional[float] = None
error_policy: Optional[ErrorPolicy] = None
```

`execution_timeout` 保持之前计划：默认不启用。超时被视为一种 `error_type="execution_timeout"`，同样进入 `error_policy` 决策流程。

`error_policy` 负责定义报错后是否允许用户处理，以及前端展示哪些 options。它需要做成 type extension / `TypedDict`，让写 action 的用户有类型提示。

建议新增文件：`unilabos/registry/action_policy.py`。

```python
from typing import Any, Dict, List, Literal, NotRequired, TypedDict


class ErrorPolicyOption(TypedDict):
    action: str
    label: str
    description: NotRequired[str]
    fallback_action: NotRequired[str]
    extra: NotRequired[Dict[str, Any]]


class ErrorPolicy(TypedDict, total=False):
    enabled: bool
    allow_retry: bool
    allow_skip: bool
    timeout_seconds: float
    severity: Literal["warning", "error", "critical"]
    category: Literal["network", "hardware", "timeout", "parameter", "resource", "unknown"]
    options: List[ErrorPolicyOption]
```

语义：

- `enabled`：是否启用报错交互。默认建议为 `True`，但只在 action 报错时生效；若为了完全兼容旧动作，也可实现为 `error_policy is not None` 才启用，最终实现前需要定一下默认策略。
- `allow_retry`：默认 `True`。
- `allow_skip`：默认 `True`。
- `options`：自定义按钮列表。`action` 是回传给 Edge 的稳定值；`label` 是前端展示名；`fallback_action` 是用户选择该 option 后 Edge 要调用的函数名。
- 对用户说的 `"关机": 某个函数名`，建议表达为：

```python
@action(
    error_policy={
        "allow_retry": True,
        "allow_skip": False,
        "options": [
            {
                "action": "shutdown",
                "label": "关机",
                "description": "关闭设备后终止当前动作",
                "fallback_action": "shutdown",
            }
        ],
    }
)
def run(...):
    ...
```

最终传给前端的 `suggested_actions` 只包含可展示字段，例如：

```json
[
  {"action": "retry", "label": "重试", "description": "重新执行当前动作"},
  {"action": "shutdown", "label": "关机", "description": "关闭设备后终止当前动作"}
]
```

`fallback_action` 不要求前端理解，但 Edge 侧需要保留映射。用户点击后回来的 `action="shutdown"` 再映射到本地 `fallback_action="shutdown"` 执行。

## 新增两个 action 协议

### 1. Edge -> Backend：请求用户决策

新增 WebSocket action：

```json
{
  "action": "job_error_decision_required",
  "data": {
    "notify_uuid": "edge-generated-or-server-filled",
    "job_id": "job-uuid",
    "task_id": "task-uuid",
    "notebook_id": "optional",
    "device_id": "pump_1",
    "device_uuid": "device-template-or-instance-uuid",
    "action_name": "aspirate",
    "exception_type": "ValueError",
    "category": "hardware",
    "severity": "error",
    "error_message": "short message for UI",
    "traceback": "full traceback",
    "error_context": {
      "attempt": 1,
      "execution_timeout": 30,
      "action_kwargs": {}
    },
    "device_snapshot": {},
    "suggested_actions": [
      {"action": "retry", "label": "重试", "description": "重新执行当前动作"},
      {"action": "skip", "label": "跳过", "description": "跳过该错误并继续流程"},
      {"action": "shutdown", "label": "关机", "description": "执行 shutdown fallback"}
    ],
    "require_confirmation": true
  }
}
```

后端处理建议：

- 创建/更新一条通知，`biz_type="device_exception"`。
- `uuid` 作为 `notify_uuid`，用于后续 decision 关联。
- 原样保存 `data`，前端已有 `DeviceExceptionDrawer` 能直接解析。
- 通过 SSE 推给前端，沿用现成 `device_exception` 逻辑。

### 2. Backend -> Edge：用户决策回传

新增 WebSocket action：

```json
{
  "action": "job_error_decision",
  "data": {
    "notify_uuid": "notice-uuid",
    "job_id": "job-uuid",
    "task_id": "task-uuid",
    "device_id": "pump_1",
    "device_uuid": "device-uuid",
    "action": "retry",
    "label": "重试",
    "reason": "user_choice",
    "extra": {}
  }
}
```

Edge 必须用 `job_id + task_id` 做主索引，`notify_uuid` 做通知索引，避免同设备同 action 并发时错配。

## Exec Callback 决策流程

在 `_create_execute_callback` 中把“执行一次 action”抽成内部 helper，统一支持同步/异步：

```text
attempt = 1
while True:
  result = await run_action_once()
  if success:
    return success result

  decision = await request_user_decision(error, attempt)
  if decision.action == "retry":
    attempt += 1
    continue

  if decision.action == "skip":
    return success result with suc_type=user_bypass_error

  if decision.action in custom options:
    fallback_result = await run_fallback_action(decision.action)
    if fallback success:
      return fallback result, or policy-defined final behavior
    else:
      return failed with original traceback + fallback traceback

  return failed
```

关键点：

- 每次报错都要收集完整 traceback。若 retry 后再次报错，可以把 `attempts` 追加到 `return_value.error_attempts`，最终彻底失败时一起带回。
- retry 是 Edge 自己重试，不需要后端重新发 job_start。
- skip 不执行原 action 的后续逻辑，但整个 action result 要成功返回：

```json
{
  "error": "",
  "suc": true,
  "suc_type": "user_bypass_error",
  "return_value": {
    "bypassed_error": {
      "error": "...原始 traceback...",
      "exception_type": "...",
      "action_name": "...",
      "job_id": "...",
      "task_id": "...",
      "decision": {"action": "skip", "label": "跳过"}
    }
  }
}
```

- custom fallback 执行同一个 driver 上的函数名。函数名来自 `error_policy.options[*].fallback_action`，不是直接信任前端 label。
- fallback 的入参默认建议为空；如需复杂入参，后续可从 `option.extra` 或 `error_policy` 定义，但第一版保持 KISS。
- 如果 fallback 报错，最终彻底失败，`return_info.error` 需要合并：
  - 原始 action traceback
  - 用户选择的 action/label
  - fallback function name
  - fallback traceback

## return_info 兼容扩展

`get_result_info_str` / `serialize_result_info` 增加可选字段，不破坏旧调用：

```python
def serialize_result_info(
    error: str,
    suc: bool,
    return_value=None,
    *,
    suc_type: Optional[str] = None,
    error_policy_result: Optional[dict] = None,
) -> dict:
    ...
```

建议 `suc_type` 取值：

- `normal` 可省略。
- `user_bypass_error`：用户选择跳过错误，动作对工作流视为成功，但 return value 保存被跳过的报错内容。
- 后续可扩展 `fallback_recovered`，表示 fallback 成功恢复。

`HostNode.get_result_callback` 继续只看 `suc` 判断 `success/failed`。这样 skip 后 `suc=True`，上层工作流继续走；同时 Cloud 的 console/return info 能看到 `suc_type=user_bypass_error`。

## 与前端现有实现的对应

前端可保留：

- `useDeviceExceptionDialog.tsx`
- `DeviceExceptionDrawer.tsx`
- `useSubmitExceptionDecision.ts`
- `services/edge/exception.ts`
- `types/device-exception.ts`
- `exception-dialog-bus.ts`

需要后端适配：

- `job_error_decision_required` -> 创建 `biz_type=device_exception` 通知。
- `POST /api/v1/edge/exception/decision` -> 生成 `job_error_decision` 并发回对应 Edge。
- 已决策的通知写入 `user_decision`，这样 `DeviceExceptionDrawer` 会禁用按钮并显示已处理。

需要前端小改或确认：

- `suggested_actions` 已支持任意 `action/label/description`，自定义“关机”等按钮无需改。
- 如果要显示 `job_id`、`traceback`、`device_snapshot`，现有 Drawer 已支持。
- 如果要在 workflow graph 上标橙色等待状态，后端可在等待用户决策时额外推 `job_status=waiting_confirm`；Uni-Lab-Cloud `test` 分支已有 `waiting_confirm` 样式。

## 同步强杀结论

- 不建议用 `PyThreadState_SetAsyncExc` 强杀同步线程。它可能把锁、串口、PLC/设备连接、物料状态更新留在不可预期状态。
- 同步 action 的 timeout 第一版只做“动作结果层面的超时决策”：如果线程可取消则 best-effort cancel；已经运行的线程不能安全杀掉。
- 真要硬杀卡死同步动作，应放到独立 worker process，超时 terminate process 并重建设备连接。这个是后续架构改造，不放进第一版。

## 实施步骤

1. 新增 `ErrorPolicy` / `ErrorPolicyOption` TypedDict，并从 `unilabos.registry.decorators` re-export，方便用户 `from unilabos.registry.decorators import ErrorPolicy`。
2. `@action(...)` 新增 `execution_timeout`、`error_policy` 参数，并只在非空时写入 `_action_registry_meta`。
3. `ast_registry_scanner.py` 支持解析 dict/list/literal 形式的 `error_policy` 和 `execution_timeout`，提升 AST cache version。
4. `registry.py` 把 action meta 透传到 `action_value_mappings`；YAML action entry 也允许同名字段。
5. `ws_client.py` 新增：
   - `publish_job_error_decision_required(...)`
   - decision pending registry，按 `job_id/task_id/notify_uuid` 等待回传
   - `MessageProcessor` 处理 `job_error_decision`
6. `base_device_node.py` 在 `_create_execute_callback` 中接入 decision loop；保留原同步/异步执行方式，先抽 helper 降低改动风险。
7. `type_check.py` 扩展 result info 序列化，支持 `suc_type`。
8. `host_node.py` 保持按 `suc` 决定最终 status；只需确保扩展字段不会被 pop 或丢失。
9. 后端接入两个协议；前端优先不改，仅验证现有 `device_exception` 能展示自定义 options。

## 验证计划

- 单元测试：
  - `@action(error_policy=...)` metadata 能进入 AST scanner 和 registry。
  - 默认 policy 生成 retry/skip 两个 option。
  - `allow_skip=False` 不生成 skip option；`allow_retry=False` 不生成 retry option。
  - 自定义 option label 为“关机”，回传 action 后能映射到 fallback function。
- exec callback 行为测试：
  - 同步 action 抛错 -> 上行 `job_error_decision_required`。
  - 异步 action 抛错 -> traceback 完整上行。
  - 用户选择 retry -> Edge 自己重新执行 action。
  - 用户选择 skip -> `return_info.suc=True` 且 `suc_type=user_bypass_error`，`return_value.bypassed_error` 保留原始错误。
  - 用户选择 custom fallback -> 执行 fallback；fallback 报错时最终 failed，错误信息包含原始 traceback 和 fallback traceback。
  - timeout -> 作为 `category=timeout` 的异常进入同一 decision flow。
- 联调验证：
  - 后端收到 `job_error_decision_required` 后能创建 `device_exception` 通知。
  - 前端弹窗展示 retry/skip/custom label。
  - 前端点击后 `POST /api/v1/edge/exception/decision`，后端能回推 `job_error_decision` 给对应 Edge。
