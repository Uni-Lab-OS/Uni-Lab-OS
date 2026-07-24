---
description: 起一个新需求目录或认领已有需求，按 docs/agent-workflow.md Session 启动协议开工
argument-hint: [FXXX-name | 新功能描述]
---

要么**起新需求**，要么**认领并继续已有需求**。参数：`$ARGUMENTS`

## 判断分支
- `FXXX-name` 且 `docs/features/$ARGUMENTS/` 已存在 → 认领已有。
- 否则视为新功能描述 → 起新需求。

## A. 起新需求
1. 编号：`ls docs/features/` 看最大 FXXX，新号 +1。跨端功能沿用 product_designs 里的同号。
2. `cp -r docs/templates docs/features/FXXX-<kebab-name>`
3. 和人类一起写清 `requirement.md`（用户故事 + 可机器判定的验收标准）与 `interface-design.md`。requirement.md 人类定稿。
4. 若涉及协议/设备契约会被 backend/frontend 消费：提醒先在 `product_designs/<domain>/` 冻结契约。
5. 停在这里等人类确认，不要直接写代码。

## B. 认领已有需求（严格按 docs/agent-workflow.md §二）
1. `pwd`（确认在 Uni-Lab-OS 根目录）
2. `git log --oneline -10`
3. `cat docs/features/$ARGUMENTS/progress.md`
4. `cat docs/features/$ARGUMENTS/feature-list.json`
5. 选下一个 `pending` 子任务（按 id，单任务原则）
6. `cat docs/features/$ARGUMENTS/requirement.md`
7. `cat docs/features/$ARGUMENTS/interface-design.md`
8. `python -c "import unilabos"`（确认可导入）

然后进入 Build-Verify Loop：Planning → Build → Verify（import + pytest + 不变量）→ Fix → Commit。

## 纪律提醒
- 每次只做一个子任务。
- 硬件/协议/调度逻辑：写 hermetic 测试（fake 硬件 + 可控时钟），数学逻辑上 Hypothesis。
- 代码改动交 `os-reviewer` 评审。
- commit 前更新 feature-list.json status 和 progress.md。
