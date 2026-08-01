# Round 02G1：local Authority Candidate round-trip 趋势与策略报告

## 1. Round 结论

Round 02G1 已达到合并门：固定生产候选
`e60451ada0cfdd06acecbfd2a960e8eb52fa843b` 通过独立 RED 测试、focused/累积/完整
测试、changed-files Ruff/format、diff check 和唯一独立 reviewer 的 Standards/Spec 双轴
审查，最终 **0 blocking / 0 non-blocking**。

本轮是 FE-D117 使用真实 production OS 联调时发现的 02G 集成修正，不是既定
`02H — Task input preflight` 的改名。缺陷表现为：persistent Authoring aggregate 中由
OS 自己签发的 Candidate graph 原样提交给 pure `generate-python`，却被错误判为
`template_catalog_mismatch`，使 D-117 画布模式无法进入。

根因不是 Candidate identity 漂移，而是两个合法 wire shape 没有在同一比较边界汇合：

```text
TemplateCatalog snapshot：可空字段存在且值为 null
Backend read DTO：        同一字段按 omitempty 省略
旧 generate proof：       对两个完整 JSON 对象做 strict compare -> 误判
```

修复只把九个冻结的 Catalog nullable read fields 的 `null` 与省略规范为同一读取形状；
Template/Handle UUID、非空字段、未知字段、Workflow/Node/Edge、input/output binding 和
不可表示图仍完整 fail closed。

## 2. 来源、分支与独立角色

| 项目 | 值 |
|---|---|
| 基线 | `integration/workflow-task-runtime@01380449868ccf334f4da1a28c7f6f946fb540d1` |
| 实现分支 | `migration/02g1-local-authority-roundtrip` |
| 设计提交 | `d3ccf35` |
| 独立 test-author | `round02g1_test` |
| tests-only 原提交 | `47773a5` |
| tests-only 保留来源后的提交 | `d019ebf` |
| 生产候选 | `e60451ada0cfdd06acecbfd2a960e8eb52fa843b` |
| 独立 reviewer | `round02g1_review` |
| review 结论 | Standards `0B/0NB`；Spec `0B/0NB` |

任一时刻只有一个 subagent 工作；test-author 完成并提交 RED 后才开始生产实现，reviewer
只在候选测试完成且工作树干净后启动。未 squash test provenance，未 push。

## 3. RED、GREEN 与门禁证据

### 3.1 独立 RED

test-author 新增真实 local Authority engine 合同和 production composition HTTP 合同：

```text
tests/workflow/test_local_authority_roundtrip_contract.py
tests/app/test_persistent_authoring_production.py
```

在 `0138044`/`d3ccf35` 上：

```text
1 failed, 10 passed
```

唯一失败经过真实 local `CatalogAuthority`、SQLite `TemplateCatalog`、production
`WorkflowAuthoringEngine`、persistent aggregate GET 和 public pure
`POST /api/v1/authoring/generate-python`。其余 10 项已证明：caller UUID 被拒、Catalog
分配 UUIDv4、直接 engine Candidate 可自 round-trip、scalar default=4、Node input
binding、Edge Handle、root output binding 均使用 server-owned identity，foreign Catalog
和真正不可表示 graph 继续失败。因此 RED 不是坏 fixture 或宽松预期。

### 3.2 修复后验证

| 门禁 | 结果 |
|---|---:|
| 本轮 focused | `11 passed` |
| Authoring/Catalog/source lifecycle/pure+persistent 累积 | `270 passed` |
| 完整 `pytest -q tests` | `1706 passed, 3 skipped, 35 warnings` |
| changed-files Ruff | passed |
| changed-files Ruff format | passed |
| `git diff 0138044 --check` | passed |
| reviewer 补充变异探针 | 7 组预期结果全部通过 |

reviewer 的补充变异探针确认：

- nullable `null -> omitted` 成功；
- unknown Node/Handle 字段、required=null、nullable 非空篡改、Handle UUID 篡改均为
  `template_catalog_mismatch`；
- input binding 篡改为 `candidate_invalid`；
- output binding 篡改为 `round_trip_mismatch`。

误运行仓库根 `pytest -q` 时会额外收集硬件示例，出现 Modbus 构造和 cameraUSB import 两个
既有 collection error；正式仓库门禁按 `AGENTS.md` 明确使用 `pytest tests/`。全仓 Ruff
critical 仍有 77 个既有脚本/驱动诊断；本轮 changed files 为 0，新诊断为 0。两者均未
夹带到 Authoring 修复。

## 4. 实现规模

相对 `0138044`、不含本报告：

| 类别 | 文件数 | 新增 | 删除 |
|---|---:|---:|---:|
| 生产实现 | 1 | 79 | 8 |
| 独立测试 | 2 | 312 | 0 |
| 设计文档 | 1 | 64 | 0 |
| 合计 | 4 | 455 | 8 |

生产改动只在 `unilabos/workflow/authoring_engine.py`：一个读取形状规范化 helper、两个
冻结 nullable 字段集合，以及 Catalog projection/semantic round-trip 两个既有比较点。
没有新增 route、DTO、持久表、状态机或并行 Authority。

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
| 02G1 | 0 | 0 | 0 | 0 |

02G1 在 review 前由真实 FE-OS 联调和独立 test-author 找到 **1 个**跨模块集成根因，
review 没有再发现新问题。与 02G 的 3 个 review 根因相比，新增问题数从 3 降到 1，且
一次修复后归零：

```text
真实 FE-OS 联调：1 个 read-shape / snapshot-shape 缝隙
  -> 独立最小 RED：1 failed, 10 passed
  -> 修复：11 passed
  -> reviewer：0B/0NB
```

因此不能说“02G 后再也没有新问题”，但整体仍在收敛：新问题来自把已完成模块第一次
放进真实前端消费链，而不是新增 public DTO、持久模型或 grill 决策。问题类别也从大型
Authority/transaction/composition 缺口收缩为单一 wire-shape 兼容缝隙。

## 6. 前端、Backend 与遗留问题

### Frontend

本轮 **没有修改 Frontend**，但缺陷由 FE-D117 的真实浏览器/production OS 联调发现。
FE-D117 独立分支已完成模式按钮、单编辑权、只读投影、dirty guard、完整 Python diff、
Draft 双 CAS、Apply 单 token 和 SSE 接线；在 OS 修复合入 integration 后恢复其真实 E2E、
评审、报告和合并。重要性：**高，当前下一步**。

### Backend

本轮 **没有修改 Backend**；冻结 Backend `09609a2` 继续只读。重要性：无本轮遗留。

### Catalog 部署 importer

真实部署仍需要外置 Registry importer 调用 02C `TemplateCatalog.replace()` 发布 local
Catalog。本轮和 FE E2E 使用明确 seed，不代表部署 importer 已接通。重要性：**高的
integration dependency**，但不是本轮 round-trip 修复 blocking。

### 仓库测试基础设施债务

裸仓库根 Pytest 会收集硬件示例；全仓 Ruff critical 有 77 个既有诊断。重要性：**低到
中**；应由独立维护 Round 处理，不能污染 Workflow migration 切片。

## 7. 策略调整与下一步

从本轮开始，Authoring 每个跨层切片增加一类强制组合门：

```text
producer public read DTO
  -> 不经私有修补原样送入对应 pure/write Interface
  -> 验证 identity、语义和 DTO omitempty 闭环
```

具体执行调整：

1. 不再只用 engine 自产 write-shape Candidate 证明 round-trip；必须同时测试 persistent
   aggregate 的 Backend read-shape Candidate。
2. FE-OS 联调保留 production app、真实 SQLite/Catalog/compiler/SSE，不用 route mock；
   它是组合缺陷发现门，不只是前端截图门。
3. 发现 OS 缺陷时插入来源明确的独立修正 Round，不在 FE 增加兼容生成器，也不改写既定
   02H 范围。
4. 02H 已在 02G1 验证期间从同一 `0138044` 基线独立完成门禁并先行合入 integration；
   02G1 合入最新 integration 后立即恢复 FE-D117，不重复执行 02H。

本报告提交后，Round 02G1 可非 squash 本地合入已含 02H 的
`integration/workflow-task-runtime`；不 push。两个分支均从 `0138044` 开始且修改面不重叠，
合入后仍需执行 02G1 focused 回归，证明合并结果而非单分支候选。
