# 需求规格：PackageCatalog 与 Package Workspace

> 人工评审：`APPROVED — 2026-08-01`

## 目标

领域包开发者可以从一个仓库根静态检查、构建和调试设备、资源、工作流及模型资产；
同一份内容从 workspace、已安装 distribution 或缓存 wheel 进入 OS 时得到相同的
source-neutral `PackageCatalog`。

实验室开发者通过 Graph 选择设备和资源实例，并在 Graph 节点中填写全部初始化及连接
参数。Graph 边描述物理/通信拓扑。发现包内容本身不得导入驱动、实例化设备或连接硬件。

## 验收标准

### AC-1：单一包身份

- 根 `pyproject.toml` 的 `[project].name` 是 distribution 身份来源；
- 归一化 distribution 名必须对应唯一顶层 import package；
- definition FQID 固定为 `community.<import_package>.<definition_id>`；
- 不引入 `package_id`、D 身份方案或持久 Package Inventory。

### AC-2：静态、确定性发现

`compile_package_source(source)` 仅通过受限文件读取与 AST 发现 device、resource、
显式列于根 `package.yaml` 的 workflow，以及它们的资产闭包。它不得 `import`、`exec`
或实例化领域代码。Catalog 不含绝对路径、mtime、解包目录等来源状态，RFC 8785
canonical bytes 与 digest 可重复。

### AC-3：发现与激活分离

完整 PackageCatalog 可供 Registry、Authoring/Template 和 Asset consumer 查询；OS 只对
Graph 引用的 definition 做解析和运行激活。未被 Graph 选择的设备不 import、不实例化、
不创建运行节点、不连接硬件。

### AC-4：三种显式来源一致

`WorkspaceSource`、`InstalledDistributionSource` 和 `CachedArchiveSource` 使用同一 compiler
并产生相同 canonical Catalog。来源必须由调用方显式给出，不扫描整个环境或所有
site-packages。

### AC-5：构建和发布自审计

wheel 构建在临时 staging tree 中嵌入 Catalog；构建后从 wheel 来源重新编译和审计，
确认内容及 Catalog parity 后才可发布。`inspect` 保持只读。

### AC-6：FE-OS workspace 调试

`unilab --workspace <root> -g <graph>` 在 FE-OS 单进程启动链中完成 PackageCatalog 编译、
Registry 投影、Graph 选择和运行初始化。workflow 使用 FE authoring source loader 和
Graph authority；不得引入 package Profile 或领域包专用 local bridge。

## 用户入口

```bash
unilab package inspect --path . [--json]
unilab package build --path . [--out DIR]
unilab package upload --path . [--out DIR]
unilab package install <spec> [--distribution NAME]
unilab --workspace . -g deployment/graphs/lab.json [--check_mode]
```

`--workspace` 不与 legacy `--devices` 混用。旧 YAML/`--devices` 兼容逻辑只允许留在
`package_manager.legacy` 内部，不成为新 public API。

## 非目标

- Profile、Deployment Preset 或第二份连接参数文件；
- local bridge 或跨进程 PackageCatalog 同步；
- discovery 阶段创建/覆盖 Material Instance；
- 自动发现所有已安装包；
- 本轮实现按 Graph 派生的 `DefinitionClosure`；
- 原计划 R6。
