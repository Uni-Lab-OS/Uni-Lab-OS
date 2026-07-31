# Round 02G：persistent Authoring production composition 趋势与策略报告

日期：2026-08-01

实现分支：`migration/02g-persistent-authoring`

integration 基线：`27d625f`

最终 production/test 候选：`6ae0667`

状态：**目标、Phase 02 当前累积门和受控完整门全绿；同一独立 reviewer 最终确认
Standards/Spec 均为 0 blocking、0 non-blocking，允许非 squash 本地合并。**

## 1. 本轮交付

02G 把此前分开的 Catalog、编译器、纯转换、持久 Draft/Apply 和 package source lifecycle
组合到一个真实 OS production root：

```text
显式 local CatalogAuthority + editable package roots
  -> WorkflowStore(workflow.db)
  -> TemplateCatalog
  -> WorkflowAuthoringEngine
  -> WorkflowService
       -> startup recovery
       -> WorkflowSourceMonitor
  -> 一次性挂载 pure + persistent + SSE routes
```

交付内容：

- production 必须显式提供 `CatalogAuthority` 和 editable roots；persistent runtime 只接受
  `kind=local`，不得把 Backend Catalog identity 写入本地 SQLite Graph/Apply；
- pure compile/generate/validate、Draft PUT、外部文件 reconcile、启动恢复和 Apply revalidation
  共用同一个真实 `WorkflowAuthoringEngine`、Catalog facade 和 Store；
- production router 先在临时父 Router 中完整构造 pure、persistent 和 SSE，再通过一次 app
  mutation 安装；构造失败不半挂载，修复后重试不产生重复 operation；
- pure 与 persistent Candidate 共用 `candidate_validation.py`；Service 只保留 Backend read DTO
  hydration、持久状态与错误归一化，删除了约 150 行重复语义 validator；
- Candidate raw entity 使用 closed field boundary；Workflow identity、非 authoring metadata、
  source-map、changeset、Graph、minimal Catalog projection 和 retained Catalog/Handle 投影均由
  同一边界验证；
- semantic Apply 在同一 SQLite transaction 中提交 Graph、`name`、`description`、reserved
  `unilab` metadata、applied source/source map、revision 和 event；source-only Apply 不增加
  revision；
- public Apply 继续只接受 `candidate_hash`，会重新编译并持有 Catalog guard，且绝不回写
  package source，成功 `warnings=[]`，不会创建 Task/Job；
- 旧 Catalog 数据在任何 schema/index mutation 前做 active business-key 审计；Catalog
  import/lookup 与 migration audit 共用 Python `strip().lower()` Unicode 规范化规则，冲突以
  `legacy_catalog_business_key_conflict` 零变更失败。

本轮没有修改 Frontend、Backend、Task input preflight、Material、Scheduler 或设备执行路径。

## 2. 独立 TDD 与评审 provenance

| 阶段 | 唯一 subagent | 提交/候选 | 结果 |
|---|---|---|---|
| 原始合同测试 | `round02g_test` | `9c9acae` | 14 个新用例；`9 failed, 18 passed` |
| 测试观察修正 | 同一 test-author | `8fc39d1`、`aaccba4` | OpenAPI 代替 FastAPI 私有 route；按 D-077 从 `draft.source_uri` 读取 |
| 旧合同迁移 | 同一 test-author | `e69ec5d` | 把 Round 6/11 从“整库投影/未知字段丢弃”迁到 closed + minimal projection |
| 首个实现候选 | 主代理 | `e073e65` | focused 27、扩展 912、完整 tests 1698 全绿 |
| 首次双轴评审 | `round02g_review` | `e073e65` | 3 个唯一 Blocking、0 NB；Standards 2B，Spec 3B |
| finding tests | 同一 test-author | `5b75625` | 8 项中 `5 failed, 3 passed`，精确覆盖 3 个 Blocking |
| finding 修复 | 主代理 | `6ae0667` | local-only、Unicode shared key、router atomic install |
| 最终复核 | 同一 reviewer | `6ae0667` | Standards 0B/0NB；Spec 0B/0NB，可合并 |

全程一次只运行一个 subagent。finding test 与复核继续使用原 test-author/reviewer，没有新增
并发 agent，也没有让实现者删除、skip 或弱化独立测试来制造 GREEN。

## 3. 实现与测试规模

相对 `27d625f...6ae0667` 的 production/test 净变化：

| 类别 | 文件数 | 新增 | 删除 | 净增 |
|---|---:|---:|---:|---:|
| Production | 9 | 295 | 182 | 113 |
| Tests | 7 | 933 | 87 | 846 |
| 合计 | 16 | 1228 | 269 | 959 |

Production 新增行不大，且净增只有 113 行，主要原因是本轮在增加 composition、原子 router、
Unicode migration audit 和 shared key helper 的同时，删除了 Service 内第二套 Candidate
validator。按新增行计算，tests/production 比约为 `3.16`；它来自真实 server、两条 HTTP
路径 parity、Apply transaction、失败注入和旧库零变更快照，而不是业务 DTO 膨胀。

测试行数也包含 104 行早期 Round 6/11 合同重写：这不是放宽，而是把已经被 D-077/02E
替代的“空图携带整库 Catalog”和“未知字段静默丢弃”改为闭合 DTO 与最小 Catalog 投影，
并保留 retained Catalog/Handle 反篡改覆盖。

## 4. 最终门禁

精确候选 `6ae0667`：

```text
02G focused + Phase 01 server 回归：        30 passed
Workflow/Authoring 扩展门：                 915 passed
Phase 02 当前已存在的计划切片：             479 passed
完整 tests/：                              1701 passed, 3 skipped
独立 reviewer finding + 相关旧合同：        65 passed
独立 reviewer 累积专项：                    222 passed
Ruff 相对 integration 差分：               0 个新增诊断，减少 10 个旧诊断
Ruff critical / changed clean files：       passed
Ruff format --check：                       passed
git diff --check：                          passed
```

计划门中的 `tests/workflow/test_workflow_task_input_v1.py` 属于尚未开始的 02H，当前文件不
存在，因此未把它伪装为 02G 通过项。

裸仓库根 `pytest -q` 会额外收集 `unilabos/device_comms/modbus_plc/test` 和
`unilabos/devices/cameraSII/*_test.py` 两个既有硬件示例，并分别因真实 Modbus 连接/过期
构造参数和本地 Camera import 在 collection 阶段失败。受控完整门继续使用历轮一致的
`pytest tests -q`。该仓库测试发现配置债务与 02G 无关，不计作候选回归，也没有在本轮顺手
修改设备代码。

33 个 warning 来自既有 TestClient/httpx、pytest class collection、可选 SOCKS 和 FastAPI
`on_event` 提示；新增 production server fault tests 使既有 lifespan warning 被更多次观察，
但没有功能失败或未登记 skip。

## 5. 问题趋势

| Round | 初次 review blocking | 初次 non-blocking | 最终 blocking | 最终 non-blocking |
|---|---:|---:|---:|---:|
| 02B1 | 7 | 1 | 0 | 1 |
| 02B2 | 2 | 1 | 0 | 1 |
| 02B3 | 4 | 1 | 0 | 0 |
| 02B completion | 2 | 1 | 0 | 0 |
| 02C | 3 | 3 | 0 | 2 |
| 02D | 6 | 2 | 0 | 1 |
| 02E | 1 | 1 | 0 | 1 |
| 02F | 3 | 1 | 0 | 0 |
| 02G | 3 个唯一问题 | 0 | 0 | 0 |

02G 初始 RED 从 9 个失败收敛到 1 个测试观察问题，再到 0；扩展门随后发现 9 个旧 Catalog
语义冲突，分类后 3 个由 shared retained-projection 校验关闭，6 个确认是被 D-077/02E
替代的旧断言并由独立 test-author 迁移。首次 reviewer 又找到 3 个唯一生产根因：Authority
边界、Unicode business key、router 原子安装；一批精确 RED 和一批修复后全部归零。

因此整体不是不断产生新的产品设计问题，而是 02G 作为组合轮第一次把此前模块放进真实
production root 后，暴露了三个集成边界。收敛路径是：

```text
9 个初始缺行为
  -> 0（核心实现）
  -> 9 个累积冲突
  -> 0（共享规则 + 旧合同迁移）
  -> 3 个 review 根因
  -> 0（一次 finding 修复）
```

没有新增 public DTO、持久模型或 grill 组；最终 0B/0NB，问题总量继续趋于减少。

## 6. 遗留问题与重要性

### 02G 自身

**无遗留 blocking 或 non-blocking。** production composition、Candidate parity、Apply、
Unicode migration 与 route retry 均有独立行为证据。

### Catalog 真实发布入口

重要性：**高；是实际部署和最终 FE-OS E2E 的 integration dependency，但不是 02G
Blocking。**

02C 已冻结 `TemplateCatalog.replace()` 为外置 Registry import / Backend sync 发布 seam；
02G compiler 不应在读请求中隐式同步 Catalog。下一轮真实联调可通过明确的 local importer
fixture 发布 Catalog，但不能把“测试已 seed Catalog”表述为部署 importer 已接通。

### 裸 pytest 硬件示例收集

重要性：**低到中；属于仓库测试基础设施债务。** 它不影响 `tests/` 受控门，但应在单独
维护轮通过 `testpaths`/collection policy 或硬件 integration marker 处理，不应夹带进
Authoring/Frontend 迁移。

## 7. 策略调整

1. 下一轮立即进入独立 Frontend 分支，不等待 02H；Frontend 只实现 D-117 单编辑权按钮、
   只读投影、完整 diff 接受和持久 Authoring aggregate/SSE 状态，不修改 Backend。
2. FE-OS 联调必须启动真实 OS production app、真实 SQLite、真实
   `WorkflowAuthoringEngine` 和真实 SSE；允许通过 02C 的显式 local importer seam seed
   Catalog，禁止 route mock。报告必须把“受控真实 OS E2E”和“部署 importer 尚未接通”分开。
3. Frontend 轮继续保持一个 test-author、一个 reviewer、每次仅一个 subagent；finding
   follow-up 复用原角色。
4. 组合根变更的首轮测试以后必须默认包含合法但不适用的 authority kind、第二阶段安装失败
   和 Unicode 业务键；不再只测 `None`、ASCII 和 happy path。
5. 02H 仍从完成 FE 联调后的最新 integration 独立开分支，不能把 Task input preflight
   夹进 Frontend 或 02G 修复。

## 8. Frontend、Backend 与合并判断

- Frontend：**02G 未修改**；下一工程 Round 单独实现；
- Backend：**未修改，也未调用 Backend 写路径**；
- FE-OS：真实 OS public interface 已完成，受控联调前仍需显式发布 local Catalog；
- 最终评审：Standards 0B/0NB，Spec 0B/0NB；
- 合并：允许非 squash 本地合入 `integration/workflow-task-runtime`，不 push；
- 下一步：从合并后的 integration 创建独立 Frontend 分支并直接继续。
