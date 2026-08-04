# R3A：SiteRef typed Workflow contract 设计与修改说明

日期：2026-08-04

状态：**IMPLEMENTED / FULL GATE PASSED / INDEPENDENT REVIEW ACCEPTED**

实现目录：`/home/zhangshixiang/Uni-Lab-Core/Uni-Lab-OS`

精确基线：`fef34d2ccee250eba8f03612dfd83cb196c2b56b`
（`integration/workflow-task-runtime` 最新同步点）

按用户要求，本轮没有创建或切换到新的实现分支；主目录以 detached HEAD 检出上述基线并保留
未提交修改。切换前旧工作完整保存在 stash 对象
`4bdb5896b7c37b8f2334ff0be51d2bc57753a5db`，stash message 为
`backup-before-r3a-site-ref-2026-08-04`。未跟踪目录 `unilabos_local_ui/` 不在本轮范围且没有修改。

## 1. 本轮解决的问题

旧合同只有 `ResourceSlot` typed value；Site 只能退化为带
`site_selector` 展示提示的普通字符串或开放 object。这样会造成三个问题：

1. Workflow 无法在类型层证明“这里需要 Site，而不是 Material”；
2. Task 创建时没有统一的 Site authority lookup，无法证明 Site UUID 存在且未被 resolver 替换；
3. Catalog/Handle 可能错误继承 `ResourceSlot` 的物料 allowlist 与 implicit pass-through。

R3A 新增一个独立的 `SiteRef` typed value，只表达 Site 的稳定身份：

```json
{"uuid": "72b51092-21f7-4d77-a478-9803dcfe5c1a"}
```

它的 canonical Workflow schema 固定为：

```json
{"$slot": "SiteRef"}
```

`SiteRef` 与 `ResourceSlot` 不可赋值、不可互换：

```text
SiteRef       -> SiteRef       allowed
ResourceSlot  -> ResourceSlot  按既有 allowlist 规则判断
SiteRef       -> ResourceSlot  rejected
ResourceSlot  -> SiteRef       rejected
```

## 2. 正确的领域关系

本轮不采用线性的
`ResourceSlot -> SiteRef -> SiteAccessBinding -> RobotCommand` 继承关系。正确关系是不同事实在执行
边界汇合：

```text
Operation parameters
  + ResourceSlot（要移动/处理的 Material）
  + SiteRef（目标或来源 Site identity）
  + SiteAccessBinding（后续：Site 到设备可执行点位的映射）
                         |
                         v
                    RobotCommand（后续）
                         |
                         v
              RobotExecutionBackend（后续）
```

R3A 只加入 `SiteRef`。它不表示 Site occupancy，不包含点位坐标，不选择机械臂，不生成机器人命令，
也不直接驱动物理设备。

## 3. Interface 与不变量

### 3.1 Python Annotation

Action/Workflow 源码通过公共类型声明：

```python
from unilabos.registry.placeholder_type import SiteRef

def move_to_site(target_site: SiteRef) -> MoveResult:
    ...
```

Annotation parser 与 renderer 必须确定性往返 `SiteRef <-> {"$slot":"SiteRef"}`。源码生成器在
合同包含 SiteRef 时自动导入该类型。

### 3.2 Task input

Task 创建只接受 exact object `{uuid}`：

- 裸 UUID 字符串拒绝；
- `id` 等别名拒绝；
- `label`、坐标、显示名等 presentation 字段拒绝；
- `resource_template_uuid` 等 ResourceSlot 字段拒绝；
- UUID 进入 resolver 前规范化；
- resolver 必须返回 frozen `ResolvedSiteRef(uuid=...)`；
- resolver 返回不同 UUID 时以 `invalid_input` 拒绝；
- 未装配 Site authority resolver 时 fail closed，不形成 partial Task/Job write。

Task snapshot 中冻结的仍是最小 JSON identity `{uuid}`，不是 resolver 对象或可变领域实体。

### 3.3 Catalog 与 Handle

Catalog 投影保留：

- Handle `type = "SiteRef"`；
- `meta_data.unilab.value_schema = {"$slot":"SiteRef"}`；
- editor control 为 `site_selector`；
- `implicit_passthrough = false`；
- 不生成同名隐式 source Handle；
- 不接收 `allowed_resource_template_uuids`。

### 3.4 Execution 安全边界

现有 legacy `DeviceActionTask` 尚未接入 Site execution backend，因此任何 `$slot` typed contract
仍明确走 `unsupported_contract`。这保证 R3A 不会把 `{uuid}` 当普通 dict 静默下发到设备。

## 4. 修改文件与原因

| 文件 | 修改 | 原因 |
|---|---|---|
| `unilabos/registry/placeholder_type.py` | 新增公共 `SiteRef` 类型及 strict Pydantic `{uuid}` shape | 让 Python Action 具有独立类型，不借用 ResourceSlot |
| `unilabos/workflow/schema.py` | 接受且只接受 canonical `{"$slot":"SiteRef"}` | 建立闭合 Workflow v1 值词汇，禁止物料 allowlist 混入 |
| `unilabos/registry/annotation_schema.py` | 增加 SiteRef import identity、解析与渲染 | 保证源码 Annotation 确定性往返 |
| `unilabos/registry/action_contract_schema.py` | Action compatibility type 投影识别 SiteRef | Catalog 中保留独立 Handle 类型 |
| `unilabos/workflow/handle_projection.py` | 新增 SiteRef schema 查找与 Handle type | 让 nullable/collection 外形可稳定投影 |
| `unilabos/registry/catalog_consumer.py` | SiteRef 使用 `site_selector`，不创建物料透传 | 保留类型，隔离 ResourceSlot 语义 |
| `unilabos/workflow/workflow_io.py` | 同类 SiteRef 可赋值，跨 ResourceSlot 拒绝 | 在连线/绑定编译时阻止 provider 混用 |
| `unilabos/workflow/graph_validation.py` | Handle vocabulary 增加 SiteRef，值 shape 要求 exact `{uuid}` | Graph provider 校验不再把两类 slot 混为一类 |
| `unilabos/workflow/task_input.py` | 新增 `ResolvedSiteRef`、`SiteRefResolver`、fail-closed adapter 与 preflight resolution | Task 首次持久化前由唯一 Site authority 关闭身份 |
| `unilabos/workflow/service.py` | 在正常 Task 创建入口注入 SiteRefResolver，缺省装配 fail-closed adapter | 让 public WorkflowService 路径可使用该端口，而不是只允许底层 helper 测试注入 |
| `unilabos/workflow/authoring_engine.py` | 生成 SiteRef Annotation 与 import | Published Workflow 源码可 round-trip |
| `unilabos/workflow/device_action_task.py` | 未接 backend 前拒绝全部 `$slot` contract | 防止 typed identity 静默降级为普通 dict |
| `CONTEXT.md` | 增加 SiteRef 领域定义与 Avoid 列表 | 固定 ubiquitous language，避免与 Site/ResourceSlot/Command 混用 |
| `tests/workflow/test_r3a_site_ref_typed_contract.py` | 独立 public contract RED/acceptance | 从公共 seam 验证类型、Catalog、兼容性和 Task input |

## 5. 明确不在本轮实现的内容

- `SiteAccessBinding` 的持久模型、版本与校准状态；
- SZLab 现有 `SiteControlBinding` 到中性合同的迁移；
- PLC/MoveIt 无关的 `RobotCommand` union；
- `RobotExecutionBackend` 选择与 TCP/IP、MoveIt adapter；
- Site selector API 和 FE 控件实现；
- Site occupancy mutation、Job claim/fencing 与运动完成后的 observation commit。

这些内容必须在后续轮次基于 SiteRef 接口继续实现，不能塞进 R3A 的类型解析器或 Task input
resolver。
