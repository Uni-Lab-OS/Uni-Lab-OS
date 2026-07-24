# 实现进度: Quick Debug Alpha（W1–W2）

## 当前状态

- 开始时间: 2026-07-21
- 最后更新: 2026-07-22
- 当前进度: 5/6 子任务完成
- 状态: 独立评审与最终门禁中

## 实现记录

### T01–T05: 实现完成，进入统一验证

- Registry/Workflow：ActionContract v2、Canonical、typed bindings、Python finite loop、兼容序列化。
- Runtime：设备无关 DagExecutor 组合进程级 live leases、确定性 admission、SQLite journal、unknown fencing、branch/fork/join。
- 生产路径：`return_info` 可穿过 MessageProcessor/TaskDagRunner 形成 `ResultEnvelope`，支持真实下游 binding。
- pTLC：旧 Recipe 变成 `ptlc_station.*` 通用 TaskDag；OS 无专用路由/专用 Runtime；Templates 提供纯声明式 Profile。
- Shared UI：同一 Kernel Workbench 通过 RuntimeClient 切 local/cloud，Action Debug、Code/DAG、Event 与 timeline 复用。
- Workflow 对外参数：Python 函数签名生成有序参数合同；string/integer/number/boolean、required/default/description 进入 Canonical；运行前 fail-closed 校验并由共享 Workbench 动态生成同一张 local/cloud 表单。
- 真实 pTLC Operation：从权威 Git blob 固化 `02_develop/develop_execute.yaml`（14 个 operation）和 `develop_prepare.yaml`（12 个 operation），通过通用 Profile codec 递归展开 `run_script`，在 OS 内执行 branch、result binding、join 与 `finally` cleanup。
- 上述 YAML 能力只作为迁移语义 baseline。`develop_execute` 与 `develop_prepare` 已迁移到 `Uni-Lab-Templates/packages/ptlc_station/ptlc_station/workflows/*.py`，由 decorator Registry 和 Profile `python_ast_v1` importer 走生产 Runtime 路径编译；Python source artifact 与 YAML migration provenance 已分离。
- pTLC 设备能力位于可独立构建的 `Uni-Lab-Templates/packages/ptlc_station` 装饰器包；OS core、Runtime API 和 bridge 没有 pTLC 专用端点或条件分支；Human gate 复用 `host_node.manual_confirm`。
- 独立测试 agent 多轮 RED→GREEN。2026-07-22 主代理新鲜复跑：OS `tests/app tests/registry tests/runtime tests/workflow tests/scheduler tests/devices/ptlc` 为 **353 passed**；Templates 为 **18 passed**；旧 pTLC harness 为 **73 passed**；Cloud Kernel Vitest 为 **28 passed**，typecheck/lint 通过。
- Playwright mock E2E **16 passed**，未豁免 `console.error/pageerror/requestfailed` 均为 0；真实 Runtime E2E **3 passed**。真实 E2E 使用 Profile AST importer 编译设备包 Python，不由前端伪造 Canonical/DAG；YAML 仅由 migration harness 校验 path/blob。完整运行、Action Debug、条件分支和双 `finally` cleanup 均有浏览器证据。
- E2E real golden 截图：`Uni-Lab-Cloud/docs/features/F004-quick-debug-alpha/screenshots/` 下的 `whole-workflow-runtime.png`、`action-debug-runtime.png`、`python-code-view-runtime.png`、`real-develop-execute-dag.png`、`real-develop-prepare-cleanup.png`；以固定 manifest 原子发布。
- 仓库内两条兼容 Recipe（`spotting.yaml`、`spottingandscarp.yaml`）继续覆盖旧格式导入 → Canonical → TaskDag，以及 RuntimeService → Offline OS → SQLite `run_completed` 回归；它们不替代真实 Operation 验收源。
- 架构决定更新为 workflow local file/Git 优先、SQLite index/draft/runtime journal 次之；持久化 save API/UI 仍按 AC-8 收口。

## 遇到的问题

- 系统 `pytest` 启动器仍指向已删除的 Python 3.13；验证命令固定使用 `conda env unilab` 的 Python。
- Cloud 全量 production build 曾在默认 4 GB Node heap 下 OOM；Kernel 相关 unit/typecheck/lint/mock+real E2E 已单独通过，最终报告不得把它写成全量 Cloud build 通过。
- 裸跑仓库根 `pytest -q` 会收集生产目录内连接真实 Modbus/相机的脚本并在 collection 失败；G2 使用 `pytest.ini` testpaths 与显式测试目录，不把硬件脚本伪装成 unit gate。
- 未执行真实 pTLC 硬件 smoke；当前结论只能写 Quick Debug Alpha。
- 运行时不定次 `while/repeat` 和 debugger 尚未实现；恢复游标不得标成“从任意节点开始”。

## 下一步建议

- 独立 code-review agent 最终复审未发现 Critical/Important/Minor；软件 Harness 已新鲜复跑。
- 下一步进入文件原子保存/SQLite 索引、panel-runtime 抽取和单独的真机 smoke 验收。
