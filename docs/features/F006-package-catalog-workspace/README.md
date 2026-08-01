# F006 — PackageCatalog 与 Package Workspace

> 规格状态：`APPROVED BY HUMAN — 2026-08-01`
>
> 实现状态：`R5 VALIDATION`

F006 把领域包发现、构建、分发和 OS 内消费收敛到
`unilabos.package_manager`。显式的 Package Source 先被静态编译为不可变
`PackageCatalog`；Registry definition、Workflow source identity、Asset 和 community
resolver 随后在同一个 OS 进程中消费它。OS 不再需要领域包专用 bridge。

核心边界是：

```text
发现完整 PackageCatalog
        ≠ Graph 选择定义
        ≠ import / 实例化设备
        ≠ 连接硬件
```

Graph 是运行实例、连接参数及物理/通信拓扑的唯一输入。F006 不引入 Profile，
也不恢复 FE-OS migration 之前的 local bridge。原计划 R6 已由用户取消。

## 文档

- [需求规格](requirement.md)
- [接口设计](interface-design.md)
- [已批准 implementation spec](spec-draft.md)
- [实现轮次](feature-list.json)

## 当前范围

R1–R4 已实现，R5 正在完成 OS 全量门禁与独立评审。按 Graph 裁剪定义可见性的
`DefinitionClosure` 已由用户明确延期；当前导入外部包时会发现完整 Catalog，但运行时
仍只激活 Graph 引用的定义。canonical Action contract、Registry snapshot 到
`TemplateCatalog.replace()` 的原子发布、Backend-shaped Template HTTP 与 FE typed editor
属于后续 A1，不是 F006 的 production-ready 声明。
