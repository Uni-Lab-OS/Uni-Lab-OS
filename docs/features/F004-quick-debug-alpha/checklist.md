# 验证检查清单: Quick Debug Alpha（W1–W2）

- [x] `python -c "import unilabos"` 通过
- [x] registry/workflow/scheduler/runtime/pTLC 相关 pytest 通过
- [x] fake transport + fake clock；无真实硬件、无真实 sleep
- [x] 锁/编译/时间线 Hypothesis 不变量通过
- [x] AC-1～AC-7 逐条有自动化证据
- [x] 独立 test agent 的测试先看到正确 RED，再由实现转 GREEN
- [x] 独立 code-review agent 结论 APPROVE（无 Critical/Important/Minor）
- [x] 与共享云前端 E2E 通过
- [x] Mock E2E 16/16；真实 Runtime E2E 3/3，覆盖整图、Action Debug、Python Code、真实 Operation branch/finally，并产出可视化截图
- [x] Workflow 对外参数合同、运行前校验、local/cloud 共享表单和提交 payload 均有 unit/E2E 证据
- [x] `develop_execute.yaml`（14 operations）与 `develop_prepare.yaml`（12 operations）来自权威 Git blob，均由通用 Profile/Runtime 导入和执行
- [x] pTLC 迁移形态是可独立构建的装饰器设备包；Human gate 复用 `host_node.manual_confirm`；OS 无 pTLC 专用 API
- [x] 两条现有 pTLC Recipe 通过导入、Canonical/TaskDag 编译、Offline OS 执行与 SQLite 终态验证
- [x] 两条真实 pTLC Operation 已迁移为项目包 Python workflow，调用全部来自 decorator Registry；Code Panel/E2E/截图展示 Python，YAML 仅作 provenance
- [ ] Workflow 文件优先保存 API/UI：原子文件写入→compile/hash→SQLite index；draft 禁止执行（后续 authoring/persistence 切片，不伪报为本轮已完成）
- [x] 能力说明明确区分 RuntimeParameter/恢复游标与 Workflow 参数表单/debugger

**总体结论**: VERIFYING（Quick Debug Alpha；不等同生产发布）
