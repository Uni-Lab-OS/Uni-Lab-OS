# Round 02D：production Authoring engine 趋势与策略报告

日期：2026-08-01

实现分支：`migration/02d-authoring-engine`

基线：`eaa8f05`

固定 production/test 候选：`da33efa`

状态：**production、独立测试与完整质量门已全绿；同一独立 reviewer 在
`9780ec4` 最终确认 Standards 0 blocking、Spec 0 blocking，允许合并。**

## 1. 本轮交付与边界

02D 把 02A/02B 的冻结 schema、02C 的 immutable Catalog snapshot 与 authoring
source 连接为纯转换 Module：

```text
Python source + authority-scoped Catalog snapshot
                     │
                     ▼
     compile / generate_python / validate
                     │
          ┌──────────┴──────────┐
          ▼                     ▼
 structured diagnostics   Candidate graph + normalized source
                                  │
                                  ▼
                       proof compile / graph equivalence
```

本轮完成：

- Python AST 到 Workflow/Node/Edge/HandleBinding 的确定性编译；
- Graph 到 canonical Python 的生成，以及生成后递归 proof compile；
- authority-scoped Catalog、模板根参数 fallback 与显式 input/output binding；
- Node anchor 身份保持、新 Node UUIDv4 分配、重复 anchor 的结构化多方案修复；
- reserved device selector、固定 `executor_binding` 与 unbound selector；
- selector/import alias 与 graph 数组顺序无关的稳定命名；
- malformed anchor、坏 graph、重复 keyword 等稳定 diagnostic；
- source map、changeset、normalized source 与 graph 等价验证；
- `device_action` 不再错误要求 authoring 阶段的 `material_uuid`，符合 D-092 的
  Scheduler admission 边界。

本轮刻意没有接入 persistent Draft/Apply、production composition、数据库 Candidate
authority、SSE、Frontend 或 Backend。评审中发现的提前 02G 接线已经移除；02D
最终只交付纯转换能力与其公共 DTO/校验支撑。

## 2. 独立 TDD 与评审 provenance

| 阶段 | 唯一 subagent | 提交 | 结果 |
|---|---|---|---|
| 原始合同测试 | `round02d_test` | `f918fbe`、`96e5706` | 47 个独立 RED；实现后原合同全绿 |
| 首次双轴评审 | `round02d_review` | `19d5e01` | 1 个 Standards blocking、5 个 Spec blocking、2 个 non-blocking |
| finding tests | 同一 test author | `ebc700c` | 15 个精确 RED，覆盖六项 blocking |
| 修复确认 | 同一 reviewer | `4dd304c` | 原六项 accepted-fixed；发现 1 个共同根因的新 Standards/Spec blocking |
| 真实 device action 测试 | 同一 test author | `6ab3c01` | 修复前 71 passed、2 failed |
| 最终确认 | 同一 reviewer | `9780ec4` | 73 passed；D-092 accepted-fixed，原六项不回归，02G 边界保持 |

测试和评审始终各只有一个独立 subagent；后续 finding 与确认由原角色继续，未用新增
agent 稀释责任边界。

## 3. 实现与测试规模

相对 `eaa8f05...da33efa` 的 production/test 净变化：

| 类别 | 文件数 | 新增 | 删除 | 净增 |
|---|---:|---:|---:|---:|
| Production | 6 | 2890 | 14 | 2876 |
| Tests | 3 | 1978 | 0 | 1978 |
| 合计 | 9 | 4868 | 14 | 4854 |

Production 的主要增量是 `authoring_engine.py` 的 2681 行；其余 195 行分布于 public
facade、DTO、共享 graph validation、Service diagnostic range validation 与纯
template-root fallback helper。测试/production 行数比约为 0.68。

这次规模明显高于 02C，原因是单一切片同时覆盖双向 AST/Graph lowering、identity、
diagnostic、source map 与 proof round-trip。公开 caller surface 仍收敛为
`compile/generate_python/validate`，没有把内部 AST visitor、selector index 或 repair
算法外泄为第二套 API。后续 02E 不应复制这些规则，只做 HTTP adapter。

## 4. 测试与质量门

固定候选 `da33efa` 的结果：

```text
02D 原合同 + review findings + device action： 73 passed
tests/workflow：                              769 passed
完整 tests/：                                 1570 passed, 3 skipped
修改 Python 文件 Ruff E/F/I：                 passed
新增 engine/tests 完整 Ruff：                 passed
Ruff format --check：                         9 files already formatted
git diff eaa8f05...da33efa --check：           passed
```

3 个 skip 与 18 个 warning 均为既有可选依赖、pytest collection、FastAPI lifespan/
TestClient 提示；本轮没有新增 warning。worktree 在候选提交后保持 clean。

## 5. 问题趋势

| Round | 初次 review blocking | 初次 non-blocking | 最终 blocking | 最终 non-blocking |
|---|---:|---:|---:|---:|
| 02B1 | 7 | 1 | 0 | 1 |
| 02B2 | 2 | 1 | 0 | 1 |
| 02B3 | 4 | 1 | 0 | 0 |
| 02B completion | 2 | 1 | 0 | 0 |
| 02C | 3 | 3 | 0 | 2 |
| 02D | 6 | 2 | 0 | 1 |

02D 初次 blocking 增加到 6，说明跨入完整 compiler boundary 后暴露面明显扩大；这不是
产品语义继续发散。六项都能映射到已冻结决定：UUIDv4/重复 anchor repair、顺序无关
命名、malformed token、坏 graph fail-closed、template-root fallback、重复 keyword。

六项关闭后，同一 reviewer 又发现 1 个被便利 fixture 遮蔽的共同根因：测试 action
模板一直使用 `node_type=compute`，因此旧 `graph_validation` 对真实
`node_type=device_action` 强制 `material_uuid` 的规则未被执行。加入真实类型 fixture
后先得到 2 个 RED，再以 3 行净减少关闭；没有新增 grill 或持久接口设计。

因此本轮呈现“先增加、再快速归零”的漏斗：发现数量较上一轮上升，但每个问题都形成
独立 RED、修复并由同一 reviewer 复核，最终只剩 1 个已定界的非阻塞互操作问题。
整体仍在收敛，下一轮重点从 compiler 语义正确性转向薄 HTTP adapter。

## 6. 遗留问题与重要性

### P-NB01：source-map column 编码单位

重要性：**中高；不阻塞 02D 纯 engine 合并，但阻塞把 source range 宣称为 FE 可直接
消费。**

Python AST、OS DTO 与 Monaco 对 Unicode code point/UTF-16 code unit 的 column 定义
尚未统一。ASCII 测试无法发现中文或 emoji 前缀后的偏移。02E 必须冻结 HTTP 契约中的
单位、增加中文/emoji request/response 测试，并在 FE-OS 联调前关闭。

### 02C P-NB01：旧脏库 unique index 启动诊断

重要性：**中高运营风险；仍不属于 02D，在 02G production composition 前关闭。**

### 02C S-NB02：Catalog 字段 ledger 重复

重要性：**中等；P0-4 projection 再扩字段前处理。** 02D 通过 immutable snapshot
消费现有字段，没有继续复制持久字段 ledger。

## 7. 本轮后的策略调整

1. compiler/validator 测试 fixture 必须使用真实 `node_type`，不能用 `compute` 代替
   `device_action` 只验证相邻路径。
2. 后续每个切片固定检查更高 authority：根/子目录 `AGENTS.md`、`decisions.md`、
   master plan，再冻结 round design，避免旧 validator 规则压过新决定。
3. permutation、malformed reserved token、坏 graph shape 与 Unicode range matrix 成为
   Authoring adapter 的必测项。
4. 02E 只包装 02D 的三个纯操作，禁止重写 compiler 规则或接入 Draft/Apply；任何
   persistent Candidate authority 继续留在 02G。
5. 02E 增加大 Catalog/大 Workflow 的 adapter 开销预算；02D 已缓存 Catalog/applied
   index，不允许 HTTP 层每请求重复构造隐式第二索引。
6. 02E 完成后立即另开 Frontend 分支，实施“单编辑权按钮切换”和 FE-OS 纯转换联调；
   Frontend 不与 OS 02E 分支混合，Backend 仍不修改。

## 8. 前端、Backend 与合并判断

- Frontend：**本轮未覆盖、未修改**；
- Backend：**未覆盖、未修改**；
- FE-OS 联调：**本轮尚未触发**，因为可调用的 HTTP seam 属于 02E；
- 02G 内容：production composition、persistent Candidate authority、Draft/Apply 仍未
  混入；
- 合并判断：最终 reviewer 为 0 blocking 且同一精确候选完整门禁全绿后，允许非
  squash 本地合入 `integration/workflow-task-runtime`；不 push。

下一 Round 是计划切片 **02E：pure Authoring transform HTTP**。它将直接从最新
integration 新开分支执行，不等待额外确认。
