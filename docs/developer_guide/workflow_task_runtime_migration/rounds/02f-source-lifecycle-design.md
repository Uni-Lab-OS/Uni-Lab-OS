# Round 02F：package source declaration、启动恢复与 watcher 设计

日期：2026-08-01

实现分支：`migration/02f-source-lifecycle`

基线：`4875cc1`

对应计划：`02-authoring-schema-plan.md` 的 **02F — package source declaration、启动恢复和 watcher**。

## 1. 本轮目标

02F 只补齐 editable package 到既有持久 Authoring lifecycle 的 production adapter：

```text
package.yaml
  -> closed declaration parser
  -> existing WorkflowService.register_editable_source(...)
  -> startup reconcile
  -> WorkflowSourceMonitor
```

02F 不实现 compiler/Catalog 的 production composition；该责任仍属于 02G。既有
`WorkflowService` 继续独占文件读取、hash、compile、Draft/Candidate 持久化和
`workflow.authoring.changed` 事件，discovery 与 watcher 不复制这些规则。

## 2. package.yaml 合同

一个显式选择的 editable package workspace 根包含：

```yaml
package:
  name: szlab_poly_studio

workflows:
  - workflow_uuid: 8feecdda-3898-4afc-9735-4f1ac59553fd
    source: szlab_poly_studio/workflows/magnetic_stirring.py
```

closed shape：

- 顶层只能有 `package`、`workflows`；两者必填；
- `package` 只能有非空 `name`，名称必须是单个 Python package identifier；
- `workflows` 是非空数组；每项只能有 `workflow_uuid`、`source`；
- `workflow_uuid` 必须是规范 UUID；同一 manifest 中不得重复；
- `source` 必须恰好是
  `<package.name>/workflows/<filename>.py`，不得为绝对路径、嵌套 workflow 目录、
  `.`/`..`、反斜线或另一 package；同一 manifest 中不得重复；
- 不扫描任意 `.py`，不从 Python 内容猜 Workflow UUID，不接受 codec-only declaration；
- YAML 必须是 UTF-8、单文档、普通 closed map/list/scalar；未知字段、tag、alias、
  duplicate mapping key 或递归结构 fail closed 为稳定 declaration error。

manifest 根和 `<package.name>` source root 都必须是非 symlink directory。声明的
canonical source 可以缺失，以便保留 `draft_missing` 及随后 `recovered` 生命周期；
但存在时必须是 containment 内、非 symlink 的 regular UTF-8 file。读取验证只用于
注册前拒绝错误 package，实际 Draft 内容仍由 `WorkflowService` 在锁内重读。

## 3. 深模块 Interface

新增 `unilabos/workflow/source_discovery.py`，只暴露小 Interface：

```python
load_editable_package_manifest(package_root) -> EditablePackageManifest
register_editable_package_sources(service, package_root) -> tuple[registration, ...]
```

内部 immutable declaration 保存：

```text
package_id
package_source_root
workflow_uuid
relative_path = workflows/<filename>.py
```

logical `source_uri` 仍由既有 Service 产生：
`package://<package_id>/workflows/<filename>.py`。调用方不能传 URI 或任意绝对
source path。

所有 manifest 和路径检查先于任何注册调用；adapter 随后只调用既有
`register_editable_source(...)`，不直写 Store。声明只绑定已经持久存在的 Workflow；
不得为修复坏 UUID/缺失 Workflow 而暗中创建 Workflow 或改写 Graph。

## 4. composition 生命周期

`compose_workflow_runtime(...)` 增加显式 keyword-only
`editable_package_roots`：

1. 取得 workspace lease 并创建 Service；
2. 按传入顺序加载每个显式 package manifest 并注册全部声明；
3. 对 Store 中全部注册 source 执行既有 startup reconciliation；
4. reconciliation 完成后启动唯一 `WorkflowSourceMonitor`；
5. 任一步失败都停止 monitor、关闭 Service/Store、释放 lease，不能留下半启动
   Authority；
6. reset/close 必须先确认 monitor 已停止，再关闭 Store 和释放 lease。

同一 process 已完成 composition 后，传入不同 working directory、compiler 或 package
root 集合都不能热改当前 Authority。相同配置的幂等调用返回同一 Service。

本轮 watcher 继续使用既有 polling adapter：signature 必须在 settle window 内稳定才
调用一次 `reconcile_registered_source(workflow_uuid)`；同 hash OS write 由 Service
去重。删除/rename 只使固定 canonical path 进入 missing，不跟随新路径、不删除 Applied
Workflow；路径恢复由 Service 发 `cause=recovered`。

## 5. 错误与隔离

- manifest/schema/path/package 错误：`SourceDeclarationError`，携带稳定 machine code
  和不泄漏任意外部文件内容的说明；
- Workflow 不存在、跨 package/path identity 冲突：保留既有 `WorkflowError`；
- 单个已注册 Draft 在 startup compile 失败：沿用既有 per-source recovery isolation，
  不能阻止其他 source 被监视；
- manifest 本身不可信：composition fail closed，不能静默跳过一部分 declaration；
- monitor 的单 source 暂态失败继续有界退避，不能杀死 monitor thread。

## 6. 测试验收矩阵

唯一独立测试作者先提交 RED tests，至少覆盖：

1. closed manifest happy path 与精确 Service registration 参数；
2. unknown/missing/wrong-type/empty/duplicate UUID/duplicate path；
3. YAML duplicate key、alias/tag、多文档、非法 UTF-8；
4. absolute/traversal/backslash/nested/wrong-package/non-Python source；
5. package root/source root/source file symlink、目录、FIFO 和 containment race；
6. missing source 可注册，首次 startup 为 `draft_missing`，恢复 canonical path 为
   `recovered`；
7. startup 顺序是 register -> reconcile -> monitor，monitor 不观察半注册集合；
8. package discovery 失败时 composition 清理 Service、monitor、Store 和 lease；
9. watcher debounce/coalesce、same signature/hash、delete/rename/restore；
10. 两个显式 package root 的 deterministic registration，以及重复 identity fail closed；
11. 不扫描未声明 `.py`，不创建 Workflow，不调用 Store 私有 API；
12. 原 Phase 01 lifecycle、shutdown、TOCTOU 与 durable event tests 保持全绿。

## 7. 停止线

本轮不得：

- 接入 02D engine/Catalog 到真实 server；
- 修改 Draft PUT/Apply wire contract；
- 增加 browser filesystem access；
- 从文件名、decorator 或目录扫描猜 Workflow；
- 跟随 rename 或 symlink；
- 创建/删除 Applied Workflow；
- 修改 Task/Job、Scheduler/device、Frontend 或 Backend。

## 8. 合并门

本轮使用一名 test subagent 和一名 review subagent，顺序执行。目标、Phase 累积、完整
测试、Ruff、format 与 `git diff --check` 全绿，review blocking 为 0 后，才允许非
squash 本地合入 `integration/workflow-task-runtime`；不 push。
