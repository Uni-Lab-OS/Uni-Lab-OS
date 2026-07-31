# Round 02E：纯 Authoring HTTP Interface 趋势与策略报告

日期：2026-08-01

实现分支：`migration/02e-authoring-transform-api`

基线：`2aa8595`

固定 production/test 候选：`981c7d6`

状态：**本轮目标、累积与完整门禁全绿；同一独立 reviewer 最终确认 Standards/Spec
均为 0 blocking，允许非 squash 本地合并。**

## 1. 本轮交付

02E 把 02D 的三个 pure transform 暴露为 OS-only HTTP Interface：

```text
POST /api/v1/authoring/compile
POST /api/v1/authoring/generate-python
POST /api/v1/authoring/validate
```

交付内容：

- closed request DTO：`workflow_uuid + revision` 使用 Backend identity，拒绝旧 alias、
  Apply token 与未知字段；
- Backend `code/data/error` envelope：语法/semantic diagnostic 为 HTTP 200，malformed
  request 为 400，Catalog unavailable 为 503，内部失败为不泄漏细节的 500；
- D-101 的 8 MiB body、4096 位外部整数和完整 JSON depth budget 覆盖三条新路由；
- focused router/app injection seam，每次请求只调用一个 02D engine 操作；
- D-102：AST byte、tokenize/SyntaxError code point、renderer 与 persistent Service
  统一输出一基、end-exclusive UTF-16 code-unit 坐标；
- transport-independent Candidate bundle validator，关闭五集合、closed entity、请求
  identity、graph semantic、source-map Node membership 与 changeset 生命周期；
- generate/validate 必须返回完全相同 graph 与空 `source_only` changeset；
- pure response 不签发 `candidate_hash`，不具备 persistent Apply authority。

本轮未把 engine 接入进程级 production composition；该动作仍属于后续 composition
切片。未修改 Draft/Apply、Store schema、Task/Job、Scheduler/device、Backend 或 Frontend。

## 2. 独立 TDD 与评审 provenance

| 阶段 | 唯一 subagent | 提交 | 结果 |
|---|---|---|---|
| 原始合同测试 | `round02e_test` | `36921b6` | 31 RED：28 个缺 HTTP seam，3 个旧坐标单位 |
| 历史合同迁移 | 同一 test-author | `583f6df` | 将 1 个 UTF-8 byte 测试迁为 D-102 UTF-16，原断言保留 |
| 首次双轴评审 | `round02e_review` | `9e8db45` | Standards 1B/1NB；Spec 1B/0NB，同一出站 bundle 根因 |
| finding tests | 同一 test-author | `9134d86` | 新增 13 项；11 精确 RED、2 个既有 guard GREEN |
| 修复确认 | 同一 reviewer | `7afe833` | 44 + 118 全绿；B=0，保留 1 个 Standards NB |

测试作者和 reviewer 各只有一位，finding/confirm 由原角色继续；没有并发 reviewer，也
没有让实现者修改独立测试来制造 GREEN。

## 3. 实现与测试规模

相对 `2aa8595...981c7d6` 的 production/test 净变化：

| 类别 | 文件数 | 新增 | 删除 | 净增 |
|---|---:|---:|---:|---:|
| Production | 5 | 940 | 49 | 891 |
| Tests | 2 | 1048 | 2 | 1046 |
| 合计 | 7 | 1988 | 51 | 1937 |

Production 分布：

- `workflow_api.py`：+300/−4，包含 closed DTO、三个 handler、统一 envelope 与出站
  DTO 收紧；
- `candidate_validation.py`：+476，新建深 Module，集中 graph/bundle fail-closed；
- `source_coordinates.py`：+99，新建 UTF-16 坐标深 Module；
- `authoring_engine.py`：+61/−17，只把四类内部坐标接入公共 helper；
- `service.py`：+4/−28，删除旧 UTF-8 byte range helper，复用公共坐标模块。

与 02D 的 production +2890/−14 相比，本轮降到 +940/−49，且 route 本身没有复制
compiler。初次实现只有 +428/−49；评审后增加的 512 行主要是防止内部 graph/Canonical
字段成为公开 wire contract 的 bundle validator，而不是增加新产品功能。

测试/production 新增行比约为 1.11，finding test 让原 31 项增加到 44 项。这个比例与
HTTP 信任边界的风险相符，但也提示下轮不能继续在多个 seam 复制 Candidate 验证。

## 4. 最终门禁

精确候选 `981c7d6`：

```text
02E tests：                                  44 passed
02E + 02D + D-102 相关目标：                 118 passed
tests/workflow：                             769 passed
完整 tests/：                                1614 passed, 3 skipped
修改 Python 文件 Ruff E/F/I：                passed
新增 validator/coordinate/tests 完整 Ruff：  passed
Ruff format --check：                        7 files already formatted
git diff 2aa8595...981c7d6 --check：          passed
```

3 个 skip 与 18 个 warning 均为既有 optional dependency、pytest collection、FastAPI
lifespan/TestClient 提示；本轮没有新增 warning。确认报告是候选之后的 doc-only 提交，
不改变被测试/评审的 production/test SHA。

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

02E 初次 blocking 从 02D 的 6 个降为 1 个，且不是新产品 grill，而是 HTTP trust
boundary 漏掉出站 Candidate graph fail-closed。11 个 RED 把它拆成 graph shape/private
field、Workflow identity、source-map membership、compile lifecycle 与
generate/validate immutability 五类，随后一次修复全部关闭。

因此总体问题在明显变少：compiler 大边界在 02D 集中暴露后，薄 adapter 只新增一个
共同根因；D-102 直接关闭了上一轮唯一 Spec non-blocking，没有产生新的交互决策。
最终仍有 1 个维护性 non-blocking，但正确性/安全性 blocking 已归零。

## 6. 遗留问题与重要性

### S-NB01：HTTP 与 persistent Service 的 Candidate 验证重复

重要性：**中等；不阻塞 02E，但应在 02G production composition 前关闭。**

pure HTTP 已使用 `candidate_validation.py`，persistent Service 仍保留一套近似的
diagnostic、graph hydration 与 changeset semantic validation。当前两边均有完整测试，
不会使 02E wire contract 错误；但继续演进会再次产生 reviewer 本轮发现的漂移。

策略：02G 接入真实 engine/Catalog/persistent Candidate authority 时，把两条 seam 的
共同 closed DTO 与 bundle semantic 统一下沉；Service 只保留持久化 hydration、错误码与
事务责任。不要在 02E 为未接线的持久路径做高风险大搬迁。

### production composition 尚未接入

重要性：**计划内阻塞真实 OS server 暴露，不是 02E Interface correctness 缺陷。**

focused app/router 已可用于 FE contract 测试；真实 `setup_server()` 需要明确选择
Graph Authority 并注入其 Catalog engine，属于后续 composition 切片。不能在没有
authority 配置的情况下默认猜 `local` 或 Backend。

## 7. 策略调整

1. 从 02F 起，所有公开成功 DTO 都增加“坏内部 producer”出站 adversarial matrix，
   不能只验证请求入站。
2. `source_map` 与 `changeset` 必须和 graph 一起验证引用/生命周期，不把各字段独立
   Pydantic green 当作 bundle green。
3. 02G 前关闭 S-NB01，共用 `candidate_validation`；避免第三套 Candidate validator。
4. 继续保持 router 只做 transport，compiler、graph semantic 与 persistent transaction
   分别在自己的深 Module。
5. FE 从现在开始另开分支实现单编辑权切换与 pure transform client；先以 focused OS
   contract 联调，不等待 persistent Draft/Apply UI。
6. Backend 继续只读；纯 Authoring 是 D-040 明确的 OS-only exception，不向 Backend
   增加或代理三条 route。

## 8. 前端、Backend 与合并判断

- Frontend：**02E 未修改**；合并后立即在独立 FE 分支启动实现与 contract 联调；
- Backend：**未修改**；
- FE-OS：02E 已提供可注入的 focused HTTP seam，可开始 compile/generate/validate 联调；
- persistent server mount：留在明确的 OS composition Round；
- 最终评审：Standards 0B/1NB，Spec 0B/0NB；
- 合并：允许非 squash 本地合入 `integration/workflow-task-runtime`，不 push。

下一步不等待人工确认：先合并 02E，然后按“前端更新单开分支”的约束创建 FE Round；
OS 后续切片仍从最新 integration 新开分支。
