# PackageCatalog 与 Package Workspace implementation spec（已批准）

> Spec revision: `approved-fe-os-1`
>
> Owner / reviewer: HUMAN
>
> Review status: `APPROVED BY HUMAN — 2026-08-01`
>
> Implementation status: `AUTHORIZED — R5 VALIDATION`

## 1. Outcome

Uni-Lab-OS 通过一个垂直 `package_manager` seam 读取领域包。显式 `PackageSource` 经
`compile_package_source()` 编译为无副作用、确定性的完整 `PackageCatalog`；OS 内的
Registry、FE Workflow/Template、Asset 与 community resolver 直接消费 Catalog。

```text
发现全部定义 ≠ Graph 选择定义 ≠ 激活全部定义 ≠ 连接全部硬件
```

## 2. 已冻结决策

1. 使用 A 身份：distribution 归一化名对应唯一 import package，namespace 为
   `community.<import_package>`；D 身份和持久 Package Inventory 取消。
2. PackageCatalog 是 definition/source/asset truth；Graph 独占 runtime instance、连接与
   初始化参数及 topology；Material authority 保持独立。
3. 不需要 Profile。所有连接参数都写在 Graph 中。
4. 基于已经完成 WorkflowTask/FE-OS migration 的 `integration/workflow-task-runtime`；
   使用 FE persistent authoring source loader 和 Graph authority。
5. 不引入 local bridge。consumer 与 OS composition root 位于同一进程。
6. `package_manager` 按 package lifecycle 垂直组织；删除平行的 app package scanners，
   `app/main.py` 仅做 adapter 和依赖注入。
7. 根 `package.yaml` 显式声明 package workflow membership；只登记 manifest 中的 workflow。
8. 当前以完整 distribution 为 Catalog、审计和可见性单位；Graph 只激活选中定义。
   按 Graph 裁剪可见性的 `DefinitionClosure` 延后实现。
9. 原计划 R6 取消；本 feature 止于 R5。

## 3. PackageCatalog v1

Catalog 包含：

- distribution identity、唯一 import package 和 namespace；
- device/resource/workflow definition provenance 与静态 contract；
- workflow UUID、`package://` source identity 和内容摘要；
- model/asset 逻辑路径、媒体类型、大小和摘要；
- content/catalog digest 与结构化 diagnostics。

Catalog 为 frozen、source-neutral 数据；不包含绝对路径、mtime、安装位置、运行实例、
Material、Template 数据库 UUID、Applied Workflow 或 Task。

## 4. Source 和 compiler

- `WorkspaceSource(root)`：显式源码仓库；
- `InstalledDistributionSource(name)`：显式 distribution；
- `CachedArchiveSource(wheel, expected_digest)`：community Graph resolve 后的缓存 wheel。

compiler 只读取声明和 AST，不调用领域代码的 import/exec。identity、manifest、装饰器参数、
引用或资产无法静态验证时 fail fast。三个来源必须生成 byte-identical canonical Catalog。

## 5. 运行边界

OS 启动时先编译 workspace，再使 workspace importable。Catalog 注册阶段只投影 metadata；
Graph 解析阶段才 import 和实例化所选 definition。Graph 节点 `config` 原样进入允许的设备/
资源构造参数，Graph edge 表达拓扑。发现未选 definition 保持零运行副作用。

FE workflow source roots 和 Graph authority 由 OS composition root 配置；领域 workflow
直接进入 FE authoring compiler，不经过 bridge 或另一套 DAG/runtime。

## 6. 构建、安装与发布

build 在临时 staging tree 嵌入 canonical Catalog，构建 wheel 后以 wheel Source 重新编译，
执行 artifact audit 与 workspace parity。upload 复用成功的 build/audit；install 要求显式
distribution identity 并在安装后验证，不记录 Inventory。

## 7. 验收与延期

必须覆盖 AST 无副作用发现、canonical digest、三来源 parity、wheel audit、显式 workflow
manifest、Registry/Template 投影、Graph-only activation、Graph config 构造、单设备 Graph 和
完整实验室 Graph 调试。

延期项仅为 `DefinitionClosure`/Lab 级定义可见性裁剪。它不影响“仅激活 Graph 所选设备”，
也不得在本轮以隐式过滤或第二 manifest 的形式提前实现。
