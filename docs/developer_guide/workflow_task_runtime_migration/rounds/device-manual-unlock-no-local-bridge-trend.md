# 设备目录与手动解锁 no-local-bridge 实现记录

日期：2026-08-02

状态：**LOCAL IMPLEMENTATION REVIEWED / PUBLICATION PENDING**

## 1. 基线与范围

- integration 基线：`8fad069c16faeb991fade5232eaf84ef32b17146`。
- 实现分支：`migration/device-manual-unlock-no-local-bridge`。
- 跨仓决策：Uni-Lab-OS/Uni-Lab-Core#160。
- owning ticket：Uni-Lab-OS/Uni-Lab-OS#13。
- 本轮只恢复 current FastAPI composition root 上的 live device catalog 与 CAS manual unlock；
  不恢复 local bridge、`/api/v1/runtime/runs`、第二个 communication client/manager 或旧 Run
  identity。

## 2. 独立 RED provenance

- 唯一独立 test-author：`device_unlock_red`。
- test branch：`test/device-manual-unlock-integration-red`。
- 原始 test commit：`6d9dbd8c7e60f25330f16559a1da36e7ea0bec92`。
- implementation branch 上保留 provenance 的 cherry-pick：`20371b5`。
- RED 命令：

  ```bash
  /home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest \
    tests/app/test_device_manual_unlock_integration.py -q --tb=short
  ```

- RED 结果：`10 failed, 1 passed`。10 项分别因缺失 atomic holder CAS、live holder
  projection、public command、409/幂等/安全门而失败；唯一通过项证明当前 composition 没有
  local bridge endpoint 或 `/api/v1/runtime/runs`。

test-author 没有修改 production，也没有查看旧 `760a53d...` candidate 的实现或测试。

## 3. 实现内容

1. 新增纯 `unilabos.app.device_catalog` projection，将 live HostNode Action schema/default、
   online 与唯一 DeviceActionManager 的 busy/full holder 合并为 `device-catalog/v1`。
2. `CommunicationClientFactory.current_client()` 只读取已组合的 cached live client，API 不会
   因 request 隐式创建第二个 `WebSocketClient`。
3. `DeviceActionManager.force_unlock()` 在同一 `RLock` 内完成 holder CAS 与 active/queue/all
   jobs 快照隔离，并为旧 Job 建立不按时间/数量淘汰的 manual fence。
4. 初次 dispatch 和 queue promotion 均通过 `dispatch_if_current()` 与 `force_unlock()` 共用同一
   临界区；解锁先完成时不得再调用 `HostNode.send_goal()`，下发先发生时解锁随后发起物理取消。
5. Action 锁状态读取与 `report_action_lock` 入队同临界区；调用方传入的 stale free/busy 只作
   翻转提示，最终上报始终重新读取 live holder，避免 `busy(new) → free(old)`。
6. `WebSocketClient.force_unlock_action()` 逐 Job 隔离 `HostNode.cancel_goal()` 异常；一个 driver
   cancel 失败不产生 500，也不跳过同一旧快照中的其余 Job。
7. 当前 `unilabos.app.web.api` 直接提供 Backend-envelope `GET /api/v1/devices` 与
   `POST /api/v1/devices/{device_id}/actions/{action_name}/commands`；两者 loopback-only、
   unknown profile deny-by-default。command 手动解析 JSON，missing/malformed/extra field 不泄漏
   FastAPI `detail`；HostNode 或 Action 缺失 fail closed；`setup_api_routes()` 重复调用只挂载一次。
8. 普通 Job 保留原有 `failed → success` 终态纠正；只有 manual fence 阻止迟到 success/failed
   覆盖人工 `cancelled`，本轮没有扩大 Workflow Job 终态语义。
9. HostNode 在 trace side-channel 或 Action Server 等待开始前登记 pending goal；人工解锁先取得
   goal-tracking lock 时完全丢弃未提交 goal，ROS submit 先取得锁时解锁等待提交完成并登记
   accept 后立即取消。检查与 `send_goal_async()` 是同一线性化临界区。
10. HostNode 晚到 feedback/result 先查询 manual fence；旧 HTTP `job_result_store` 的写入与
    fence 共用 manager lock。结果先写时解锁随后清仓，解锁先完成时迟到写入被拒绝，两个顺序
    最终都不会暴露伪 `SUCCEEDED`。

## 4. 首轮独立 review 与修复

唯一独立 reviewer 对首轮 exact SHA
`4c7381259189e9a89c71ab2a5b64e70e681eb5c1` 给出 7 个 blocker：

- pending-start 与 initial dispatch handoff 可在解锁后继续 `send_goal`；
- 新 holder 的 busy 上报可被旧 free 覆盖；
- HostNode 未装配时伪造 `already_unlocked`；
- physical cancel 首项异常会跳过其余快照；
- missing/malformed JSON 泄漏 FastAPI `detail`；
- 重复 composition 复制危险 command route；
- 普通 `failed → success` 被越界禁止。

实现者先把以上 7 类问题补为 8 项回归，确认 `8 failed, 5 passed`，再完成修复；target
由首轮 16 项增至 24 项。首轮 reviewer 还指出英文 production docstring、额外字段默许和
24 小时/4096 项 fence 淘汰三个 non-blocker；本轮也分别改为简体中文、`extra="forbid"` 与
永久 fence。

首轮修复候选：`bebc2128fe5ab6fd525e3d7c0f28c35691cced86`。

第二轮 exact-SHA review 又发现两项 blocker：

- `send_goal_async()` 已返回但 goal-response 尚未注册 `_goals` 时，人工解锁找不到物理 goal；
- HostNode 会在 bridge fence 前把迟到 success 写入旧 `job_result_store`。

`0ea0ceb328d707adcbc3ff3e055c8e7467e9e5ca` 增加 pending request/cancel、late
feedback/result fence 和旧结果仓的双向并发保护。第三轮 reviewer 用确定性屏障确认 P1 已关闭，
同时发现取消检查与 `send_goal_async()` 之间仍有一个锁外窗口。最终代码候选
`da64ca2adeb9a0d7e5f89d75d52f45929ff68a7d` 把检查与 ROS submit 合并到同一临界区，
并分别覆盖 unlock-first 零发送与 submit-first 解锁等待两种顺序。

同一独立 reviewer 对 `da64ca2...` 的最终结论为 **0 blocking / 1 non-blocking**：P0
双向屏障确认 unlock-first `send_calls=0`，submit-first 中 unlock 等待、随后登记 pending
cancel，goal accepted 后仅取消一次；P1 双向并发最终旧结果仓均为空。non-blocking 仅指出一条
实现者并发测试用 50 ms wait 观察锁竞争，在极端线程调度下可能假阳性；reviewer 已用显式
attempted-event 屏障独立证明实现正确，建议后续改善测试可观测性，不阻止本候选。

## 5. 修复后 gate

| Gate | 结果 |
|---|---|
| 独立合同 + 实现者竞态/安全测试 | `29 passed` |
| 实现者 ROS trace 传播/回归 | `19 passed` |
| reviewer target + trace 独立复跑 | `54 passed` |
| `pytest tests/app -q` | `345 passed` |
| `pytest tests -q` | `2347 passed, 4 skipped` |
| 新增测试 Ruff + production critical Ruff `E9/F63/F7/F82` | passed |
| `git diff --check` | passed |
| FE `pnpm typecheck` | passed |
| FE `pnpm test` | 6 个 workspace suite 全部 passed |
| FE `pnpm build:web` / `pnpm build:desktop` | passed / passed |
| 真实 OS composition browser E2E | `1 passed`，10 张截图 |

完整 OS 测试的 4 个 skip 与 warning 均为既有硬件/collection/deprecation 情况，没有失败。
两个大型既有模块 `web/api.py` 与 `ws_client.py` 在基线即未通过全文件 Ruff format；本轮没有
把功能迁移扩大为全文件机械格式化，新增文件与新增 hunk 遵循当前 formatter。

## 6. FE 适配与 E2E 证据

- FE service/E2E 候选：`57cb9ada76dc00bddc7e0347cd9488f9647a9900`；基于最终 OS
  代码候选重跑并刷新证据后的 FE HEAD 为
  `dd95d8f90161f504ee060f68a04a639f67b9e0c8`。
- `LaboratoryService` 只增加 Backend envelope 解包并接受 `unlocked`；原
  `DevicePanel.tsx`、设备列表、Action 卡片、参数表单、锁面板和确认框均未修改。
- test-only fixture 导入当前 OS `app.web.server`、真实 `WebSocketClient/DeviceActionManager`，
  仅通过 `__e2e` seam 建立/结束 holder；fixture 路由不进入产品 bundle，也不恢复 Run route。
- 有效 E2E 使用本工作树独占的 FE `4176` 与 OS `18114`，避免误用其他工作树占用的 4173。
- 网络账本记录全部 setup 与 browser traffic；`/api/v1/runtime/runs*`、node-template、前端直连
  Edge WebSocket、console error 与 page error 均为 0。
- 10 张截图覆盖 locked device、列表/header/Action busy、完整 holder、未/已勾选安全确认、
  OS 权威 free、refetch ready 以及新 holder 再占用。证据在 FE
  `docs/evidence/device-manual-unlock/`。

## 7. 尚未完成的 gate

- 最终代码 exact SHA 已由同一独立 reviewer 放行；本记录/矩阵的 docs-only SHA 仍需快速复核。
- OS/FE 候选尚未 push 或合入 integration，Core submodule 尚未 pin；本轮没有 push 授权。
- exact-SHA review、Issue 测试报告和用户判断前，不得把 Core #160 标为 accepted；是否进入
  `stage:testing` 以复核结论和团队可访问证据为准。
