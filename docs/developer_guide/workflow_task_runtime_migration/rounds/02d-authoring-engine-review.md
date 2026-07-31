# Round 02D production Authoring engine 独立评审

## 1. 固定对象与结论

- 基线：`eaa8f05a5458882141648c54ff1cd8d5a05ed33e`
- 被评审且已测试的候选：`d230884f06bd371a7405ae992aa7b60f897d76fd`
- 比较命令：`git diff eaa8f05...d230884`
- reviewer：`round02d_review`；未参与 02D 实现或独立测试编写。
- 轮换视角：regression/security，同时完成 Standards 与 Spec 两轴。
- 结论：**不允许合并**。Standards 有 1 个阻塞，Spec 有 5 个阻塞；修复后必须生成新候选 SHA、重跑完整 gate，并由同一 reviewer 复审。

本轮保持了一个较深的公开模块：外部只有 `WorkflowAuthoringEngine` 三个转换入口，未复制 public Canonical DTO，也未修改 Backend/Frontend。Catalog snapshot、Apply 的 `Catalog -> Store` 锁序、候选重编译/hash 对比、Draft 事务内线性化、Workflow 名称/描述与 graph/source 的同事务提交，逐项检查后未发现可复现阻塞。引擎没有 import/执行作者源码，也没有 Draft、网络、Task、Job 或 device dispatch 写入。

## 2. 验证证据

| 检查 | 结果 |
|---|---|
| `pytest -q tests/workflow/test_authoring_engine.py tests/workflow/test_authoring_roundtrip.py` | `51 passed, 1 warning` |
| Workflow、冻结 API、02A/02B 回归（`tests/workflow`、`test_workflow_contract_api.py`、四个 annotation schema suite） | `923 passed, 3 warnings` |
| `ruff check --isolated`：新增 engine/marker 与两份独立测试 | 通过 |
| `ruff format --check`：新增 engine/marker 与两份独立测试 | 4 files already formatted |
| `git diff --check eaa8f05...d230884` | 通过 |

现有绿灯没有覆盖下列反例，因此不能替代代码评审。所有反例均使用规定的 Python 3.11 环境、02D 测试中的真实 `TemplateCatalog`/`WorkflowStore` fixture，在临时数据库运行。

## 3. Standards 轴

### 3.1 Blocking

#### S-B01：实现与仓库 mandatory UUID-anchor 合同互相冲突

- 位置：`AGENTS.md:732-747`；`unilabos/workflow/authoring_engine.py:234-247`；`tests/workflow/test_authoring_engine.py:818-844`；02D 设计 `:148-150`。
- 证据：仓库规则要求新 persisted Node 分配 UUIDv4，并要求重复 UUID diagnostic 包含重复 UUID、每个 source range 及机器可执行替代修复；实现以 AST 结构生成 UUIDv5，测试又把“相同未锚定源码每次得到同一 UUID”冻结为合同。当前 `CandidateDiagnostic` 也只能携带一个可选 range，无法表达 mandatory duplicate repair。
- 影响：这是 mandatory 仓库规则、D-092 既有决策与本轮设计之间的直接冲突。无论选择哪一侧，当前候选都不能声称同时满足 Standards 与 Spec；identity 修复工具也无法按仓库合同服务 FE、CLI/MCP 和 coding agent。
- 最小修复：先明确唯一权威决策。若保留既有合同，则改用 UUIDv4，并扩展结构化重复-anchor diagnostic/测试；若产品明确采用确定性 UUIDv5，必须先以正式决策同步 `AGENTS.md`、`decisions.md` 与 diagnostic 合同，不能只让本轮局部设计静默覆盖 mandatory 规则。

### 3.2 Non-blocking

#### S-NB01：每个 action 重建完整 Catalog 索引和 applied-node map

- 位置：`authoring_engine.py:226-232, 1162-1170, 1275-1277`。
- 证据：`_CatalogIndex(state.snapshot)` 会为每个 action detach 全部 Node/Handle；`state.applied_nodes` 也为每个 Node 重建完整字典。
- 影响：形成 `O(action_count × catalog_size)` 和 `O(action_count × applied_node_count)` 的重复工作。现有小 fixture 不暴露该增长；02E 暴露纯 HTTP 转换后会扩大 CPU/内存攻击面。
- 最小修复：一次 snapshot 构造一个不可变 `_CatalogIndex`，一次构造 applied-node index 并放入 `_BuildState`；在 02E 前补一个有明确预算的宽 Catalog/大 Workflow 回归。

## 4. Spec 轴

### 4.1 Blocking

#### P-B01：Graph→Python selector 命名依赖 `graph.nodes` 数组顺序

- 位置：`authoring_engine.py:2197-2247`；设计 `:203-211, 227-231`。
- 复现：编译两个同一 `Reactor` class、分别为 `device()` 与 `device("r-1")` 的顺序 action；原图可成功编译。只执行 `graph["nodes"].reverse()` 后调用 `generate_python()`，结果从成功变为 `round_trip_mismatch`。原因是先遇到的 selector 获得 `reactor`，后遇到的获得 `reactor_2`，反序后 executor binding 被错误映射到另一组规范名。
- 影响：Node 数组本身不是图语义；同一 Backend-shaped graph 因展示顺序不同而无法生成，违反确定性、detached graph 与双向 round-trip 验收门。Backend/SQLite 返回顺序变化即可触发。
- 最小修复：先按 `(class_identity, device_id)` 的稳定 JSON 顺序分配 selector 名，再独立建立 Node→selector 映射；新增多 selector、同 symbol、不同 fixed binding、Node 数组全排列测试。

#### P-B02：格式错误的 anchor-like comment 被静默当普通注释

- 位置：`authoring_engine.py:962-990`；设计 `:148-150`。
- 复现：把一个有效 anchor 分别改成 `# unilab:node_uuid=`、在 UUID 后加 ` trailing`、或写成 `# unilab:node_uuid = <uuid>`；三种输入都返回 valid Candidate，并为原 statement 分配一个新 UUID，而不是 `invalid_node_anchor`。
- 影响：一次粘贴或轻微格式错误会无提示地删除旧 Node identity、创建新 identity，调试断点/source map 与 changeset 均漂移；违反 raw source edit 必须 fail-closed 的核心约束。
- 最小修复：token 扫描先识别保留前缀 `unilab:node_uuid`，再严格解析完整规范形状；任何保留前缀但不 full-match 的 comment 都返回带定位的 `invalid_node_anchor`。补空值、空格、尾随内容、错误分隔符测试。

#### P-B03：合法方法调用中的坏 graph 会泄漏 Python 异常而非 diagnostic

- 位置：`authoring_engine.py:646-675, 1837-1895, 1968-1971`；设计 `:72-86`。
- 复现：从完整空 graph 删除 `graph["workflow"]["name"]`，以合法 UUID/revision/source URI 调用 `generate_python()`，实际抛出 `KeyError('name')`。类似非对象 `workflow.meta_data` 可产生未归一的属性错误。
- 影响：设计明确要求 graph/round-trip 用户输入错误只返回 `CandidateCompilation.diagnostics`。02E 若适配层漏掉任一深层形状校验，就会产生 500 和实现异常泄漏；CLI/MCP 直接使用领域入口也不稳定。
- 最小修复：在 `_require_graph_identity` 后使用完整冻结 Backend graph/read model 验证 workflow 与五集合成员，或把所有用户 graph 访问转为显式 `_AuthoringFailure`；不要以广泛 `except Exception` 隐藏编程错误。补缺字段、错误容器、非对象成员矩阵。

#### P-B04：Action 默认值没有复用冻结的 Template fallback

- 位置：`authoring_engine.py:1173-1175`；`store.py:839-854`；`AGENTS.md:704-718`；设计 `:139-143`。
- 复现：Catalog template 的 `goal_default={}`、`goal={"cycles": 3}`，源码省略 required `cycles`；普通 full-graph Store 合同会回退非空 `goal`，02D compiler 却只读取 `goal_default`，返回 `candidate_invalid`。
- 影响：同一 Backend template 在 Authoring 与 graph PUT 得到不同持久语义，违反“required/default/type 最终证明复用 Catalog 合同”；有效工作流无法通过 Compile Preview。
- 最小修复：抽取并复用现有 `goal_default -> goal -> {}` 的单一纯 helper，而非在 compiler 复制另一套默认算法；补三层 fallback、显式 keyword 覆盖和 binding 移除默认值测试。

#### P-B05：decorator/group 的重复 keyword 被接受并静默归一

- 位置：`authoring_engine.py:840-868, 1254-1269`；Python 模块合同设计 `:89-117, 157-170`。
- 复现：`@workflow_definition(workflow_uuid="wrong", workflow_uuid="right", displayname=...)` 和 `group(name="Ignored", name="Used")` 均被 `ast.parse` 接受，当前 dict comprehension 采用后值并返回 valid Candidate；Python `compile()` 本应以重复 keyword 拒绝该源码。
- 影响：Compile Preview 接受并悄悄改写一个 Python 语义上非法且作者意图有歧义的 Draft，违反有限静态语法和失败关闭原则。
- 最小修复：在构造 keyword dict 前检查原始 keyword 名称非空且唯一；workflow decorator、group 与所有 marker 共享一个严格 keyword helper，并补重复字段测试。

### 4.2 Non-blocking

#### P-NB01：source-map column 的编码单位尚未冻结

- 位置：`authoring_engine.py:1954-1964`；设计 `:213-215`。
- 证据：`with group(name='样品制备'):` 的 entry `end_column=29`（Unicode 字符计数），同一行 UTF-8 端点为 37；diagnostic 的 AST column 与 Service 范围校验又按 UTF-8 byte 长度处理。
- 影响：当前模型未声明 column 是 Unicode code point、UTF-8 byte 还是 FE/Monaco 的 UTF-16 code unit。尚不能仅据此判定实现错误，但 FE runtime highlighting 接入后会出现多字节文本偏移风险。
- 最小修复：02E DTO 前冻结一个单位并由 source-map、diagnostic、Service 校验和 FE adapter 统一转换；补中文与 emoji 范围测试。

## 5. 已确认通过的边界与后续复审门

以下重点未发现阻塞：

1. AST-only 路径不 import/decorate/执行作者源码；动态调用、条件、循环、async 等保持 P1-2 停止线。
2. 每个转换在一个 authority-scoped immutable 02C snapshot 中完成；外层 snapshot 可复用，结果 detached，未知/外来 Catalog projection 失败关闭。
3. `generate_python` 会回编译并比较完整 graph；`validate` 同时证明 source 与 graph。P-B01 是该证明前的 selector 命名不确定性，而不是跳过证明。
4. Apply 只接受 server-issued Candidate hash；Apply 前重编译并比对 Candidate/hash，Catalog guard 先于 Store transaction，Draft 在事务内线性化；没有发现 Store callback 反向进入 Catalog。
5. graph、Workflow reserved metadata、名称/描述、Applied Source 与事件仍在一个 SQLite transaction 中提交；source-only 不推进 revision。
6. P0-4/P1-2 停止线保持：没有合成 implicit ResourceSlot Handle、模板或 condition/join，也没有修改 Backend/Frontend。

复审前必须：关闭 S-B01 与 P-B01～P-B05；为每个反例增加独立回归；重跑 02D 目标、Workflow/Registry/API 回归、完整仓库 suite、configured lint/static、format 和 `git diff --check`；将新测试 SHA 固定后再请求同一 reviewer 确认。当前 `d230884` **不得非 squash 本地合并**。
