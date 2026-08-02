# Round R2E：SZLab 声明工作流经 FE 到 ROS 的完整执行

日期：2026-08-02

实现分支：`migration/r2e-szlab-workflow-ros-e2e`

integration 基线：`75a6abb69b25b65f75c1c40c5c60b55ceb7353ae`

最终 production/test 候选：`31590d51f7008afd1f57b12dd6c805e250dee57d`

FE 已合并候选：`40fe1b83a62d056c054f3a44b2bd0f93d4569521`

状态：**PackageCatalog 声明的 SZLab S09 工作流已从原 FE 工作台创建 WorkflowTask，
经 ROS backend 与 HostNode 顺序执行四个 Job；完整门禁、半程/终态截图、OS 全日志和同一
独立 reviewer 的最终复审均已通过，允许 non-squash 本地合并。**

## 1. 本轮范围与停止线

本轮交付：

- 公共 `unilab` CLI 继续经 `unilabos.app.main:main` 启动，接受 workspace、config、graph、
  working directory、`--backend ros` 与 FastAPI bridge；
- 启动时由 PackageCatalog 编译 workspace 中声明的 Workflow，完成 Registry、
  TemplateCatalog、Authoring 与 WorkflowTask runtime composition；
- WorkflowTask worker 只使用 canonical `job_uuid`、`task_uuid`、`node_uuid`、
  `workflow_uuid` payload，在 Edge scheduler boundary 才适配旧 dispatch DTO；
- HostNode 对一个设备的 ActionClient 使用 staged 初始化；任一必要 ActionClient 失败时不发布
  device、online 或 ready 事实；
- dispatcher 同步异常进入 `execution_unknown`，包括异常文本为空时使用稳定
  `dispatch_outcome_unknown`，不能把不确定物理结果伪造为 failed；
- composition 拒绝 dispatcher、resolver 或 PackageCatalog 配置漂移，worker stop 注销 result
  listener；
- legacy transport 使用显式 `@legacy_action`，不调用 canonical typed parser；普通
  `@action` 仍严格 fail closed，完整 Registry snapshot 不再按来源整体排除；
- FE desktop launcher 不再导入或启动 `local_bridge_entrypoint.py`，直接启动上述公共 CLI，并
  等待 health 与 workflow template readiness。

明确停止线：

- **没有 Local Bridge，没有 Profile**；连接参数继续由 graph 承载；
- R6 已取消，本轮不做外部包按单设备选择性导入；
- production physical dispatch 需要完整 durable Job Execution Claims。本轮尚未实现 Claims，
  因而非 `test_mode` 的 WorkflowTask ROS dispatcher 必须 fail closed；
- 本轮 E2E 的动作结果是 `--test_mode` 模拟事实，不代表真实硬件已执行；
- `POST /api/v1/device-action-runs` 的 FE→Backend 单步动作接口不在本轮实现。它必须另开迁移
  轮次，先按 Backend `feat/workflow` 合同写 OS/FE spec，并先交人工评审。

## 2. 独立 RED 与提交 provenance

唯一独立 test-author 为 `/root/r2e_test_author_resume`，使用独立 worktree
`uni-lab-os-r2e-szlab-workflow-ros-e2e-tests` 与分支
`test/r2e-szlab-workflow-ros-e2e`；未编写 production。

| 阶段 | tests-only commit | RED / 处置 |
|---|---|---|
| 首轮 CLI/Package Workflow/ROS RED | `28b79c9` | 3 项因缺 production composition、HostNode/ROS 执行闭环而失败；经 `0f0d327` non-squash merge 保留 |
| 首审 finding RED | `2316105` | 旧候选上 `10 failed, 4 passed`，冻结 unknown fence、listener lifecycle、composition identity、HostNode readiness、完整 Registry 与声明 Workflow 来源；经 `692667d` merge 保留 |
| legacy assertion 纠正 | `37e3699` | legacy transport 可保留 transport schema，但禁止 canonical typed extension/diagnostic；旧候选仍因缺原名 record 而 RED；经 `a28be09` merge 保留 |
| bare exception finding RED | `638fdb7` | bare `RuntimeError()` 后 Job 实际卡在 running，预期 `execution_unknown` 与 Task reconciliation；经 `9f480d2` merge 保留 |

测试没有删除、skip、xfail 或弱化行为断言。最后一次 test-author commit 只增加 40 行回归，
独立 RED 为 `1 failed`，失败原因是空 reason 被 `mark_job_unknown()` 拒绝。

主要 production lineage：

```text
190befa  feat(workflow): execute package workflows through ROS
8735a40  style(workflow): format ROS runtime worker
a4a46db  fix(workflow): fence ROS test execution runtime
31590d5  fix(workflow): fence empty dispatch outcomes
```

## 3. 独立 review 与 finding 收敛

本轮恰好一名独立 reviewer：`/root/r2e_workflow_ros_reviewer`。Reviewer 未实现 production，
也未编写本轮 tests。

| 评审 SHA | 结论 | 主要 finding 与处置 |
|---|---|---|
| `8735a40` | 不可合并 | dispatch exception 曾伪 failed、非 test physical dispatch 缺 Claims、composition 重入忽略配置、listener 泄漏、旧 identity payload、来源整体排除、HostNode 伪 ready、测试临时创建 Workflow；全部 `accepted-fixed` |
| `a4a46db` | Standards 1B/1NB；Spec 0B/0NB | bare `RuntimeError()` 的空 reason 使 Job 卡 running；由独立 RED `638fdb7` 复现并在 `31590d5` 修复 |
| `31590d5` | Standards 0B/1NB；Spec 0B/0NB；允许合并 | reviewer 独立重跑 bare-exception 回归为 `1 passed`，并交叉核对 E2E JSON、截图与 OS 日志 |

最终唯一 non-blocking：test mode 中排队动作可能在真正取得设备执行权前写入
`running/started_at`。当前 dispatcher 只允许 test mode，因此不构成 physical safety blocker；
后续应让 scheduler/dispatcher 的 authoritative accepted/started fact 驱动该状态，不能在本轮
Claims 停止线之前顺带扩大 production 执行语义。

## 4. 精确候选门禁

所有最终结果针对 `31590d51f7008afd1f57b12dd6c805e250dee57d`：

| 门禁 | 结果 |
|---|---|
| bare-exception finding | `1 passed` |
| R2E focused | `15 passed, 1 warning` |
| R1B + Registry + PackageCatalog + scheduler 累积门 | `713 passed, 1 warning` |
| 完整 `pytest -q tests/` | `2364 passed, 4 skipped, 69 warnings` |
| 现代改动与新测试 Ruff `E,F,I` | passed |
| 现代改动与新测试 `ruff format --check` | passed |
| 全部本轮修改 Python 文件 `py_compile` | passed |
| `git diff <base>..HEAD --check` | passed |
| worktree | clean |
| 独立 reviewer | Standards `0B/1NB`；Spec `0B/0NB`；mergeable |

遗留大文件的全文件 Ruff 在 integration 基线已有债务：相同 legacy 文件的候选为 478 条，
基线为 480 条；错误类别相同，候选少 2 条 E501，没有新增 lint shape。全目录
`compileall unilabos` 仍会命中基线已有的
`unilabos/devices/cytomat/cytomat.py:4` 未闭合括号；本轮全部修改 Python 文件已单独编译通过，
完整测试也已通过。4 个 skip 与 warning 类别均来自既有 optional hardware、TestClient/
FastAPI deprecation、test class collection 与 Pydantic Field 提示。

FE `40fe1b8` 的独立门为 desktop tests `18 passed`、desktop typecheck passed、desktop build
passed；FE worktree clean。没有恢复 Local Bridge。

## 5. 精确 SHA 的真实 E2E 证据

OS 使用以下等价公共 CLI 形态启动；验收运行的端口为 `18026`：

```bash
PYTHONPATH=<OS-worktree>:<SZLab-workspace> \
unilab \
  --workspace /home/changjunhan/Uni-Lab-Core/Uni-Lab-SZLab \
  --graph /home/changjunhan/Uni-Lab-Core/.whalent_tmp/2026-08-02/73g0elxvl0bltnmu3b3ovxdv5-szlab-ideawit-sim_copy.json \
  --config /home/changjunhan/Uni-Lab-Core/Uni-Lab-SZLab/deployment/local_config.py \
  --working_dir <artifact>/runtime \
  --backend ros \
  --app_bridges fastapi \
  --port 18026 \
  --disable_browser \
  --skip_env_check \
  --test_mode
```

执行事实来自 PackageCatalog 声明 Workflow，而不是临时创建 Workflow：

| 事实 | 值 |
|---|---|
| OS SHA | `31590d51f7008afd1f57b12dd6c805e250dee57d` |
| FE SHA | `40fe1b83a62d056c054f3a44b2bd0f93d4569521` |
| Workflow | `d176a938-5e34-511b-9e28-68540833559b`，`S09 移液调试` |
| Task | `fc50b897-e682-4bcd-94e4-1d3c5df7857e` |
| Device | `szlab_mixer_pipetting_station` |
| 半程 | Task running/paused；prepare、bind succeeded；add、release pending |
| 终态 | prepare、bind、add、release 全部 succeeded；Task succeeded |
| Browser | console/page errors `[]` |

OS 日志交叉验证：

- line 46：WorkflowTask ROS backend 启用，并明确标注仅 `test_mode`；
- line 60：Host Node initialized；
- line 174：同一 Task 进入 running；
- lines 176/182、219/225：前两个 Job dispatch/succeeded；
- line 228：pause command HTTP 201；
- line 259：FE resume command HTTP 201；
- lines 268/273、293/298：后两个 Job dispatch/succeeded；
- line 318：同一 Task succeeded。

完整证据根：

```text
/home/changjunhan/Uni-Lab-Core/.artifacts/r2e-szlab-workflow-ros-e2e-31590d5/
├── os-console.log
├── fe-e2e-workflow-result.json
├── runtime/logs/
└── screenshots/
    ├── 00-s09-declared-workflow-loaded.png
    ├── 01-s09-applied-ready.png
    ├── 02-s09-workflow-in-progress.png
    ├── 03-s09-paused-at-half-2-of-4.png
    └── 04-s09-resumed-and-succeeded.png
```

## 6. 合并与下一轮

本报告是 ledger-only 变更，不修改 `unilabos/` 或 `tests/`；最终经过门禁与 review 的行为 SHA
仍是 `31590d51f7008afd1f57b12dd6c805e250dee57d`。本轮必须保留独立测试 merge 与所有修复
commit，以 non-squash 方式本地合入 `integration/workflow-task-runtime`，未经授权不得 push。

实际 non-squash integration merge 为
`fd716cdfa0d945452af75323afb26c982c2b20d3`。本轮进行期间 integration 另行合入 I1 typed
Workflow I/O validator，因此合并后额外执行组合门禁，而没有只依赖两个分支各自的结果：

- R2E focused：`15 passed, 1 warning`；
- 完整 `pytest -q tests/`：`2381 passed, 4 skipped, 69 warnings`；
- Ruff `E,F,I`、format、全部本轮 Python 文件 `py_compile`、merge diff check：passed；
- integration worktree：clean。

组合后还用 merge SHA `fd716cd` 重跑相同真实 FE→OS→ROS E2E。Workflow UUID 保持
`d176a938-5e34-511b-9e28-68540833559b`，Task 为
`25259a00-e036-42b5-bd68-3d61d59cee5b`：半程仍为 2 succeeded / 2 pending，resume 后
4/4 succeeded、Task succeeded，`browser_errors=[]`。完整组合证据位于：

```text
/home/changjunhan/Uni-Lab-Core/.artifacts/
  r2e-szlab-workflow-ros-e2e-integration-fd716cd/
```

因此 I1 input/output validator 与本轮 ROS runtime 在最终 integration 上经过同库完整测试和
同一 S09 browser execution，不存在仅在分支级门禁中被遗漏的组合冲突。合并与记录均为本地
操作，没有 push。

下一轮只允许从本轮合并后的 integration 新建分支。其第一份交付物是
`POST /api/v1/device-action-runs` 的 OS/FE implementation spec，合同对齐 Backend
`feat/workflow`，并在任何 production code 之前交人工评审。
