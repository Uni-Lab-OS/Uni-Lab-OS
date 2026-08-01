# 接口设计：Package manager

## 模块边界

```text
unilabos/package_manager/
├── catalog.py       # immutable schema、canonical bytes、diagnostics
├── compiler.py      # 唯一静态编译 seam
├── sources.py       # workspace / installed / cached wheel adapters
├── assets.py        # Catalog 约束下的逻辑资产读取
├── distribution.py # wheel staging、build、audit
├── publication.py  # 后端发布 payload 与顺序
├── community.py    # Graph 引用的缓存 wheel resolve
├── consumers.py    # Registry definition 与 Workflow source identity 投影
├── cli.py           # package 子命令 adapter
└── legacy.py        # 旧 YAML / --devices，仅内部兼容
```

`app/main.py` 只是 OS composition root：注册命令、注入 HTTP adapter、编译显式 workspace，
并把 Catalog 交给进程内 consumer。package-specific 判断不再分散到 `app/package_cli.py`、
`app/community_packages.py` 或 `app/web`。

`registry/catalog_consumer.py` 只保留浅层兼容 re-export；实现和演进权威均在
`package_manager.consumers`，避免形成第二套 consumer。

## 稳定 public seam

```python
from unilabos.package_manager import WorkspaceSource, compile_package_source

catalog = compile_package_source(WorkspaceSource(root))
payload = catalog.to_canonical_bytes()
```

稳定导出包括 `PackageCatalog`、`PackageCompileError`、三个显式 Source adapter 和
`PackageAssetResolver`。AST walker、CLI、publication、community、consumer 和 legacy
转换器不属于稳定 public seam。

## 数据流

```text
explicit PackageSource
        │ read-only / AST-only
        ▼
compile_package_source ──> immutable PackageCatalog
        │                         │
        │                         ├─> Registry definition projection
        │                         ├─> FE Workflow source identity
        │                         └─> PackageAssetResolver
        │
        └─> build staging ─> wheel ─> recompile/audit/parity

Graph node: instance id + definition fqid + config
Graph edge: physical / communication topology
        │
        └─> resolve/import/instantiate only selected definitions
```

## Workflow 和 Template

根 `package.yaml` 是 workflow membership 的显式 manifest。条目的 `workflow_uuid` 必须与
源码 `@workflow_definition` 一致；源码以 `package://...` identity 进入 FE persistent
authoring loader。PackageCatalog 只携带 source identity 和内容摘要，不生成 Applied revision、
Task 或数据库 UUID。

`workflow_template_imports_from_package_catalog()` 只保留为 F006 测试期间的结构兼容
adapter，不是 production publisher，也不允许 composition 逐 Action 调用。A1 会以唯一
`parse_action_contract()` 先生成 Registry canonical schema，再由只读 Registry snapshot
一次投影完整 aggregate 并调用 `TemplateCatalog.replace()`。持久 ResourceTemplate UUID
由现有 authority 显式提供，PackageCatalog 不抢占其身份权威。

## Registry 与 Graph 激活

Registry 投影登记定义元数据和 module/symbol，不在登记时 import。Graph 初始化解析所选
FQID 后才加载相应 Python symbol，并将 Graph 节点 `config` 作为构造参数。完整 Catalog
可见不等于全部定义运行激活。

## Asset 与安全边界

`PackageAssetResolver` 只接受 Catalog 中的逻辑相对路径，并在当前 Source observation 中
复核 containment、文件类型、大小和 digest。consumer 不获得任意源码目录读取能力。

## 错误模型

静态身份、manifest、引用、资产或 wheel 审计失败时返回 `PackageCompileError` 及结构化
diagnostics；CLI 显示路径和行号并以非零状态退出。失败不得留下源码 `_generated/`、
隐式 Inventory 或部分激活的设备。
