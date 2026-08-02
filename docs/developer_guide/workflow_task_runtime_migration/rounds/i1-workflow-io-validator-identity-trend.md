# I1：Workflow I/O validator identity 轮次记录

日期：2026-08-02

实现分支：`migration/i1-workflow-io-validator-identity`

integration 基点：`75a6abb69b25b65f75c1c40c5c60b55ceb7353ae`

精确受测/受审候选：`30e97a76c263ec1ae8b47e8a95b4b6a4c590b523`

non-squash integration merge：`1e6e02cf8da0f9073a7cc4a3a1796b8fd76a7afd`

Wayfinder：Core #154 冻结 I1 合同；Core #157 仍是跨仓 E2E 验收门。

状态：**本 validator/identity 切片已通过完整仓库门禁与同一独立
reviewer 的 Standards/Spec `0B/0NB`，并已 non-squash 本地合入 integration；
未推送远程。**

本记录不将整个 I1 或 Core #157 E2E 标记为完成；后续 canonical
Workflow result-record、implicit ResourceSlot output、Task common-validator 复用与真实
FE→OS 联调仍按 I1 设计基线继续。

## 1. 本轮交付

- 新增 transport-independent `workflow_io.py` 深 Module，统一解析 Workflow
  input/output contract、Node input binding、root output binding 与 A1 Handle value schema。
- strict Compile/Generate/Validate/Apply 公共路径逐 binding 执行同一个
  `schema_is_assignable`；普通 protected Graph PUT 仍不获得 reserved contract
  写权。
- assignability 明确实现 nullable、`integer -> number`、list 递归与
  ResourceSlot allowlist subset；无约束 producer 不能满足受限 consumer。
- Workflow input 连接多个 Action 时逐 consumer 证明 subset，因此等价于
  满足所有 consumer allowlist 的交集；系统不会静默改窄已公开的
  Workflow contract。
- `AllowedResourceTemplates(resource_symbol)` 复用 A1 authority-scoped 双向
  identity index：编译时 symbol→UUID，生成时 UUID→symbol，两向不一致
  则 fail closed。
- allowlist 约束真实 ResourceSlot schema，同时覆盖 `ResourceSlot`、nullable
  slot、`list[ResourceSlot]` 与 nullable slot list，列表约束独立作用于每个 item。
- A1 JSON Schema property 投影到 I1 value-set schema 时只移除不改变值集的
  `default/title/description` 与等价开放对象标记
  `additionalProperties: true`；其他未支持结构继续由 closed parser 拒绝。
- 完整 Workflow I/O strict 开关更名为 `validate_workflow_io_contract`，不再
  误导为仅验证 input binding schema。

## 2. 独立 RED 与提交 provenance

本轮始终只使用一名独立 test-author：`/root/i1_os_red_minimal`。独立工作树为
`/home/changjunhan/Uni-Lab-Core/.worktrees/uni-lab-os-i1-validator-identity-red`，
测试分支为 `test/i1-workflow-io-validator-identity`。所有 tests-only 提交均
cherry-pick 保留来源，没有删除、skip、xfail 或弱化断言。

| 独立测试阶段 | 原始提交 | 实现分支提交 | RED / 对照 |
|---|---|---|---|
| output binding identity/assignability | `b443883` | `9ff84ab` | 原始 output RED，生产实现前失败 |
| input binding allowlist/nullability | `4d890f2` | `e64ef56` | `3 failed, 2 passed`；unconstrained、disjoint、nullable 错误被接受 |
| constrained Workflow annotation round-trip | `13ae62c` | `133096c` | `2 failed, 1 passed`；symbol identity 未进入 Workflow contract |
| A1 canonical fixture 对齐 | `6a24937` | `14c800e` | 修正 legacy `type` 与 A1 `value_schema` 双描述不一致 |
| reviewer slot-list finding | `425ea3a` | `34a3864` | singular 对照通过；list/nullable-list 均稳定失败 |

## 3. Finding 收敛

本轮唯一独立 reviewer：`/root/i1_startup_reviewer`，同时覆盖
Standards/module design 与 Spec，并逐次固定精确 SHA。

| 评审 SHA | 结论 | Finding | 处置 |
|---|---|---|---|
| `7ff184f` | `1B/1NB` | strict input 路径绕过 ResourceSlot allowlist/nullable；开关名误导 | `c332221` 移除绕过并更名，公共 RED 转绿 |
| `c332221` | Standards `0B/0NB`，Spec `1B/0NB` | allowlist 未递归到 `list[ResourceSlot].items` | `34a3864` 独立 RED；`30e97a7` 递归定位真实 slot schema |
| `30e97a7` | Standards `0B/0NB`，Spec `0B/0NB` | 无 | 同一 reviewer 复跑 focused `18 passed`，确认不存在未决 finding |

## 4. 精确候选门禁

以 `30e97a76c263ec1ae8b47e8a95b4b6a4c590b523` 为精确候选：

| 门禁 | 结果 |
|---|---|
| input propagation + constrained fixed-point focused | `18 passed` |
| 完整 `tests/workflow` | `1327 passed, 13 warnings` |
| 完整 `tests` | `2366 passed, 4 skipped, 70 warnings` |
| 本轮 production/tests Ruff | passed |
| `service.py` / `store.py` 本轮变更行 Ruff | 0 findings |
| Ruff format | `11 files already formatted` |
| `git diff --check 75a6abb..30e97a7` | passed |
| 独立 reviewer focused 复跑 | `18 passed`，Ruff/diff clean |

完整 Ruff 未作为“全仓零诊断”证据：本仓库尚有与本轮无关的 broad
modernization 存量诊断。本轮证据仅表示所有新增/修改行与可独立检查的
完整目标文件通过，不把存量数量表述为已核验的“历史问题数”。

## 5. 停止线与下一入口

- 本切片不保留 Task preflight 作为静态 allowlist 绕过；M1 对实际
  Material 的解析/预留仍是运行时第二道权威校验。
- 本切片不生成 `WorkflowTask.output`、不实现 Composite、ExecutionPlan
  admission、device dispatch 或 Debugger。
- 不应因本轮可合并就启动 C1 production；C1 仍需要 I1 余下作者化
  与 Core #157 所需的真实跨仓证据达到对应入口门。
