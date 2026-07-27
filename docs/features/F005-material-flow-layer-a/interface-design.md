# 物料流与 Layer A 即时分配设计（grill-me 草案）

> 状态：讨论中，不是实现完成说明。
>
> 本文只记录已经确认的共识、当前实现缺口和仍待确认的设计项。未确认的字段与接口不得据此直接实现。

## 1. 目标

为 Canonical v2 工作流补齐以下闭环：

1. 通用物料起始节点表达物料需求；
2. 运行时按类型、warehouse 或具体物料选择实际物料；
3. Python scheduler 在 Layer A 检查当前设备、物料和库位状态并原子取得所需资源；
4. 物料通过有类型的输入、输出端口沿节点传递；
5. 具体物料能够关联运行、节点进度和当前位置；
6. 同一套工作流同时支持“直接发给 OS”和“后端先分配后发给 OS”。

## 2. 已确认共识

### D001：Python Layer A 覆盖当前设备、物料与库位

Python scheduler 的 Layer A 不只表示设备忙闲。它负责节点准备执行时的当前状态判断：

- 当前设备能否立即取得；
- 当前具体物料是否存在、可用且未被其他运行占用；
- 当前源库位是否确实承载该物料；
- 当前目标库位是否为空且可立即使用；
- 一次动作需要的设备、物料与库位必须原子取得，不能只取得其中一部分。

Layer A 不表达未来时间区间，不生成 PlannedOccupancy，也不承诺跨站硬时间窗。

### D002：采用“短锁 + 持久 MaterialBinding”

按类型自动选材采用以下语义：

1. 节点进入 READY 后读取当前 ResourceTreeSet、warehouse 配置和传感器证据；
2. 按稳定规则生成候选具体物料；
3. 对候选物料及本次即时操作涉及的设备、库位执行原子 acquire-all；
4. 取得锁后重新验证身份、占用、传感器与当前 binding 状态，避免使用锁前快照；
5. 原子提交 MaterialBinding，使该物料从“可分配”进入“已分配给某次运行”；
6. 选材临界区结束后释放短锁，后续排他性由持久 MaterialBinding 保证；
7. 后续实际移动节点仍需对正在操作的物料和即时目标库位取得动作级 Layer A 锁；
8. 结束、消费、交接、取消或补偿时显式转换或终止 MaterialBinding。

不采用“从物料起始到工作流结束一直占有 warehouse 设备锁”的方案。

### D003：一个设备实例只拥有一个 warehouse

- warehouse 必须有唯一 owner device；
- owner device 负责该 warehouse 的传感器读取与物理状态；
- 多个 warehouse 必须创建多个设备实例；
- 每个设备实例使用自己的 OPC/Modbus client；
- allocator 不能跨 owner device 直接读取或修改 warehouse 私有状态。

### D004：传感器不提供物料身份

- 传感器只提供 `occupied / empty / unknown` 证据；
- 具体物料身份来自 ResourceTreeSet、扫码或人工绑定；
- 传感器与图状态不一致时必须 fail closed；
- 不一致的物料或 Site 进入待盘点状态，不得继续自动分配。

### D005：主工件与辅助物料使用同一种起始节点

- 通用物料起始节点通过 role 区分主工件、试剂、耗材等；
- 主工件在起始时确定具体身份，并沿后续物料端口保持同一 binding；
- 辅助物料可以在靠近消费节点时再执行动态分配；
- 纯控制节点无需物料端口；
- 其他实际处理、观察、移动或消费物料的节点必须声明相应输入、输出。

## 3. Layer A 与 Layer B 的边界

| 能力 | Python Layer A | Layer B |
|---|---:|---:|
| 当前设备互斥 | 是 | 可纳入计划 |
| 当前具体物料互斥 | 是 | 可纳入未来计划 |
| 当前源 Site 占用验证 | 是 | 可预测未来位置 |
| 当前目标 Site 空位验证 | 是 | 可预留未来空位 |
| 按类型即时选择具体物料 | 是 | 可提前规划具体物料 |
| 原子取得设备、物料、Site | 是 | 可规划未来区间 |
| 未来时间段预约 | 否 | 是 |
| PlannedOccupancy | 否 | 是 |
| `material_at / slot_reserved / in_transit` 时间轴 | 否 | 是 |
| 优化、分解、跨站硬窗保证 | 否 | 是 |

## 4. 两种提交模式的统一语义

### 4.1 直接发给 OS

提交的是物料需求。OS 在物料起始节点进入 READY 后，根据当前状态选择具体物料并创建 MaterialBinding。

### 4.2 后端先分配再发给 OS

提交的是已经指定具体物料的需求。OS 不盲信外部分配结果，仍需在 Layer A：

- 验证物料属于指定类型和 warehouse；
- 验证 MaterialBinding 没有冲突；
- 验证传感器与 ResourceTreeSet 一致；
- 原子接管或确认 binding。

两种模式在物料起始节点成功后必须产生同一种输出，后续节点不得区分物料来自后端还是 OS 自动选择。

## 5. 当前实现可复用部分

- ResourceTreeSet 及物料、父子、Site 关系；
- 本地只读 Material Aggregate 投影；
- Canonical v2、普通 input binding 和节点结果存储；
- DAG 节点运行状态、事件日志和恢复框架；
- ResourceLockManager 的 acquire-all、lease 与 unknown fence；
- ActionContract 中 material mode、effect、resource claim 的结构；
- SZLab 物料类型、warehouse、Site 与传感器映射；
- 前端 2D/2.5D Material Aggregate 视图。

## 6. 当前实现缺口

- MaterialRequirement；
- 通用物料起始与结束节点；
- MaterialBinding 及其持久状态机；
- 按类型和 warehouse 生成候选的 allocator；
- 物料与 Site 的 Layer A claim 解析和准入；
- 可写的物料 revision/CAS 命令；
- 传感器与 ResourceTreeSet 的通用对账；
- MaterialHandle；
- Profile material ports 到 Canonical schema 的物化；
- 可执行的 material edge；
- 物料 effect 的原子提交与补偿；
- `binding → run → node` 进度索引；
- 前端类型化物料端口、连线与起始节点配置界面；
- 对应的运行时 capability。

## 7. 待确认问题

### Q002：节点之间传递什么

尚未确认 MaterialHandle 是：

1. 轻量 binding 引用；
2. 完整 MaterialAggregate 快照；
3. 只有 material UUID。

在 Q002 确认前，不冻结 MaterialHandle 字段，也不定义 material edge wire schema。
