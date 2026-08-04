# Round 02G2：Windows Draft CAS 趋势记录

状态：**实现与完整正式测试已通过，等待精确候选 SHA 的独立复审。**

基线：`5f111ffb`（`integration/workflow-task-runtime`）

分支：`migration/02g2-windows-draft-cas`

精确测试 SHA、测试命令结果与 reviewer disposition 记录在候选提交的 Git note；
提交内文件无法安全地自引用自身 SHA，因此本文件记录稳定设计与过程证据，Git note
作为本轮可追加迁移 ledger。

## 1. 问题与边界

`92aa3d50` 已允许 Windows 发现、读取和 Apply editable package，但
`WorkflowService._atomic_write()` 在缺少 POSIX `dir_fd` 或 Linux file lease 时
直接返回 `draft_hash_conflict`。因此 FE 在 Windows 可以打开 Workflow，却不能完成
“保存 Draft → 接受规范化源码 → Apply”。

本轮只补 Windows 文件 CAS，不修改 PLC、ROS、执行器、Candidate 或 SQLite Apply
语义。冻结不变量如下：

1. 保存仍同时校验调用方观察到的 Draft hash 与 Workflow revision；
2. 外部 coding-agent、Git 或编辑器的字节不得被静默覆盖；
3. Draft 源码仍只有 registered package 文件这一份权威；
4. Apply 仍只提交已物化 Candidate，不在事务中写回 Draft。

## 2. 独立 RED 测试

唯一 test-author：`/root/windows_cas_test_author`（Ptolemy）。

独立测试分支：`test/02g2-windows-draft-cas-red`。

原始测试提交：`815fdeda76f38e4c2d379875a20a3737096d3c8a`；以非 squash
提交 `4727e6c8` 带入本分支。

RED 命令：

```bash
/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q \
  tests/workflow/test_02g2_windows_draft_cas_contract.py
```

结果：`3 failed, 1 warning`。三项失败分别证明匹配 CAS 无法保存、代码没有进入
Windows 锁区、首轮保存失败导致规范化 Draft 无法 Apply。

测试通过真实 `WorkflowService.save_draft()`、`apply_authoring()` 和 `get_graph()`
Interface 验证：

- 匹配 Draft hash/revision 时安全落盘并返回 Candidate；
- 锁区内外部改写时返回 `draft_hash_conflict` 且保留外部字节；
- 保存规范化 Draft 后，Applied Graph、revision 与 Applied Source hash 一致。

实现阶段另补“最终复核后、原子替换前”的 gap 竞争回归，证明替换 backup 能识别
并恢复外部胜者，且不遗留本次 `.tmp`/`.cas` 文件。

## 3. 实现结论

Windows 路径使用独立深模块 `windows_draft_cas.py`：

1. 在 registered root 内校验/创建父目录，拒绝符号链接和目录身份变化；
2. 在同目录写入独占临时文件，flush 后 `fsync`；
3. 使用 `msvcrt.locking(LK_NBLCK)` 锁住 `8 MiB + 1` 字节，并在锁内重读、重验
   Draft hash 与文件身份；Microsoft CRT 明确允许锁到 EOF 之后；
4. 关闭 Windows CRT handle 后重新核验一次，再调用 Win32 `ReplaceFileW`，同时
   取得替换瞬间原文件的 backup；
5. backup hash 匹配才接受发布；若 gap 内有外部胜者，则把它恢复到 canonical 并
   返回稳定 `draft_hash_conflict`；无法证明 artifact 归属时保留而不覆盖。

`service.py` 只负责平台分派和领域错误映射，POSIX `dir_fd + file lease` 路径保持
不变。

## 4. 验证趋势

| 门禁 | 结果 |
| --- | --- |
| Windows Round 目标及原 Windows 回归 | `76 passed, 1 warning` |
| 完整 `tests/workflow` | `1560 passed, 13 warnings` |
| 完整正式 `tests/` | `2588 passed, 4 skipped, 68 warnings` |
| 变更 Python 文件 Ruff `E,F,I` | passed |
| 变更 Python 文件 `ruff format --check` | passed |
| `compileall` 与 `git diff --check` | passed |

仓库根目录裸跑 `pytest -q` 在收集正式 `tests/` 之前被两个既有硬件示例阻断：

- `unilabos/device_comms/modbus_plc/test/node_test.py` 构造旧 `Coil` 时缺少
  `data_type`；
- `unilabos/devices/cameraSII/cameraUSB_test.py` 导入不存在的顶层
  `cameraUSB`。

两项都不经过 Workflow 模块，也未被本轮修改；正式仓库测试目录 `tests/` 已完整
通过。

## 5. 文件规模与模块边界

- 新 `windows_draft_cas.py`：408 行，保持在 500 行预算内；
- 新/扩展 02G2 合同测试：414 行，保持在 500 行预算内；
- `service.py`：3068 行，属于既有超大应用服务。本轮没有继续内联 Windows 算法，
  而是只增加平台分派并把完整 CAS 边界抽到新深模块；拆分整个 Service 会扩大本轮
  迁移范围并触及大量无关 Workflow/Task Interface；
- `test_authoring_source_discovery.py`：841 行。本轮仅把一条过时的 Windows
  fail-closed 断言更新为保存能力合同；保留在原文件可继续复用 package discovery、
  composition 与 lease fixture，强行搬移会复制跨模块 fixture 并削弱该集成链路。

因此两个既有超大文件都采用“最小接线/最小契约更新”，新增复杂度全部留在预算内
的专用模块与合同测试中。
