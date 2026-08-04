# M2A-R3 复合工作流输入物料保证修复记录

日期：2026-08-04
分支：`migration/m2a-r3-composite-input-guarantee`
基线：`integration/workflow-task-runtime@fef34d2ccee250eba8f03612dfd83cb196c2b56b`
行为候选：`afd26fef367ba37fd25364f4d14f7ddcff154d4f`

状态：**独立红灯、审查 finding 回归、SZLab 原始复现和完整仓库门禁均已转绿；等待同一独立审查者对新精确候选复核。**

## 1. 结果与范围

本轮补齐已接受决策 D-067：当物料占位符（ResourceSlot）通过已发布工作流（Published Workflow）调用的同名隐式透传输出继续流向受限消费者时，生产者保证必须继承父工作流（Workflow）有效输入合同中的 `allowed_resource_template_uuids`。

实现没有增加或修改 HTTP API、JSON wire 字段、前端（FE）能力或复合工作流（Composite Workflow）持久结构。模板兼容仍使用严格的 `producer ⊆ consumer`：无约束生产者不能供给受限消费者，不兼容资源模板（ResourceTemplate）继续在候选编译阶段失败。

## 2. 修复语义

物料占位符（ResourceSlot）隐式透传的生产者保证按以下顺序解析：

1. 物料来源（MaterialSource）使用其已选资源模板（ResourceTemplate）；
2. 同名目标句柄存在唯一入边时，继续沿物料流（MaterialFlow）边递归回溯；
3. 没有入边、但公共父边界目标句柄绑定工作流（Workflow）输入时，读取父工作流有效输入合同；
4. 上述路径均不能证明时，回退到源句柄自身的允许列表；
5. 结果仍必须是消费者允许集合的子集。

公共父边界绑定和展开子图的 child-local 私有绑定保持两张映射。私有绑定只参与必填输入与 provider 形状校验，绝不读取父工作流（Workflow）输入合同。

## 3. 独立测试与提交 provenance

本轮始终只使用一个独立测试作者 `/root/composite_input_guarantee_tests`，测试先于对应生产修复提交，且没有删除、skip、xfail 或弱化断言。

| 阶段 | 独立分支提交 | 实现分支提交 | 结果 |
|---|---|---|---|
| 原始复合输入保证红灯 | `60109bff7fe8f2c5a26f610713505682afb5b0d9` | `a274c38a` | 同模板正例 RED；不兼容反例 GREEN |
| 首个生产修复 | — | `fc1289abc64ea34c63f8cd9f666203a153c4274d` | 原始正反例转绿 |
| 审查 finding：私有同名绑定越权 | `202eb93dbd10a57cb5a545f2ad9ff1d61c670919` | `2159be13` | 在首个候选上稳定 RED：未抛出预期拒绝 |
| 绑定作用域修复 | — | `afd26fef367ba37fd25364f4d14f7ddcff154d4f` | finding 与原始合同共同 GREEN |

首个独立 regression/security 审查发现：完整 `bindings_by_node` 同时包含公共父边界绑定和复合内部 child-local 私有绑定；内部参数与父输入同名时，`fc1289ab` 可能错误取得父输入的资源模板（ResourceTemplate）保证。`afd26fef` 只把公共父边界绑定交给生产者保证解析，同时保留完整绑定集合用于普通 provider 校验，关闭了该越权路径。

## 4. 验证证据

精确行为候选 `afd26fef367ba37fd25364f4d14f7ddcff154d4f`：

| 门禁 | 结果 |
|---|---:|
| M2A-R3、D-067 与复合 provider 专项 | `12 passed, 1 warning` |
| SZLab `single_sample_atomic_material.py` 嵌套 `material_transfer.py` 原始合同 | `1 passed` |
| 完整 `pytest -q -rs tests` | `2604 passed, 7 skipped, 68 warnings` |
| changed-file Ruff `E/F/I` | passed |
| changed-file Ruff format | passed |
| changed Python `py_compile` | passed |
| `git diff --check` | passed |

7 个 skip 是三个显式联网慢测试、一个需外部 Phoenix executable 的集成测试，以及三个仅在真实 Windows 文件共享环境运行的 Draft CAS 测试。本轮没有新增 skip 或 waiver。

## 5. 文件规模复核

| 文件 | 行数 | 职责与处理决定 |
|---|---:|---|
| `unilabos/workflow/graph_validation.py` | 1466 | 既有完整图事务校验入口；本轮保持一个校验顺序和错误优先级，不在安全修复中拆散。 |
| `tests/workflow/test_m2a_composite_input_resource_guarantee.py` | 216 | 原始跨复合边界正反合同，保持独立文件。 |
| `tests/workflow/test_m2a_composite_private_binding_scope.py` | 124 | 审查 finding 的私有绑定作用域回归，保持独立文件。 |

`graph_validation.py` 在基线已为 1318 行，本轮因完整中文参数/返回/异常文档和生产者保证逻辑增至 1466 行。当前保持完整的理由是：资源模板（ResourceTemplate）证明与边引用、provider 唯一性、复合内部节点识别和稳定错误顺序共享同一次 `validate_graph` 事务；仅搬移一个递归 helper 会暴露更宽的节点、边、模板、句柄、参数、绑定和输入合同接口，降低模块深度而不能减少耦合。

后续若继续扩展物料图校验，拆分触发点和顺序固定为：

1. 新建私有模块 `unilabos.workflow.resource_slot_graph_validation`；
2. 一次迁移物料流（MaterialFlow）fan-out、资源模板（ResourceTemplate）生产者保证、允许列表规范化和对应错误；
3. 只向 `graph_validation.validate_graph` 暴露一个窄的 `validate_resource_slot_graph(...)` 入口；
4. 先迁移现有 M2A、复合输入与直接图保存回归，再删除旧私有 helper；
5. 保持 `CodedGraphValidationError`、`MaterialSourceGraphError` 和普通 `GraphValidationError` 的现有错误优先级。

## 6. 停止线

- 本轮没有修改前端（FE）、SZLab 源码或真实设备配置。
- 本轮没有合并到 `integration/workflow-task-runtime`，也没有 push。
- 合并前仍需同一独立审查者对包含 finding 修复的精确候选确认 `ACCEPT`；任一生产代码变化都使该确认失效。
