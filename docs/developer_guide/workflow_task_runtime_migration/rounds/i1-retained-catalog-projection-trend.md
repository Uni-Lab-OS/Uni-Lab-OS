# I1：保留 Applied Catalog read projection 轮次记录

日期：2026-08-02

实现分支：`migration/i1-retained-catalog-projection`

integration 基点：`de95b9965ed551ed2ed1581fe2bd3100e1b93672`

状态：**首轮 exact-SHA 独立评审的唯一 blocker 已按独立 RED 修复；完整门禁与同一
reviewer 复核均通过。本文不把整个 I1 或 Core #157 标记为 Accepted。**

## 1. 触发与根因

FE Candidate Workflow I/O 真实 OS E2E 已完成编辑、Validate、Apply、reload 与
graph→Python，随后把 generated canonical Python 回编译时，public
`POST /api/v1/authoring/compile` 返回 `500 internal_error`。

服务端 traceback 为 `Candidate changed retained Catalog projection`。Applied read graph
按 Backend JSON `omitempty` 省略 nullable Catalog 字段；Authoring compiler 却把所有
已引用 template/handle 从当前 snapshot 重新投影，补回 `null`。两者 wire-equivalent，
但 Candidate boundary 正确要求 retained Catalog projection strict 不变，因而互相冲突。

## 2. 本轮交付

- `_candidate_graph()` 对 Applied 已出现的 NodeTemplate 原样保留 read projection。
- 一个 retained NodeTemplate 的全部 HandleTemplate 同样原样保留，包括零 Handle 的
  presentation/group template。
- 只有 Candidate 新引入的 template/handle 才从当前 Catalog snapshot 投影。
- 未放宽 `_validate_catalog_projection()`、retained projection boundary、Catalog
  fingerprint 或 Handle UUID 校验。
- compile 在复用 Applied read projection 前，先用同一
  `_validate_catalog_projection()` 证明它属于当前 snapshot；只有 nullable `null`/省略及
  JSON 数字域表示差异按既有规则 wire-equivalent，`display_name`、`required` 等语义漂移
  必须 `template_catalog_mismatch`。
- FE E2E 的固定点按 DTO 层级收紧：Applied/generated read graph 自身 exact；
  compiled/regenerated Candidate write graph 自身 exact；两层之间逐字段比较完整 I/O、
  全部 Node input binding 与 Node/Edge authoring semantics，不比较数据库 audit 字段。

## 3. 独立 RED 与 provenance

本轮唯一独立 test-author 为 `/root/i1_fe_io_editor_red`。tests-only 工作树为
`/home/changjunhan/Uni-Lab-Core/.worktrees/uni-lab-os-i1-retained-catalog-projection-red`。

- 原始 tests-only commit：`26e2862209cfcda26550c4efbc76465644ba45c2`。
- 实现分支 cherry-pick：`d4b7a76`。
- RED：public Authoring engine compile 把 retained template 的
  `schema/icon/header/footer` 从省略重新补成 `null`；测试 `1 failed`。
- 测试同时要求新引入 analyze template/handle exact 来自 snapshot，防止修复退化为
  忽略当前 Catalog。
- Production GREEN：`c669afc`；零 Handle retained template 回归修复：`b0533ba`。

首轮 reviewer 在 `e3fd85122394a228b3fc19d9896415fd006f6993` 报告
`Standards 0B/0NB; Spec 1B/0NB`：Applied retained Template/Handle 的非 nullable 语义字段
尚未在复用前与当前 snapshot 对照。由同一 test-author 补充 tests-only commit
`c9afc60eba7f77626d80f57a6f96e18272305fd8`，实现分支 cherry-pick 为 `4f31507`：

- `WorkflowNodeTemplate.display_name` 漂移必须拒绝；
- `WorkflowHandleTemplate.required` 漂移必须拒绝；
- nullable read projection 与 retained zero-handle template 继续允许。

独立补充测试先得到 `2 failed, 1 passed`，production fix
`8e7a6eeb61ba5bd461f2fe2bb5c4802c478c7deb` 仅在 compile 入口复用现有 snapshot
校验，随后为 `3 passed`。

没有删除、skip、xfail 或弱化独立测试。

## 4. 门禁证据

当前 production 候选：`8e7a6eeb61ba5bd461f2fe2bb5c4802c478c7deb`。包含首轮 finding
disposition 的 exact-SHA 候选 `8ddf649b9ea56362632aec83a7d669e6748f56be` 已由同一
reviewer 复核为 `Standards 0B/0NB; Spec 0B/0NB`；本文最终状态提交只再做文档一致性
复核。

| 门禁 | 结果 |
|---|---|
| 独立 retained projection RED→GREEN | `1 passed` |
| Authoring/I1/API focused（修复后扩大重跑） | `159 passed` |
| group/parallel fixed-point 回归 | `25 passed` |
| 完整 `tests`（修复前） | `2312 passed, 4 skipped, 68 warnings` |
| 完整 `tests`（修复后） | `2315 passed, 4 skipped, 68 warnings` |
| changed-files Ruff / Ruff format | passed |
| `git diff --check` | passed |
| FE Candidate I/O real OS browser E2E | focused `1 passed`；完整 Authoring suite `6 passed`，原 500 消失，完整 fixed point 通过 |

## 5. 评审与下一入口

同一 exact-SHA Standards/Spec reviewer 已关闭首轮 blocker；本文最终状态提交完成文档
一致性复核后 non-squash merge 到
`integration/workflow-task-runtime` 并推送，再以新的 OS integration SHA完成 FE
Candidate editor 全门禁与评审。
