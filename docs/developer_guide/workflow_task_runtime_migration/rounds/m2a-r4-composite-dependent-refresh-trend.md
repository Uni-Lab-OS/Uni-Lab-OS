# M2A-R4 复合工作流 Catalog 依赖刷新修复记录

日期：2026-08-05  
分支：`migration/m2a-r4-composite-dependent-refresh`  
基线：`integration/workflow-task-runtime@ce71cc267009bf61a07f836d1a48bfc85b1c4e97`  
精确行为候选：`c203efe8340eed5895ed8728a66c3884ad842834`

状态：**独立 RED、三项审查 finding 回归、SZLab 生产包创作回归和完整仓库门禁均已转绿；同一独立审查者已对精确行为候选给出 `ACCEPT`。**

## 1. 结果与范围

本轮修复已发布工作流（Published Workflow）目录变化后的创作状态刷新：子工作流（Workflow）Apply、更新、删除或图保存完成目录发布后，其他已注册源码会在 Catalog guard 和当前工作流创作锁释放后重新判断其候选、诊断与 Applied Source 是否仍对应当前模板 Catalog。

修复覆盖三条合同：

1. 原本因 `composite_child_unapplied` 失败的父 Draft，在子工作流 Apply 后无需再次保存源码即可重新编译；进程重启也会恢复相同语义。
2. 已应用父工作流不能凭旧 Catalog 指纹继续证明组合边界有效；子工作流破坏性升级后，父 Draft 会自动产生当前诊断。
3. 目录发布后的依赖刷新属于 post-commit 派生工作；注册源枚举或单个刷新失败只写异常日志，不能把已经提交并发布的 Apply 返回成失败。

本轮没有修改 HTTP API、JSON wire 字段、前端（FE）、SZLab 源码、PLC 通信或执行器动作。

## 2. 刷新与并发语义

- 当前工作流持久变更与完整目录发布仍在 Catalog guard 内完成。
- 其他已注册源码只在当前工作流创作锁和 Catalog guard 都释放后刷新，避免 `Catalog -> Store` 与父工作流锁交叉持有。
- 源码 hash 未变时，候选和 Applied Source 的 `template_catalog_fingerprint` 必须匹配当前 Catalog；诊断属于可重建派生状态，也会重新编译。
- 父源码若在目录发布与父锁获取之间发生外部编辑，事件原因由锁内实际源码状态决定为 `external_draft_changed`，不会被刷新调用方覆盖为 `draft_compiled`。
- 注册源快照枚举、记录解码或单个父 Draft 编译失败均被隔离在 post-commit 边界内；已提交工作流修订和已发布 Catalog 仍作为成功事实返回。

## 3. 独立测试与提交 provenance

本轮始终只使用一个独立测试作者 `/root/composite_refresh_test_author`。测试在独立 worktree 和 `test/m2a-r4-composite-dependent-refresh-red` 分支先形成 RED，再以非 squash 提交进入实现分支；没有删除、弱化、skip 或 xfail 独立断言。

| 阶段 | 独立分支提交 | 实现分支提交 | 结果 |
|---|---|---|---|
| 子 Apply 后即时刷新 RED | `d8bfeff3fec0d87573f03a213e8b367d2273d395` | `23807b1b` | 父 Draft 保留 `composite_child_unapplied` |
| 重启恢复刷新 RED | `59304c834cf2d42a5cdf8ce6f3bdd6e81d67e744` | `495432b7` | 相同 hash 早退，旧诊断跨重启保留 |
| 首个生产修复 | — | `02a5588e76c78e9b88b185e64e44d5a0fbd79a9f` | 原始两项合同转绿 |
| reviewer 三项反例 RED | `1083c382780d589e449f161e86907469a66b16ca` | `af587ffc` | 事件误标、已应用父工作流未失效、枚举异常穿透 Apply |
| 独立测试 import 整理 | `6be3475c539b76f3be9891dc34a72a826d6444d0` | `a4a0f93c` | tests-only，未 amend |
| logger 确定性观测 seam | `34c7f619ca157d6c35f8e75a73f9ab72c6e1e07c` | `23042212` | 直接监视 `_LOGGER.exception`，不依赖运行时根 handler |
| 三项 finding 修复 | — | `c203efe8340eed5895ed8728a66c3884ad842834` | 所有独立合同与完整门禁 GREEN |

首轮独立 reviewer `/root/composite_refresh_reviewer` 固定审查 `02a5588e76c78e9b88b185e64e44d5a0fbd79a9f`，给出三个 blocking finding：

1. 强制 `draft_compiled` 会误标并发外部 Draft 编辑；
2. Applied Source 未比较 Catalog 指纹，子合同破坏性升级后已应用父 Draft 不会失效；
3. `list_registered_sources()` 枚举异常位于隔离边界外，会把已提交 Apply 伪装成失败。

独立测试作者先在精确首个生产候选上证明 `3 failed`，生产修复再关闭全部反例。同一 reviewer 重新检查精确行为候选 `c203efe8340eed5895ed8728a66c3884ad842834` 的完整 diff、测试和锁序，最终结论为 `ACCEPT`，无新增 blocking finding。

## 4. 验证证据

精确行为候选 `c203efe8340eed5895ed8728a66c3884ad842834`：

| 门禁 | 结果 |
|---|---:|
| reviewer 三项合同与 Catalog 生命周期合并集 | `9 passed, 1 warning` |
| 创作源码、Draft CAS、Catalog 锁序和恢复相关回归 | `96 passed, 1 warning` |
| SZLab `test_material_transfer_applies_before_single_sample_composite` | `1 passed` |
| 完整 `pytest -q -rs tests` | `2611 passed, 7 skipped, 68 warnings` |
| changed-file Ruff `E/F/I` | passed |
| changed-file Ruff format | passed |
| changed Python `py_compile` | passed |
| `git diff --check` | passed |

7 个 skip 是三个显式联网慢测试、一个需外部 Phoenix executable 的集成测试，以及三个仅在真实 Windows 文件共享环境运行的 Draft CAS 测试。本轮没有新增 skip 或 waiver。

真实 SZLab 创作链路也确认父 Draft 在子工作流 Apply 后无需重新保存即可从 `composite_child_unapplied` 前进到当前 Catalog 下的独立 `material_source_conflict`。隔离运行时库存数据库的 `site` 表为 0 行；因此下一层阻断属于 SZLab 部署的 Site 事实投影，不属于本轮 Uni-Lab-OS 刷新缺陷。本轮没有把该独立数据问题包装成工作流执行成功。

## 5. 文件规模复核

| 文件 | 行数 | 职责与处理决定 |
|---|---:|---|
| `unilabos/workflow/service.py` | 3426 | 基线已为 3187 行；本轮暂时保持发布、锁序和 post-commit 返回语义在同一服务边界。 |
| `tests/workflow/test_c1_catalog_publication_lifecycle.py` | 471 | 即时与重启生命周期合同，低于 500 行。 |
| `tests/workflow/test_m2a_composite_dependent_refresh_review.py` | 428 | 三项 reviewer 反例独立成文件，低于 500 行。 |
| `unilabos/workflow/graph_validation.py` | 1541 | 来自已合并 M2A-R3，本轮未修改；拆分计划见 R3 记录。 |

`service.py` 超过 800 行但本轮不在安全修复中立即拆分，具体理由是：SQLite Apply、Catalog publication、工作流创作锁释放、依赖刷新和失败降级共同决定一次 mutation 对调用方可见的原子边界；在修复期间抽走单个 helper 会暴露 Store、编译器、Catalog 指纹与锁对象，扩大接口并增加锁序回归风险。

后续拆分按以下可执行顺序进行：

1. 新建私有 deep module `unilabos.workflow.authoring_refresh`；
2. 先迁移本轮并发、重启和 post-commit 失败测试，固定现有可观察语义；
3. 由 `WorkflowService` 只提供源码快照、当前 Catalog 指纹、编译并记录三个窄端口，不向新模块暴露原始 Store 或锁；
4. 新模块只暴露 `refresh_after_catalog_mutation(mutated_workflow_uuid)` 一个入口，负责注册源快照、失效判断和逐项异常隔离；
5. `WorkflowService` 保留 Catalog guard 与当前工作流事务排序，删除迁出的刷新策略后重新跑完整迁移门禁。

## 6. 发布边界

- 用户已明确授权修复、测试、提交并推送远端。
- 本轮以保留测试 provenance 的非 squash 历史合并到 `integration/workflow-task-runtime`。
- 只推送 Uni-Lab-OS 的目标 integration 分支；不提交或推送 SZLab 工作树中既有的 `s04_robot_stirring.py` 本地改动。
- 任何后续生产代码变化都会使 `c203efe8` 的独立审查确认失效。
