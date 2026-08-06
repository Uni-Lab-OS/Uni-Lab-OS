# M1A：Material Authority create/read + shared-UoW tracer 趋势报告

日期：2026-08-01

## 1. Round 结论

M1A 首个纵向 tracer 已达到发布门。固定生产候选
`54fa57f42c56e1450a5c6fe6b94ec6659180f1c9` 通过独立 RED、目标/累积/完整测试、
changed-files Ruff/format、compileall、diff check 与同一独立 reviewer 的 Standards/Spec
exact-SHA 复审，最终为 **Standards 0B/0NB、Spec 0B/0NB**。

该结论只接受以下首个可合并切片，不表示完整 M1 Accepted：

- `unilabos.resources.authority.MaterialModule` 的 Backend-aligned business Material
  create/read seam；
- Registry/PackageCatalog 消费侧注入的最小不可变 `ResourceTemplateIdentity` snapshot；
- `material` durable table、完整 Backend 字段、soft-delete normal-read 边界与 Unicode
  casefold barcode invariant；
- 复用现有 runtime-authority coordinator 的 borrowed UoW，包含 rollback/commit、ownership、
  close 与跨 authority 拒绝；
- standalone SQLite adapter 的 closed initialization errors。

Site、ResourceSlot production adapter、Reservation、Claim/fencing、ChangeSet、legacy
Inventory migration、projection、recovery、REST/SSE 与跨仓 contention/restart gate 均仍是 M1
后续 round，不得因本报告改为 Accepted。M2 `MaterialSource`/selector 继续 deferred，且本轮未创建
其 table、DTO、placeholder 或 feature flag。

## 2. 基线、角色与 provenance

| 项目 | 值 |
|---|---|
| 基线 | `integration/workflow-task-runtime@91b00dd030483058a6d0aafc42f143de829cc1bc` |
| 实现分支 | `migration/m1-material-authority-foundation` |
| 总设计提交 | `2405292`，Backend 字段与 package placement 修订至 `43ef2e7` |
| 独立 test-author | `/root/m1_audit`，全过程同一人 |
| 独立 reviewer | `/root/m1_reviewer`，全过程同一人 |
| 初次被拒候选 | `3d1c966342109e893f2bdfcb3135a8903f467de6` |
| 中间可接受候选 | `5293badfb5559e57da6c141fcfa8af9e1063f280`，Spec `0B/2NB` |
| 最终生产候选 | `54fa57f42c56e1450a5c6fe6b94ec6659180f1c9` |
| 最终 review | Standards `0B/0NB`；Spec `0B/0NB` |

独立 test-author 的 tests-only 原提交与迁移分支保留提交如下；均未 squash：

| RED 批次 | test branch 原提交 | migration 保留提交 |
|---|---|---|
| 首个 create/read tracer | `8e50df1`、修订 `3a602a4`、format `aa7a89f` | `09cb0d8`、`f82ef10`、`3d1c966` |
| shared runtime-authority UoW | `9923bae` | `3769db9` |
| Backend fields/template identity | `1f7d44d` | `c73146f` |
| Unicode/closed initialization finding | `7dde504` | `2a5079f` |
| authority affinity/portable UoW NB | `7977eee` | `83833c3` |

每次 test-author 工作均在独立 `test/m1-material-authority-*` branch/worktree 中串行完成；
reviewer 未参与实现或测试编写。review finding 引起的 production 变化均重新运行完整门并交回
同一 reviewer，最终 review 后未再修改 production。

## 3. RED → GREEN 证据

### 3.1 独立 RED 序列

| 基线 | 独立行为 | RED 结果 |
|---|---|---:|
| M1A implementation 前 | public `unilabos.resources.authority` create/read | collection error：module 不存在 |
| `3d1c966` 前一实现候选 | outer WorkflowStore UoW rollback/commit/adapter close | `3 failed, 3 passed` |
| `1bf6b60` | exact Backend fields、required name、template identity、`class` projection | `11 failed, 1 passed` |
| `ed1164e` | `ÄBC/äbc` Unicode conflict、filesystem closed error | `2 failed, 12 passed` |
| `5293bad` | foreign UoW zero-write、NUL closed error、portable generic UoW surface | `3 failed, 14 passed` |

所有 RED 都通过 public `MaterialModule`、真实 `SQLiteMaterialAdapter` 与真实 `WorkflowStore`
观察行为。测试没有查询私有表、mock transaction、弱化异常、skip/xfail，或把 legacy
Inventory 当新 authority。

### 3.2 最终 GREEN 与门禁

固定 SHA：`54fa57f42c56e1450a5c6fe6b94ec6659180f1c9`。

| 门禁 | 结果 |
|---|---:|
| `tests/resources/authority/test_material_module_v1.py` | `17 passed` |
| M1A + WorkflowStore Backend contract + R1B durable kernel | `223 passed, 1 warning` |
| `pytest -q -rs tests/` | `2031 passed, 3 skipped, 35 warnings` |
| changed-files Ruff `E/F/I` | passed |
| changed-files Ruff format | passed，5 files |
| `compileall` | passed |
| `git diff 91b00dd..54fa57f --check` | passed |
| exact worktree | clean |

三个 skip 均为需要显式环境变量开启的既有 networking slow tests；35 个 warning 为既有
FastAPI/ROS/optional dependency 警告。本轮没有新增 skip、xfail 或 warning waiver。

## 4. Reviewer finding disposition

初次 exact-SHA review `3d1c966` 的 verdict 为：Standards `1B/0NB`，Spec `3B/1NB`。

| Finding | disposition | 实现/测试证据 |
|---|---|---|
| Standards B1：`AGENTS.md` 仍写 `code` 且要求 non-blank | resolved | `1dae391` 统一为 Backend `barcode`、空值可重复、非空 Unicode-casefold unique |
| Spec B1：adapter 自建第二 connection/lock/transaction | resolved | `3769db9` + `1bf6b60`；borrowed UoW 不 commit/rollback/close |
| Spec B2：缺 name/class/template authority 与完整 Backend projection | resolved | `c73146f` + `ed1164e`；`class` 只由注入 identity snapshot 派生 |
| Spec B3：SQLite `LOWER` 只有 ASCII 语义 | resolved | `2a5079f` + `5293bad`；DB unique index 使用 deterministic Unicode casefold collation |
| Spec NB1：filesystem error/path 泄漏 | resolved | `2a5079f` + `5293bad`；随后 `83833c3` + `54fa57f` 关闭 embedded-NUL ValueError |

`5293bad` 复审已把上表 blocking 清零，但保留 Spec `2NB`：SQLite capability 泄漏到 generic
UoW，以及 borrowed UoW 没有 authority affinity。两项没有延期：同一 test-author 写入
`83833c3` RED 后，`54fa57f` 将 collation 移至 SQLite-only internal Protocol，并由 coordinator
验证 active UoW identity。foreign authority 在执行任何 SQL 前 closed reject，两个 authority
均 zero-write 且仍可继续 transaction。最终同一 reviewer 确认 **0B/0NB**。

## 5. 架构与影响范围

### 已建立

```text
Registry/PackageCatalog structured snapshot
  -> ResourceTemplateIdentity(uuid, material_class)
  -> MaterialModule validates identity/name/JSON and derives class
  -> SQLiteMaterialAdapter borrows the sole runtime-authority UoW
  -> material row + Backend-shaped MaterialRecord.to_dict()
```

- Material 与现有 OS Resource Instance 继续是同一领域实体，代码归入
  `unilabos/resources/authority`；持久词汇保持 Backend 的 `material`，没有改成 `resource`；
- Template identity snapshot 是消费依赖，不是新的 Template Catalog owner，也不 import
  Registry/PackageCatalog implementation；
- `ResourceTreeSet` 未参与写入，仍只是未来从 durable truth 构建的 projection；
- standalone coordinator 仅用于独立部署/测试；production shared path 绑定现有
  WorkflowStore connection/UoW；
- `WorkflowStore` 只增加通用的 active-UoW ownership probe，没有获得 Material SQL 或业务规则。

### 后续 round 影响

下一 M1 round 必须从当时最新 `integration/workflow-task-runtime` 新开分支，不在本 tracer
继续堆叠。建议顺序为：

1. Backend-shaped Site + `site_allowed_resource_template` association round；
2. ResourceSlot durable adapter + exact 400/404/409 round；
3. Task Reservation 与 Task create shared-UoW contention round；
4. Claim/fencing；
5. ChangeSet + Job terminal atomicity；
6. migration/recovery/projection/REST-SSE 与 Core crash/contention acceptance。

每一项都继续使用恰好一个独立 test-author、一个 implementation owner、一个 reviewer，且
不能提前实现 M2 selector。

## 6. 发布状态

本报告记录的是 M1A 首 tracer 的 repository-local acceptance evidence。发布后应：

- 更新 OS delivery Issue 与 Core #155，状态仍保持 `stage:implementation`；
- 不关闭 Core #155，不应用 `stage:testing`/`stage:accepted`；
- 不修改 Core submodule pin，直到包含后续 M1 rounds 的 integration candidate 通过跨仓门；
- 在 issue comment 中只引用
  `Uni-Lab-OS/Uni-Lab-OS@54fa57f42c56e1450a5c6fe6b94ec6659180f1c9`
  及本 ledger 的发布 commit，不使用本地 worktree 路径。
