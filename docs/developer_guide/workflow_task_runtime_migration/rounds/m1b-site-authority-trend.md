# M1B：Backend-shaped Site Authority 趋势报告

日期：2026-08-02

## 1. Round 结论

M1B 已完成 Backend-shaped Site create/read 纵向切片。固定行为候选
`fcfcc1412b61b20c7ae85078c986182a761ad394` 通过独立 RED、目标测试、完整仓库测试、
changed-files Ruff/format、compileall、diff check，以及同一独立 reviewer 的 Standards/Spec
精确 SHA 复审。最终结论为 **Standards 0B/0NB、Spec 0B/0NB**。

本轮只接受以下能力：

- `MaterialModule.create_site/get_site` 的 Backend-shaped public seam；
- `site` durable table 与规范化的 `site_allowed_resource_template` 关联表；
- owner/occupant/template authority、单 Site occupant、单 Material placement、组合与 placement
  无环、owner 内名称唯一及 `sort_order` 稳定排序等不变量；
- Site + allowlist 在 owned/borrowed UoW 中的原子写入；
- geometry、JSON、UUID、soft-delete normal-read 和稳定 domain error 边界。

本轮没有实现 ResourceSlot resolver、MaterialSource、Reservation、Claim、Site observation、
reconciliation、REST/SSE 或前端读模型；M1 与 M2 决策仍处于实现阶段。

## 2. 基线、角色与 provenance

| 项目 | 值 |
|---|---|
| 基线 | `integration/workflow-task-runtime@46cf3649b9b6ff7b89765da81106148ac0f4209a` |
| 实现分支 | `migration/m1b-site-authority` |
| 控制面 | Core `#155`、OS delivery `#6` |
| 唯一指定的独立 test-author | `/root/test_m1b_review_fixes` |
| 唯一独立 reviewer | `/root/review_m1b_site_authority` |
| 首次完整候选 | `c7a218ffb8da65d6222599124ae4b8fb52ab0532` |
| 首次复核候选 | `21f9b6f241ac06026538cd84619afe6490833425` |
| 固定行为候选 | `fcfcc1412b61b20c7ae85078c986182a761ad394` |
| 最终行为 review | Standards `0B/0NB`；Spec `0B/0NB`；ACCEPT |

tests-first 提交均保留在可审查历史中，没有 squash、删除、skip 或 xfail：

| 合同批次 | tests-only 原提交 | migration 保留提交 | RED 证据 |
|---|---|---|---|
| public Site create/read tracer | `cf0455fd519a1122785687b18af5ee44a6dee385` | `5343283cba3b521d546386c7885b81badda1741a` | production module 尚无 Site seam，collection/contract RED |
| owner/occupant/template authority | `25e5a83c3f9b91550deb1ae61d572044d3bd5441` | `99cd78cc2a2225e7d8cfe0a80fae198c6262c023` | 缺失引用、template allowlist 与 zero-write RED |
| placement invariants | `7a9d6a10813bc8eea7bdd3b8811c42512f96d41e` | `4494479f0b04bc810cb3fc256730aa6cb8dee23b` | occupant 唯一、组合/placement 环与 owner-local uniqueness RED |
| 首轮 finding 回归 | `bce62180443105fe985b40e0c6cbb33759941db0` | `45a34a203cdbe13d8003f3e6f93297dfdd710690` | borrowed UoW fault injection 与 projection/error 边界 RED |
| 独立 test-author 事务/数值 RED | `d7a14e6e7862c0e58a2121577fbd4e6fa122100c` | `bbf8e9706908834979a56b31eca2f9dba19e51e3` | `3 failed`：borrowed UoW partial write、geometry/sort overflow |
| 独立 test-author 错误优先级 RED | `f46943183189b7c77aabf8e2c87b4bec45f273d0` | `5c8ef80d21491de688d4175a8dae858ede6d57d4` | `1 failed`：未知相同 owner/occupant 错报 conflict；Site zero-write |

`45a34a2` 是 round 分支上的补充 finding 回归，不代表第二名独立 test-author；D-096
门禁中唯一被指定、与实现及 reviewer 隔离的 test-author 是
`/root/test_m1b_review_fixes`。其 `d7a14e6` 经 cherry-pick 成为 `bbf8e97`，其
`f469431` 经 cherry-pick 成为 `5c8ef80`；后者的 tree、parent 与 stable patch-id 已由
reviewer 核对一致。reviewer 未参与实现或测试编写，所有角色串行运行。

## 3. 最终门禁证据

固定行为 SHA：`fcfcc1412b61b20c7ae85078c986182a761ad394`。

| 门禁 | 结果 |
|---|---:|
| `test_material_module_review_fixes.py` + `test_material_module_v1.py` | `34 passed` |
| `pytest -q -rs tests/` | `2102 passed, 4 skipped, 39 warnings` |
| changed-files Ruff `E/F/I --ignore E501` | passed |
| changed-files Ruff format | passed，6 files |
| changed production `compileall` | passed |
| `git diff 46cf364..fcfcc14 --check` | passed |
| exact worktree | clean |

四个 skip 是三个需显式环境变量开启的 networking slow tests，以及一个需显式 Phoenix
executable 的 integration test。39 个 warning 来自既有 TestClient、pytest collection、SOCKS
optional dependency 与 FastAPI lifespan deprecation；本轮没有新增 waiver。

## 4. Reviewer finding disposition

同一 reviewer 对 `c7a218f`、`21f9b6f`、`fcfcc14` 串行复核：

| Finding | disposition | 证据 |
|---|---|---|
| borrowed UoW 下 Site/allowlist 可能 partial write | resolved | `45a34a2` + `a912f99`；adapter 内 savepoint，fault 后 outer UoW 可继续且 Site 不存在 |
| 超大 geometry/sort 数字泄露 `OverflowError` | resolved | `bbf8e97` + `21f9b6f`；统一 `MaterialInvalidInput`、zero-write |
| 相同未知 owner/occupant 在 authority lookup 前误报 cycle | resolved | `5c8ef80` + `fcfcc14`；先验证 owner 存在，已有 owner self-cycle 仍报 `MaterialConflict` |
| 新增英文 docstring/comment 违反仓库约定 | resolved | `fcfcc14`；改为简体中文 |

最终 reviewer 明确给出 Standards `0B/0NB`、Spec `0B/0NB` 和 ACCEPT。此 ledger 只增加
迁移证据，不修改 production/tests tree；合并前由同一 reviewer 做 ledger-only final
confirmation。

## 5. 架构影响与停止线

```text
MaterialModule validates Backend-shaped Site command
  -> SQLiteMaterialAdapter borrows the runtime-authority UoW
  -> owner/occupant/template + placement invariants
  -> site row + normalized allowlist commit atomically
  -> SiteRecord.to_dict() projects one Backend-shaped read model
```

- `Site.material_uuid` 始终是 owner，`occupied_material_uuid` 才是 occupant；
- Site placement 与 `Material.parent_uuid` composition 保持两条独立关系，但统一执行无环约束；
- SQLite savepoint 只深化 borrowed UoW 内的 repository 原子性，不获得 outer transaction
  ownership；
- `sort_order` 是 owner-local 稳定排序字段，不是 Site identity；
- M2A 可以消费此 Site authority 作为只读校验 port，但本轮没有提前写 MaterialSource schema
  或分配逻辑。

下一步必须先把 M1B 非 squash 本地合入最新 `integration/workflow-task-runtime`，再从更新后的
integration 新开后续 round；不得继续在本分支堆叠 M2A。
