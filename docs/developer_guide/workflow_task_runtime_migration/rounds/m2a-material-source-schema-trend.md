# M2A MaterialSource Schema 趋势报告

日期：2026-08-02

本轮从 OS `integration/workflow-task-runtime@47990c98b836dfe229230e33895514fceec9a764`
新开 `migration/m2a-material-source-schema`。实施期间，M1D 通过
`73415ce225c6e846edcdb4c51887be4603117a53` 进入 integration；本分支以非 squash merge
`ad3033d346d207a6474dd1e2cd6ed5cd7988a839` 对齐该基线后完成复审。

M2A 只交付可由 Python Authoring、Backend-shaped Graph 保存和后续 Task snapshot 共同消费的
MaterialSource 声明合同。它不选择或创建真实 Material，不写 Site occupancy/Reservation，不扣减
Inventory，也不创建 resolution Job；这些运行时能力仍属于后续 M2B。

## 1. 交付合同

### 1.1 Python 与 Graph 的唯一 MaterialSource 形态

`material_source(...)` 编译为一个真实、稳定 UUID 的 `type=material_source` WorkflowNode，节点
`param` 是 closed selector：

- `mode`：`existing` 或 `create_new`；
- `resource_template_uuid`：由选定 Graph Authority 的双向 identity index 解析；
- `mount`：closed `ResourceSlot` 引用 `{"uuid": "<material_uuid>"}`；
- `material_uuid`：`existing` 可选固定 Material，`create_new` 必须为空；
- `site`：可选固定 Site UUID；
- `slot_range`：可选、非空、有序、去重的 Site UUID 数组，与 `site` 互斥；
- `flow_role`：`primary_sample`、`aliquot_sample`、`reagent`、`consumable`。

对应 Python `MaterialFlowRole` 同时提供“主样品、分装样品、试剂、耗材”中文标签。固定 Material
可省略 Site/range，但 authority 必须证明它已经位于 mount 的兼容 Site；自动选择时省略 Site/range
表示 mount 的全部 Site 候选。`WorkflowNode.material_uuid` 对 MaterialSource 保持 `null`，选择条件
只存在于 `param`。

MaterialSource 变量本身是唯一 `ResourceSlot` output；规范 Python 只允许下游直接引用该 bare
variable，不接受 `.material` 别名。Graph 只允许这条 Material chain 单消费者；同一物理 Material
不能 fan-out 到并行分支。D-067 的 producer/consumer ResourceTemplate allowlist 兼容性在保存前
静态证明。

### 1.2 生产 Catalog 与双向 identity

OS HostNode ResourceTemplate 发布一个 authority-owned MaterialSource framework template 和一个
`ResourceSlot` source Handle；不会为每个业务 ResourceTemplate 复制 NodeTemplate。

Registry→Catalog 和 Authoring Engine 共用同一个 `ResourceTemplateIdentityIndex`：

```python
resolve_symbol(source_identity) -> resource_template_uuid
identify_uuid(resource_template_uuid) -> source_identity
```

显式双向 index 可同时完成 compile、generate-python、compile 往返。含 HostNode/MaterialSource 的
production snapshot 若只提供旧单向 callable，会在 runtime ready 与 Catalog 发布前以
`template_catalog_mismatch` fail fast；不含 HostNode 的旧 A1 action-only 组合继续兼容。

### 1.3 静态 Material/Site authority 证明

Engine Preview、direct graph save 和 Authoring Apply 共用 MaterialSource selector/authority 规则。
保存路径先完成 closed selector、framework aggregate、拓扑和 D-067 纯校验，只有纯校验成功才查询
Material authority。

Direct save 与 Apply 均把只读 static-authority validator 注入 WorkflowStore。Store 在同一
`BEGIN IMMEDIATE` UoW 内查询 mount Material、固定 Material、固定/范围/全部 Sites，并在任何 Graph、
revision、Applied Source、authoring record 或 event 写入前完成证明。事务内事实消失会返回稳定错误并
整体回滚；Apply 的 graph/source-only 两条路径都关闭了事务外重编译到 commit 之间的 TOCTOU 窗口。

错误合同保持关闭且不回显敏感 UUID/driver exception：

- `invalid_material_source`：selector/framework 形态错误；
- `not_found`：Material/Site 不存在或已删除；
- `material_source_conflict`：owner、template、occupancy 或兼容 Site 冲突；
- `material_authority_unavailable`：authority 未配置、不可用或抛出未知普通异常；
- `template_catalog_mismatch`、`material_flow_fan_out`：identity/物料链静态合同失败。

`KeyboardInterrupt`、`SystemExit` 等控制异常不会被吞掉。

## 2. 独立测试 provenance

本轮唯一 test-author：`/root/test_m2a_material_source_schema`。所有 tests-only tracer 均在独立
worktree/branch 上先于对应生产实现；原始提交与迁移分支保留提交具有相同 stable patch-id：

| tracer | 原始 tests-only commit | 迁移分支保留 commit |
|---|---|---|
| 首条 Python→Graph→Python vertical slice | `79ba0577bea06b1b1bf5a624e44f746b4745dc02` | `7b23f57a0f52d10399a6274395d0dfa186fb906f` |
| 持久 Edge 语义 | `2f1b6cb5ae944930026596a37a43eb2c6e1dea9c` | `c666b9b4624b059900caec63c7c3f7d8da90bd33` |
| selector mode/role matrix | `38e32a678f0761e62ed5909236f4ce856f9ff51a` | `04c911da039beec7ca5d5d446372de46a226f423` |
| direct Graph selector | `0bebe762f59e3d5e90f7bb2553658dba9636cfcc` | `e7088fe018f23fe6b756816260f0902458fde331` |
| Site authority | `7a39b0466ed2c4b7badb2560a7a969ad125d0297` | `2fb036b78bd2425e4c461f11b67b04efd2023cbf` |
| 单一 ResourceSlot material chain | `ccf80612f8801095dc3295c73a9ce4565138462b` | `c3239a02654cf282c2d957b3e9bc3eba802a0b12` |
| D-067 template compatibility | `f1273c08dc64fc19d5e315171248b2d285a12793` | `eceb582a0a20dbcb5bf8130e8988e8876969a655` |
| production composition | `f42747c3dc2716e77eef05c110a8ef6cf0f9464c` | `a49d486fead44e3ba4642f76c5b31c0be8aef9a6` |
| public contract hardening | `5fd146188eaf0a90102908f4baaa3e80917a052e` | `dec063cd19aafa3ca33023d04aebaecb148ebed5` |
| stale Apply authority facts | `f598e65a6edb7c36c2b2af809322fc1274fd6926` | `d745ee7caddc9e5ca80431dd37cdbe0faae04ef7` |
| 首轮 review validation/redaction | `d07962fc48be684f68dcdf5446ace5e586fd24c8` | `b1418f3c7a608c4fb00818d99049592625cb0b1e` |
| Store UoW/rollback | `d2acc4beb643b528b13dae50c32002061c9eacaf` | `10141e7f544f38ebaf34b58ada744f84d19e23c6` |
| production 双向 identity | `283cc967bd11b82a11ba3a8a57644ee01cf72a85` | `8be75568ca33c246ed28bec6a1b1e0dad1f6d8f9` |

首轮 review 回归在修复前为 `15 failed, 4 passed`；UoW tracer 在修复前为 `3 failed,
2 passed`；identity tracer 在修复前为 `2 failed, 5 passed`。独立测试没有被删除、弱化、skip 或
xfail；identity tests-only 提交对旧 fake 的唯一调整是补齐新增的 optional keyword-only `uow`
port。

## 3. 独立评审与 finding disposition

本轮唯一 reviewer：`/root/review_a1_action_catalog`，分别检查 Standards 与 Spec。

第一次评审 exact SHA `ad3033d346d207a6474dd1e2cd6ed5cd7988a839` 的结论为
`CHANGES_REQUIRED`，Standards `1B/1NB`、Spec `3B/1NB`：

1. `STD-B01`：未知 authority 异常会穿透公共边界；
2. `SPEC-B01`：direct save 在纯 Graph 校验前查询 authority；
3. `SPEC-B02`：Apply 最后一次 Material/Site 证明不在提交事务内；
4. `SPEC-B03`：production 显式单向 resolver 与 Engine 双向 index 断开；
5. `STD-NB01`：`resource_ref()` 误执行错误回显 UUID；
6. `SPEC-NB01`：Python grammar 接受非规范 `material_source_var.material`。

前三组独立 review tests 经 `ff2093330d06f882c89f1cb57b47c438c28a340a`、
`0beb5ca001741097215fc3f94ff8f9c7446f238d` 和
`d895c95abcc492d198573e5d31852e76b7ed936b` 逐项修复。第二次评审固定在 exact candidate
`d895c95abcc492d198573e5d31852e76b7ed936b`，独立 detached worktree 为
`/tmp/uni-lab-os-m2a-rereview-d895c95-8bchy9`，最终确认：

- Standards：`0B/0NB`；
- Spec：`0B/0NB`；
- 上述 `4B+2NB` 全部 resolved；
- 未发现 M2B 越界；
- 最终结论：`ACCEPT`。

## 4. 最终门禁

固定 behavior/review candidate SHA：`d895c95abcc492d198573e5d31852e76b7ed936b`。

| 门禁 | 结果 |
|---|---:|
| M2A + M1D 专项 | `112 passed, 1 warning`（主实现 worktree） |
| reviewer 独立 M2A + M1D | `112 passed, 2 warnings` |
| 完整 `tests/workflow` | reviewer：`1308 passed, 13 warnings` |
| 完整 `pytest -q -rs tests` | `2318 passed, 4 skipped, 50 warnings` |
| changed-files Ruff `E/F/I` | passed，26 files |
| changed-files Ruff format | passed，26 files |
| changed production `py_compile` | passed |
| `git diff 73415ce..d895c95 --check` | passed |
| exact candidate worktree | clean |

四个 skip 是三个需环境变量显式开启的 networking slow tests，以及一个需显式 Phoenix executable
的 integration test。warnings 为既有 FastAPI/TestClient deprecation、测试类收集、可选 SOCKS
依赖和 Pydantic Field 提示；本轮没有新增 waiver。

## 5. 停止线与下一 frontier

本轮只修改 OS 的 Authoring marker/Engine、Graph validator、Template Catalog/production
composition、Material/Site 只读 authority port、Workflow Service/Store 保存边界及合同测试。没有
修改 Backend、FE、设备包、Core submodule pin，也没有 push。

后续 M2B 必须从本轮合并后的最新 `integration/workflow-task-runtime` 新开 migration round，并由
独立 RED 冻结 Task admission 时的 `existing` 选择、`create_new` 创建、逐个 MaterialSource
满足/失败、Task Reservation 与 Site 选择边界。本轮不能被解释为已经完成实际物料分配、上台、
Inventory 扣减、搬运规划、传感器事实或 reconciliation。
