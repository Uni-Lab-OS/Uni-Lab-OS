# 设备目录与手动解锁 no-local-bridge 实现记录

日期：2026-08-02

状态：**IMPLEMENTATION CANDIDATE / REVIEW PENDING**

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
   jobs 快照隔离；`manual_unlock_fences` 在临界区同步封住迟到 success/failed。
4. `WebSocketClient.force_unlock_action()` 先建立 cancelled terminal fence，再对隔离快照发
   best-effort `HostNode.cancel_goal()`；CAS 后新 holder 不被清理，锁上报继续公开新 holder。
5. 当前 `unilabos.app.web.api` 直接提供 Backend-envelope `GET /api/v1/devices` 与
   `POST /api/v1/devices/{device_id}/actions/{action_name}/commands`；两者 loopback-only、
   unknown profile deny-by-default，错误不泄漏 FastAPI `detail`。

## 4. 实现者补充回归

`tests/app/test_device_manual_unlock_safety.py` 增加：

- schema required/default 与 free/null holder；
- synchronous/late success 不覆盖 manual cancelled；
- report_action_lock 带完整 holder；
- IPv6 loopback 与 invalid command structured 422；
- route 只取 cached live client，不调用 constructor。

## 5. pre-review gate

| Gate | 结果 |
|---|---|
| 独立 RED + 实现者安全测试 | `16 passed` |
| `pytest tests/app -q --tb=short` | `332 passed` |
| `pytest tests/ -q --tb=short` | `2334 passed, 4 skipped` |
| changed-files Ruff `E/F/I`，忽略既有 `E501` | passed |
| 新增 Python 文件 Ruff format | passed |
| `git diff --check` | passed |

完整测试的 4 个 skip 与 warning 均为既有硬件/collection/deprecation 情况，没有失败。
两个大型既有模块 `web/api.py` 与 `ws_client.py` 在基线即未通过全文件 Ruff format；本轮没有
把功能迁移扩大为全文件机械格式化，新增文件与新增 hunk 遵循当前 formatter。

## 6. 尚未完成的 gate

- 本文件所在 exact candidate commit 的唯一独立 reviewer 尚未执行。
- 当前 FE service 尚需把 `GET /api/v1/devices` 与 command 的 Backend envelope 解包，并接受
  `unlocked` success status；原 UI 组件不改。
- 旧 E2E spec 仍以 `/api/v1/runtime/runs` 准备 holder，不能复用；必须从 current runtime seam
  重写 setup + page ledger，重新产出至少 5 张截图。
- review、FE adapter、真实 E2E、矩阵/Issue 更新与 Core pin 完成前，不得合入 integration 或
  把 Core #160 标为 testing/accepted。
