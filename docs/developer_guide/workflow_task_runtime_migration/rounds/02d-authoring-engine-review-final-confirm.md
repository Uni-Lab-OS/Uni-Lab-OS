# Round 02D production Authoring engine 最终确认

## 1. 固定对象与结论

- 基线：`eaa8f05a5458882141648c54ff1cd8d5a05ed33e`
- 最终候选：`da33efae3a93c08b7201a3d0c7c727d5aba2654e`
- 比较命令：`git diff eaa8f05...da33efa`
- D-092 finding test：`6ab3c01`
- D-092 修复：`da33efa`
- reviewer：`round02d_review`，与前两次评审相同，未参与实现或测试编写。
- 最终结论：新发现的 D-092 blocking 已关闭；原 S-B01、P-B01～P-B05 继续保持关闭；02G persistent wiring 未重新混入。**Standards blocking 0，Spec blocking 0，允许在精确 SHA 的完整 round gate 全绿后非 squash 本地合并。**

## 2. reviewer 实际验证

运行：

```text
/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q \
  tests/workflow/test_authoring_engine.py \
  tests/workflow/test_authoring_roundtrip.py \
  tests/workflow/test_authoring_engine_review_regressions.py
```

结果：`73 passed, 1 warning in 2.08s`。warning 仅为既有 FastAPI TestClient/httpx 弃用提示。

工作树检查时 HEAD 精确为 `da33efae3a93c08b7201a3d0c7c727d5aba2654e` 且干净。本确认没有修改 production、测试、Backend 或 Frontend。

## 3. D-092 blocking 最终 disposition

### S-B02 / P-B06：真实 device action 被旧 material_uuid 校验拒绝

**accepted-fixed。**

- `graph_validation.py` 不再要求尚未 admission 的 `device_action` 静态绑定 `material_uuid`；Handle、schema、provider、execution policy 等共享纯 graph 校验保持不变。
- 新回归把真实 action template 投影为 `node_type=device_action`，分别覆盖 unbound `device()` 与 fixed `device("reactor-1")`。
- 两种 Candidate 都能通过 production engine 的 compile、normalized-source proof 和 graph validation。
- 两种 Node 都保持 `material_uuid=None`，没有增加非 Backend 顶层 `device_id`。
- unbound selector 不写 executor constraint；fixed selector只写：

```text
meta_data.unilab.executor_binding =
  {"mode": "fixed", "device_id": "reactor-1"}
```

这与 D-092/`AGENTS.md` 的 Scheduler admission ownership 一致，也没有恢复任何持久 Apply 行为。

## 4. 原六项防回归确认

| Finding | 最终状态 | 当前证据 |
|---|---|---|
| S-B01 UUIDv4 与机器修复 DTO | 保持关闭 | 新 Node UUIDv4；duplicate occurrence/alternative/fresh UUIDv4 closed DTO 测试通过 |
| P-B01 selector permutation | 保持关闭 | 多 selector 反转 `graph.nodes` 后 normalized source/source map/changeset 一致 |
| P-B02 malformed anchor | 保持关闭 | 7 种 anchor-like 畸形输入均失败关闭 |
| P-B03 bad graph diagnostic | 保持关闭 | 三个公开 transform 对缺字段/错误 metadata 均返回 diagnostic |
| P-B04 template fallback | 保持关闭 | `goal_default -> goal -> {}` 与显式/binding override 测试通过 |
| P-B05 duplicate keyword | 保持关闭 | workflow decorator/group 重复 keyword 均被拒绝 |

新增 normalized-source 回编译 proof、selector import alias、Catalog snapshot、顺序/group/parallel、双向 round-trip 与 P0-4/P1-2 停止线的原合同均在 73 项中继续通过。

## 5. 02D/02G 边界

`eaa8f05...da33efa` 的变更清单不含 `unilabos/workflow/composition.py`。相对基线检查确认：

1. 没有默认 production engine/Catalog composition；
2. 没有 persistent Candidate authority、Apply name/description 或 referenced-snapshot Catalog 特例；
3. 没有 production Apply 集成测试；
4. `service.py` 仅扩展 D-030 structured diagnostic 的嵌套 source-range 校验；
5. `store.py` 仅复用行为等价的纯 template root-param fallback helper；
6. `graph_validation.py` 的 D-092 修改是 Authoring/普通 graph 共用的纯语义校验，不是 02G persistent wiring。

因此 persistent Authoring 接入仍归 02G，本轮只交付 pure production Authoring engine 及其所需的冻结领域合同。

## 6. 最终双轴结果

### Standards

- Blocking：0。
- Non-blocking：0。原性能项 S-NB01 已通过一次性 Catalog/applied index 关闭。

### Spec

- Blocking：0。
- Non-blocking：1。P-NB01 source-map column 编码单位仍按前次报告延期到 02E/FE adapter 前冻结；它不影响本轮 AST/graph identity、Apply 边界或运行正确性，不阻塞 02D 合并。

本 review gate 已允许候选继续合并流程。仓库 round gate 仍要求主代理在**精确 SHA `da33efa`**记录完整测试、configured lint/static、format 与 `git diff --check` 全绿；满足后可将 02D 以保留 provenance 的非 squash 方式本地合并到 `integration/workflow-task-runtime`。
