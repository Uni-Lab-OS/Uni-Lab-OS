# Round 02E：纯 Authoring HTTP Interface 修复确认

## 1. 固定对象与最终结论

- 基线：`2aa859535bd75e776080e8c4247e41f0b21e0cec`
- 首次被拒绝候选：`583f6df462d21367a763f33062eb29e1f9166d42`
- finding tests：`9134d86`
- production 修复：`d242f23`
- 最终候选：`981c7d65790d2a3112bf98aa91c6229dc669bf1e`
- implementation 比较命令：`git diff 583f6df...981c7d6`
- reviewer：`round02e_review`；与首次评审相同，未参与实现或测试编写。

review worktree 的 code/test HEAD `a9930fb` 是上述 finding、fix 与文档格式提交的顺序
cherry-pick；`git diff 981c7d6 a9930fb` 为空，确认两棵树内容等价。本次测试和只读 probe
均直接在 implementation worktree 的精确 `981c7d6` 上运行。

最终结论：首次评审的 **S-B01/P-B01 已 accepted-fixed**。Standards blocking 0、
non-blocking 1；Spec blocking 0、non-blocking 0。**允许精确候选 `981c7d6` 在完整
round gate 全绿并被 ledger 记录后，以非 squash 方式本地合并。**

## 2. reviewer 验证证据

| 检查 | 结果 |
|---|---|
| `pytest -q tests/app/test_authoring_transform_api.py` | `44 passed, 1 warning in 1.08s` |
| 02E + 02D engine/round-trip/review regression + D-102 历史契约目标 | `118 passed, 1 warning in 2.54s` |
| generate/validate “graph 已变化且 changeset 精确描述变化”只读 probe | 两路均为 sanitized 500，变更值未出现在 response |
| `ruff check --select E,F,I`：API、新 validation module、02E tests | 通过 |
| `git diff --check 583f6df...981c7d6` | 通过 |
| implementation worktree status | 精确 `981c7d6` 且干净 |

118 项命令为：

```text
/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q \
  tests/app/test_authoring_transform_api.py \
  tests/workflow/test_authoring_engine.py \
  tests/workflow/test_authoring_roundtrip.py \
  tests/workflow/test_authoring_engine_review_regressions.py \
  tests/workflow/test_phase01_review_contract_round14_followup.py::test_source_map_uses_one_based_utf16_code_unit_columns
```

warning 仅为既有 FastAPI TestClient/httpx 弃用提示。

## 3. Standards 轴 disposition

### S-B01：公开 Candidate graph 未执行 Backend-shaped fail-closed

**accepted-fixed。** 新增
`unilabos/workflow/candidate_validation.py` 是 transport-independent deep module：它只
依赖 Workflow graph/model/JSON 纯模块，不依赖 FastAPI、`WorkflowService`、Store、Draft、
Task/Job、Scheduler 或 device。

当前成功 bundle 在出站前完成：

1. graph 顶层五集合及 Workflow、Node、Edge、NodeTemplate、HandleTemplate closed-field
   检查；未知内部字段不再原样透传；
2. 请求 `workflow_uuid/revision` 与 Candidate/base graph identity 对齐；Node owner、UUID
   唯一性、Catalog 最小 projection/Handle parent 及完整 `validate_graph()` 检查；
3. 每个 source-map `workflow_node_uuid` 必须属于 Candidate Node；
4. changeset 的 create/update/delete 集合、集合互斥、reserved metadata flag 与
   `graph/source_only` kind 必须精确描述 base→Candidate 差异；
5. generate-python 与 validate 额外要求 Candidate graph 与请求 graph 完全相同，即使
   engine 返回一个能正确描述 mutation 的 `kind=graph` changeset 也拒绝；
6. 任一非法 engine 成功结果由 HTTP seam 统一收敛为固定 `500 internal_error`，response
   不包含私有字段、外来 UUID、revision、changeset 内容或 engine 异常文本。

`workflow_api.py` 只把请求 identity、base graph 与 unchanged policy 传入该纯模块；三条
handler 仍各调用对应 engine 一次，没有重新依赖 persistent Service。因此原 mandatory
Backend-shaped wire boundary 已关闭。

### S-NB01：pure HTTP 与 persistent Service 的 Candidate 验证仍有重复

**保留 non-blocking。** HTTP 已从 handler 内的近似检查深化为独立
`candidate_validation` module，但 `WorkflowService._backend_candidate_graph()`、
`_validate_candidate_bundle_semantics()` 和 diagnostic range 枚举尚未复用该模块。

这不再使 02E fail-open，也不影响本轮纯路由交付；但两条 seam 后续仍可能漂移。建议在
02G persistent composition/Apply 接入前，把 closed entity、source-map membership 与
changeset proof 的共同纯内核迁到该模块，并保留 Service 独有的 Backend read hydration、
Candidate hash/Apply 检查。此项不阻塞 02E 合并。

## 4. Spec 轴 disposition

### P-B01：非法成功 bundle 未按冻结合同转为 500

**accepted-fixed。** 独立 finding suite 把 02E 文件从 31 扩为 44 个 pytest case，逐项
证明：

- 非五集合 graph root、未知 Workflow 字段、未知 Node 字段为 sanitized 500；
- 外来 Workflow UUID、错误 revision 为 sanitized 500；
- source map 指向 Candidate 外 Node 为 sanitized 500；
- compile 的 Node create/update/delete changeset 与实际 graph 不一致均为 500；
- generate-python、validate 的非空 source-only lifecycle 或 graph mutation 均为 500；
- response 不回显上述私有值或 identity；
- 一个 well-formed HTTP 请求携带 semantic 坏 graph 时，真实 transform diagnostic 边界
  仍保持 HTTP 200 data，而不是被错误升级为 request 400；
- 真实 engine 的 generate→compile→validate round trip、Node UUID、normalized source
  与持久表零写入证明继续全绿。

因此非法 **engine output** 的 500 与坏 **request graph semantic** 的 200 diagnostic
仍被正确区分，02E 冻结的 envelope/纯转换语义没有回归。

## 5. 范围与合并门确认

`583f6df...981c7d6` 的 production 变化只有 pure HTTP adapter 调用和新的
`candidate_validation` deep module；没有 production server 安装、默认 Catalog/Authority
composition 或 persistent Draft/Apply wiring。Store schema、Task/Job、Scheduler、device
dispatch 均未修改；Backend 与 Frontend 也未修改。因此 02G、FE 独立分支及 Backend 只读
边界保持。

本 reviewer gate 允许合并，但仓库 mandatory round gate 仍要求主代理在精确
`981c7d6` 上记录完整 `tests/`、configured lint/static、format 与 `git diff --check`
全绿。满足后可合并；若生产代码或测试再变更，必须重新固定候选并复审。

## 6. 最终双轴计数

- Standards：Blocking `0`；Non-blocking `1`（S-NB01，建议 02G 前关闭）。
- Spec：Blocking `0`；Non-blocking `0`。
- 合并结论：**允许**，对象仅限精确 `981c7d6` 及其不改变 code/test 的评审/趋势文档提交。
