# 设备目录与手动解锁：no-local-bridge 实现设计

日期：2026-08-02

实现分支：`migration/device-manual-unlock-no-local-bridge`

OS 基线：`integration/workflow-task-runtime@8fad069c16faeb991fade5232eaf84ef32b17146`

跨仓协议权威：[Uni-Lab-Core #160](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/160)

OS owning ticket：[Uni-Lab-OS #13](https://github.com/Uni-Lab-OS/Uni-Lab-OS/issues/13)

状态：**IMPLEMENTATION AUTHORIZED**。用户已要求继续迁移和集成；本轮仍须通过独立
RED、实现、完整回归、exact-SHA 独立 review 和真实 browser E2E 后才能进入 testing。

## 1. 本轮纠偏结论

旧候选 `760a53d14b532228e461e59ffec388e2813df4ae` 与当前 integration 无共同祖先，并依赖已
淘汰的 `unilabos.app.local_bridge`。旧 FE E2E 又通过 `/api/v1/runtime/runs` 创建 holder，
该测试侧请求没有进入 page network ledger。因此旧候选只提供 holder/CAS 语义参考，旧截图
只提供 UI 历史参考；二者都不是本轮合入或接受证据。

当前 production 组合固定为：

```text
同一 Uni-Lab-OS 进程
  FastAPI composition root
    -> GET /api/v1/devices
    -> POST /api/v1/devices/{device_id}/actions/{action_name}/commands
  CommunicationClientFactory cached live WebSocketClient
    -> 唯一 DeviceActionManager
    -> HostNode ROS goal/cancel
```

禁止恢复 local bridge、内部 HTTP proxy、第二个 `WebSocketClient`、第二个
`DeviceActionManager` 或 `/api/v1/runtime/runs`。

## 2. Interface 与 ownership

### 2.1 public read model

`GET /api/v1/devices` 直接从 live `HostNode` 的设备/Action 映射与 live
`DeviceActionManager` 的锁快照投影，使用当前 Backend envelope：

```json
{
  "code": 0,
  "data": {
    "schemaVersion": "device-catalog/v1",
    "source": "edge",
    "generatedAt": 0,
    "items": [
      {
        "id": "robot",
        "deviceKey": "/cell/robot",
        "namespace": "/cell",
        "name": "六轴机械臂",
        "online": true,
        "actions": [
          {
            "id": "move",
            "actionRef": "robot.move",
            "name": "move",
            "typeName": "UniLabJsonCommand",
            "inputSchema": {},
            "outputSchema": {},
            "busy": true,
            "currentJobId": "<完整 holder job UUID>"
          }
        ]
      }
    ]
  }
}
```

`busy` 是 `device_id + action_name` 的运行时投影，不创建设备级第二锁。只有 busy 且 holder
已知时返回完整 `currentJobId`；free 时必须为 `null`。设备目录不是 A1 typed Catalog，不能
取代持久 NodeTemplate identity/fingerprint。

### 2.2 public command

```http
POST /api/v1/devices/{device_id}/actions/{action_name}/commands
```

请求只接受：

```json
{
  "command": "force_unlock",
  "expectedJobId": "<GET 中的完整 holder>",
  "reason": "operator_confirmed_device_safe"
}
```

成功响应使用相同 envelope；`data.status` 为 `unlocked` 或
`already_unlocked`。首次释放还返回 `deviceId`、`actionName`、`releasedJobIds`、
`cancelRequestedJobIds` 与 CAS 后重新读取的 `currentJobId`。holder 已变化返回 HTTP 409 和稳定
`DEVICE_LOCK_CHANGED` error；输入错误返回 422；runtime/HostNode 未装配不得伪造成功。

## 3. CAS 与物理取消语义

唯一 `DeviceActionManager` 在同一 `RLock` 临界区内：

1. 读取 active holder（异常 queue-only 状态以队首作为保守 token）；
2. 无 holder 返回幂等 `already_unlocked`；
3. holder 与 `expectedJobId` 不同，不修改任何状态并返回 `lock_changed`；
4. 相同时一次性从 active、queue 与 all-jobs index 隔离该 Action 当时的完整快照；
5. 释放 manager lock 后，才对隔离快照逐项调用 `HostNode.cancel_goal()`。

必须先隔离再取消，防止快速 ROS cancel callback 通过普通 completion path 提升旧 queue。
CAS 后新到达的 job 属于新一代 holder，不得被快照取消或被 stale free 上报覆盖。响应中的
`cancelRequestedJobIds` 只表示 best-effort 请求已发出，不表示 ROS 已接受，更不表示物理设备
已经停止。

每个被隔离 Job 可上报 `cancelled/manual_force_unlock` 终态，但绝不能上报 success。迟到的
success/failed callback 不得覆盖已缓存的 cancelled 终态。逻辑释放后重新读取 manager：无新
holder 才上报 free；有新 holder 时继续上报 busy + 新 `current_job_id`。

## 4. 安全与组合门

- 两个 public route 当前只允许 loopback client；IPv4/IPv6 loopback 允许，未知、缺失或
  非 loopback address deny-by-default。
- 危险 command 的 loopback 检查发生在调用 runtime 前；拒绝请求不得触碰 manager/HostNode。
- router 通过依赖函数取得 `CommunicationClientFactory` 已缓存的 live client；handler 不得
  调用 constructor。
- `setup_server()` 只挂载一次 router；重复测试/启动组合不得复制 route。
- `HostNode` 尚未就绪时 read 返回明确 unavailable，不得用 offline fixture 假装 live；command
  在没有支持该能力的 live client 时返回结构化 503。
- production import graph 中不得出现 `unilabos.app.local_bridge`。

## 5. 实现切片

1. 建立纯 `device_catalog` 投影，把现有 HostNode schema/default/required 信息转为 FE DTO。
2. 为 `DeviceActionManager` 增加 holder snapshot 与 atomic isolate seam。
3. 为 live `WebSocketClient` 增加 CAS unlock、best-effort cancel、cancelled fencing 与 holder
   lock report。
4. 建立可注入测试依赖的 public router，并挂到当前 FastAPI composition root。
5. 保留 FE 原设备列表、Action 卡、参数表单、锁面板与二次确认；本轮不新建 UI，也不改变
   frontend DTO。

## 6. 验收门

独立 RED 和 OS gate 至少覆盖：

- live catalog 的 online/action schema、busy holder 与 free/null 投影；
- expected holder 匹配、409 mismatch、already-unlocked 幂等；
- active + queue 同临界区隔离、快速取消 callback 不提升旧 queue；
- CAS 后新 holder 不被清理且不被 stale free 覆盖；
- best-effort cancel requested 与物理完成语义分离；
- cancelled 终态不被迟到 success 覆盖；
- loopback IPv4/IPv6 允许、非 loopback/未知拒绝；
- current composition root 直接提供 public route，且 production source 不引入 local bridge、
  internal proxy 或 `/api/v1/runtime/runs`；
- round target、完整 `tests/`、配置的 lint/static checks 与 `git diff --check` 全绿。

真实 FE→OS browser E2E 必须重新生成至少 5 张截图，并记录 **全部** setup traffic 与 page
traffic。holder 只能通过当前 Task/Job/dispatch 路径或明确标注且不暴露给产品前端的 live
runtime fixture seam 建立；账本中 `/api/v1/runtime/runs`、旧 WorkflowTask WebSocket 与前端
直连 Edge 均为 0。浏览器 `console.error`/`pageerror` 为 0，解锁后补读必须显示 free，并证明
同 Action 可被新 holder 再次占用。

## 7. 本轮停止线

- 不实现 D1A 单 Action Task materialization、A1 Catalog、ResourceSlot、Material、Site、
  ChangeSet 或完整 R2/D1。
- 不把 `force_unlock` 当作 Task cancel；不从手动解锁推导 Task success。
- 不改变普通 Workflow/Task/Job/SSE wire，也不向前端暴露 system Workflow source。
- 不在 review/完整 gate/新 E2E 前合入 integration，不在用户明确授权前 push。
