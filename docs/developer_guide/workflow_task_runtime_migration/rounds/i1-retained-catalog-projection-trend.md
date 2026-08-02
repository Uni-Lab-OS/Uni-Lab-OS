# I1：保留 Applied Catalog read projection 轮次记录

日期：2026-08-02

实现分支：`migration/i1-retained-catalog-projection`

integration 基点：`de95b9965ed551ed2ed1581fe2bd3100e1b93672`

状态：**实现与完整门禁已通过，等待 exact-SHA Standards/Spec 独立评审。本文不把
整个 I1 或 Core #157 标记为 Accepted。**

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

没有删除、skip、xfail 或弱化独立测试。

## 4. 门禁证据

当前实现候选：`b0533ba`。

| 门禁 | 结果 |
|---|---|
| 独立 retained projection RED→GREEN | `1 passed` |
| Authoring/I1/API focused | `95 passed` |
| group/parallel fixed-point 回归 | `25 passed` |
| 完整 `tests` | `2312 passed, 4 skipped, 68 warnings` |
| changed-files Ruff / Ruff format | passed |
| `git diff --check` | passed |
| FE Candidate I/O real OS browser E2E | `1 passed`；原 500 消失，完整 fixed point 通过 |

## 5. 评审与下一入口

exact-SHA Standards/Spec reviewer、finding disposition 与最终候选 SHA 将在本轮评审后
补入，并由同一 reviewer 复核最终文档 commit。通过后 non-squash merge 到
`integration/workflow-task-runtime` 并推送，再以新的 OS integration SHA完成 FE
Candidate editor 全门禁与评审。

