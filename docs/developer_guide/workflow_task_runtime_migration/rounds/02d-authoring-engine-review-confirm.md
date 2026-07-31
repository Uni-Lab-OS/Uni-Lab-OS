# Round 02D production Authoring engine 修复确认评审

## 1. 固定对象与结论

- 原评审报告提交：`19d5e01c4a000e3f7b42dacb78dc91a5ccd896f4`
- 独立 finding test 提交：`ebc700c`
- 本次固定修复候选：`8f6b288e7c1b821f6af5b79e369f1ac7f70e72ef`
- 修复比较：`git diff 19d5e01...8f6b288`
- reviewer：`round02d_review`，与首次评审相同；未参与实现或测试编写。
- 结论：原 S-B01、P-B01～P-B05 全部 **accepted-fixed**；但复核发现恢复基线 `graph_validation` 后，真实 `device_action` Candidate 无法满足 D-092 的动态 executor 语义，形成新增 Standards/Spec blocking。候选 `8f6b288` **仍不允许合并**。

## 2. 验证证据

### 2.1 reviewer 本次实际运行

| 检查 | 结果 |
|---|---|
| `pytest -q tests/workflow/test_authoring_engine_review_regressions.py tests/workflow/test_authoring_engine.py tests/workflow/test_authoring_roundtrip.py` | `71 passed, 1 warning` |
| reserved selector import alias 反例：Catalog class `lab.devices:device`，源码 alias 为 `DeviceClass` | compile 成功；normalized source 生成 `from lab.devices import device as device_2`，normalized proof 成功 |
| 真实 device action 反例：把 action templates 的 `node_type` 改为 `device_action`，使用标准 unbound `device()` source | compile 返回 `candidate_invalid`；稳定复现新增 blocking |

### 2.2 主代理提供的 gate 证据

主代理报告已在精确 SHA `8f6b288e7c1b821f6af5b79e369f1ac7f70e72ef` 运行完整 gate：`1568 passed, 3 skipped`，Ruff、format 和 `git diff --check` 全绿。该结果仅作为主代理证据记录，**不是 reviewer 本次运行冒充的结果**。现有全绿没有覆盖 `node_type=device_action + material_uuid=None` 的 production 语义反例。

## 3. 原 Standards finding disposition

### S-B01：UUIDv4 与机器可执行重复-anchor repair

**accepted-fixed。**

- `authoring_engine.py` 对无 anchor 的新 persisted Node 使用真实 UUIDv4；调用方写回 normalized source 后由 anchor 保持 identity。
- `CandidateDiagnostic` 增加 closed、typed 的 `duplicate_uuid`、`occurrence_ranges` 和 `repair_alternatives`；replacement 强制 UUIDv4，alternative 覆盖每个可保留 occurrence，并禁止任意 `details` 逃逸字段。
- duplicate diagnostic 包含全部 occurrence range；每个 alternative 明确 retained range，并为其余 occurrence 提供 range 与 fresh replacement UUID。
- `WorkflowService` 的 diagnostic 范围校验覆盖新增的全部嵌套 range。
- 更新后的 02D design 与 D-029/D-030、`AGENTS.md` 一致，不再用 UUIDv5 局部覆盖 mandatory 合同。

### S-NB01：每 action 重建 Catalog/applied index

**accepted-fixed。** `_BuildState` 现在一次构造 `_CatalogIndex` 与 applied-node map，action/group 复用，不再形成原来的重复 detach/index 路径。

## 4. 原 Spec finding disposition

### P-B01：selector 命名依赖 Node 数组顺序

**accepted-fixed。** 实现先收集唯一 `(class_identity, device_id)`，按稳定 total order 分配 selector 名，再建立 Node 映射。双 selector fixture 反转 `graph.nodes` 后，normalized source、source map、changeset 与 diagnostics 均相同。不同 class 占用 authoring marker 名时会生成稳定 import alias，新增 normalized-source 回编译 proof 可证明其 graph 等价。

### P-B02：畸形 anchor-like comment 静默换 identity

**accepted-fixed。** token 扫描先识别保留的 anchor-like 前缀，再要求唯一精确格式；空值、等号前空格、尾随内容、冒号、缺失/重复等号、非法 UUID 均返回带范围的 `invalid_node_anchor`，不产生 Candidate。

### P-B03：坏 graph 泄漏 Python 异常

**accepted-fixed。** 五集合、Workflow 必填 read fields/JSON 形状与数组成员对象形状在深层访问前验证。compile/generate/validate 对缺失 Workflow name 和数组型 metadata 均返回 `candidate_invalid`，不再泄漏 `KeyError`/属性错误。

### P-B04：Template root param fallback 不一致

**accepted-fixed。** `resolve_template_root_param()` 成为 compiler 与 Store 共用的纯 helper，保持 `goal_default -> goal -> {}`；显式 literal 与 Workflow input binding 都会移除相应 fallback provider。测试覆盖三层 fallback 与覆盖路径。

### P-B05：decorator/group 重复 keyword 被静默接受

**accepted-fixed。** 在 dict lowering 前检查原始 keyword 名唯一；`workflow_definition` 与 `group` 的重复 keyword 分别稳定返回其结构化 diagnostic。Action/output 原有重复检查继续通过。

### P-NB01：source-map column 编码单位

**仍为 non-blocking，未在本次修复范围关闭。** 02E/FE adapter 前仍需统一 source map、AST diagnostic、Service validation 与 Monaco 的 column 单位，并补中文/emoji 测试。

## 5. 02D 纯转换边界与提前 02G 接线

相对基线复核确认：

1. `composition.py` 不再默认创建 production engine/Catalog；
2. Phase 01 `WorkflowService` 不再接受 referenced-snapshot Catalog 特例、Authoring name/description Apply 扩展；
3. `store.py` 不再提前持久化 Candidate name/description；
4. 原 production Apply 集成测试已移除；
5. 保留的 Store 修改只是复用同一个纯 template-root fallback helper，行为不变，不构成 02G composition/Apply 接线。

因此 02G 的 production composition、persistent Candidate authority 和 Apply 接入已经正确移出 02D。新增 diagnostic DTO/Service 范围校验是 D-029/D-030 已冻结公共领域合同所需，不引入第二套持久 compiler。

## 6. 新增 blocking：真实 device action 被旧 material_uuid 校验拒绝

### S-B02 / P-B06：D-092 unbound/fixed selector 无法生成真实 `device_action` Candidate

- 位置：`unilabos/workflow/graph_validation.py:153-155`、`unilabos/workflow/authoring_engine.py:1853-1866`；上位合同 `AGENTS.md:407-421`、D-092 `decisions.md:2720-2760`；02D design `§4-5`。
- 复现：使用与 02D fixture 相同的真实 Catalog aggregate，只把 action template 从测试默认的 `node_type=compute` 改为 `node_type=device_action`；源码使用 `reactor: Reactor = device()`，Node 按 D-092 只携带 `workflow_node_template_uuid` 且 `material_uuid=None`。`_validate_built_graph()` 调用共享 `validate_graph()` 后返回 `candidate_invalid`。
- 根因：恢复基线时一并恢复了“每个 device_action 静态图必须预绑 `material_uuid`”的旧校验。D-092 已冻结：unbound selector 在每个 Job admission 时独立选设备；fixed selector只写 `meta_data.unilab.executor_binding`，也不能在 authoring 中滥用 `material_uuid`。现有 02D action fixtures 全用 `node_type=compute`，所以 71 项与完整 gate 均未命中。
- 影响：production Catalog 一旦发布真实 device/resource action NodeType，02D 的核心 `compile` 无法生成任何合法 Candidate；这不是 02G 持久接线问题，而是纯 Python→Graph transform 自身不可用。把它推迟到 02G 会让 02D 声称的 production engine 只能处理测试中的 compute action。
- 最小修复：在 02D 关闭共享纯 graph validation 的 D-092 语义，让未 admission 的 device action 不要求 `material_uuid`；fixed constraint 仍只验证 reserved `executor_binding`。不得恢复 composition、Apply 或数据库写接线。新增至少 unbound/fixed `device_action` 两个 public-engine 回归，并保留 ordinary graph validation 的必要 Handle/schema/provider 检查。

## 7. 最终双轴结论与合并门

### Standards

- Blocking：1（S-B02，新增；与 mandatory D-092 selector/executor 合同冲突）。
- Non-blocking：0（原 S-NB01 已关闭）。

### Spec

- Blocking：1（P-B06，与 S-B02 是同一根因在 Spec 轴的独立判定）。
- Non-blocking：1（P-NB01，column 编码单位延期到 02E/FE 前关闭）。

修复必须只落在共享纯校验及回归测试，不得重新引入 02G composition/Apply 接线。修复后固定新 SHA，重跑 finding+原合同、相关 Workflow regression、完整 gate，并由同一 reviewer 再次确认。当前 `8f6b288e7c1b821f6af5b79e369f1ac7f70e72ef` **不允许非 squash 本地合并**。
