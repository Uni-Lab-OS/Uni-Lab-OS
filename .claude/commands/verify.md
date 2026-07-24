---
description: 跑本仓完整验证 loop（import + pytest + hermetic/property + ruff + 验收对齐），对齐 docs/agent-workflow.md 完成标准
argument-hint: [FXXX-name]
---

对需求 `$ARGUMENTS` 跑完整验证。这是 `docs/agent-workflow.md`「完成标准」的一键版。**任何一项 FAIL，不得宣布完成。**

> 验证铁律：本条消息里没亲自跑过的命令，不得声称它通过。禁止「应该过了 / 大概没问题 / Done!」——先跑、读输出、再下结论。详见主仓 `product_designs/team_collaboration/02-agent-execution-protocol.md`。

## 1. 机器门禁（必须全绿，逐条贴 PASS/FAIL）

```
python -c "import unilabos"        # 包可导入（底线，任何改动都要过）
pytest tests/<相关领域>            # 相关测试通过（贴出 N passed / N failed）
ruff check <改动路径>              # 若已引入 ruff，无新增错误
```

FAIL 的贴出关键报错，不要只写「失败」。

## 2. 正确性红线（OS 特有，对照 docs/agent-workflow.md §五）

- [ ] 设备驱动测试用 **fake / mock transport**，未连真实 OPC-UA / Modbus / RS485 / 串口？
- [ ] 涉及超时 / 调度 / 重试的测试注入了**可控时钟**，无 `time.sleep` 真实等待？
- [ ] 协议编译 / 坐标变换 / 调度：有 **property-based（Hypothesis）不变量**（往返一致 / 旋转叠加 / 无资源冲突），而非只举几个例子？
- [ ] 无 flaky：断言不依赖随机 / 并发 / DDS 时序？

## 3. 验收对齐

`cat docs/features/$ARGUMENTS/requirement.md`，逐条对验收标准打勾。**没有对齐验收标准 = 未完成**，无论代码多漂亮。

## 4. 若涉及协议 / 设备 registry 契约变更

被 cloud / backend / 设备图消费的契约（action 签名、协议 YAML schema、registry 类型路径）有改动时，调 `contract-guardian` 子 agent 评审，确认结论为 PASS，或人类已处理 NEEDS-HUMAN。

## 5. 状态真相

- [ ] `feature-list.json` 中已完成任务 status = "completed"，无残留 "in_progress"
- [ ] `progress.md` 记录了完成情况、遇到的问题、下一步

## 输出

给出总结论：**READY-TO-MERGE** / **NOT-READY**（列出所有 FAIL 项 + 证据）。
