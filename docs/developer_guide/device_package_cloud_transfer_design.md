# 设备软件包 CLI 云端上传与下载设计

> 状态：候选设计（Candidate Design）；本期范围已冻结为仅修改 Uni-Lab-OS
> 日期：2026-08-10
> OS 基线：`product/durable-scheduler-kernel@aee69e03`
> Cloud 基线：`main@6d69d27a`
> 设备包示例基线：`Uni-Lab-SZLab@origin/main@1016dd30`
> 遗留 Backend 证据基线：`/home/sunmenglei/uni-lab-backend@test@2d94a64`；该分支相对远端有分叉
> Core Backend 证据基线：`origin/main@d5520789`、`origin/feat/workflow@fa269e9d`
> 上游约束：[Uni-Lab-Core#147](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/147)（当前仍为 Open / `stage:testing`）

## 1. 结论

**当前用户不是通过 OS CLI 完成完整的上传/下载闭环。** 当前分支已经注册
`unilab package upload --path ...`，但它只是遗留协议上传骨架，仍有 MIME、OSS scene、流式传输、
业务错误和上传后对账等缺口；当前 CLI 没有 `package download`。启动期旧下载代码也不是用户可用的
软件包管理器（Package Manager）CLI。

2026-08-10 冻结本期决策：**用户入口统一为 OS CLI，本期只修改 Uni-Lab-OS，既不修改
uni-lab-backend，也不修改 Uni-Lab-Cloud。** 具体目标是：

1. 硬化现有 `package upload`，仍调用已部署 Backend 的现有 OSS token、`/lab/resource` 和设备广场接口；
2. 新增 `package download`，按模板 UUID 或包名解析、下载并校验同一 wheel，写入可丢弃缓存；
3. 用只读 capability probe 证明目标环境兼容 `legacy-template-package/v1`。接口缺失或信封不匹配时返回
   `backend_incompatible` 并停止，不通过临时修改 Backend 或 Cloud 兜底；
4. 为二次开发提供可选的 `download --extract-source DIR`：从已经完成三重身份校验的 wheel 导出一个
   可再次 `inspect`、`build` 和 `upload` 的派生 Package Workspace。上传仍只有一个 wheel；构建阶段把
   重建工作区必需的规范化清单嵌入 wheel，不新增 Backend/Cloud 字段或第二个远端 Artifact。

本期不建立规范软件包发布（Package Release）v2，不新增 Backend 表、路由或 migration，也不修改 Cloud
页面和类型。第 14 节只保留为后续架构方向，不是本期依赖或验收项。该 OS-only 方案的成立前提是目标
test/UAT/prod 环境已经提供本文列出的遗留接口；若实际部署是已经删除模板包元数据且无等价路由的 Core
Backend，本期 CLI 应明确报告不兼容，而不是宣称上传/下载成功。

## 2. 事实基线

### 2.1 当前 OS

| 能力 | 当前实现 | 判断 |
|---|---|---|
| 静态发现 | `package_catalog` 可扫描设备、资源模板、显式工作流源码和资产 | 可复用 |
| 构建 | `build_workspace_package()` 生成 wheel，校验 RECORD、闭包、Artifact 摘要并从 wheel 源码重编译 | 可直接作为上传唯一输入 |
| 上传 CLI | 已注册 `unilab package upload --path ...` | 有骨架，不是完整产品闭环 |
| 上传编排 | `package_distribution/adapters/cloud.py` 先上传同一已审计 wheel，再发布模板投影 | 方向正确 |
| 下载 CLI | 未注册 | 缺失 |
| 启动期下载 | `app/community_packages.py` 可 resolve、下载、校验可选 SHA 并解压缓存 | 遗留旁路，需收敛 |
| 三类 Package Source | 当前仅正式公开 `WorkspaceSource` | 与 #147 的接受设计尚有缺口 |
| 安装 | `adapters/directory.py` 有实现但未接入当前 CLI，且只做安装目录扫描 | 不应代替可信下载 |

当前上传骨架还存在以下实质问题：

- OSS token 使用 `scene=models`，而遗留 Backend 只接受 `model`、`file` 等值；未知值会静默降为
  `default`。
- wheel 被声明为 `application/gzip`，实际不是 gzip；兼容协议统一使用
  `application/octet-stream`，且 token 签名值必须与 PUT 请求头完全一致。
- 通用 HTTP client 会把整个 wheel 一次性读入内存；构建侧允许的 Artifact 上限为 2 GiB，
  二者不匹配。
- `/lab/resource` 的业务错误仍以 HTTP 200 返回；当前发布层只检查 HTTP 状态，可能误报成功。
- 普通 `package upload` 只读取 `BasicConfig.ak/sk`，没有复用已存在的 login/session 凭据解析。
- 旧 HTTP client 会记录可逆的 Lab auth secret；设备包传输不能继续使用这条日志路径。
- 上传完成后没有重新查询设备广场，也无法证明凭据对应权威实验室。
- `build_resources_from_registry()` 没有把顶层 Handle 完整投影给 Backend，且
  `source_fqid`、`content_hash` 和动作 Schema 的跨仓字段合同没有固定测试。
- 设备、资源模板会上传；工作流定义不会进入 `/lab/resource`。这一点应明确，不得把
  “wheel 包含工作流”误写成“工作流已发布到云端工作流广场”。

### 2.2 当前 Cloud 与遗留 Backend

Cloud 的设备广场是读取与展示层：

- `GET /api/v1/lab/square/list`：设备列表；
- `GET /api/v1/lab/square/detail/{templateUuid}`：设备详情，含 `package_info` 和
  `source_registry`；
- `GET /api/v1/lab/square/packages`：按 `package_info.name` 聚合包列表；
- `GET /api/v1/lab/square/packages/{packageName}`：包详情和包内设备；
- `POST /api/v1/lab/square/copy_resource`：复制模板到用户实验室，不下载 Artifact。

Cloud 当前没有执行 wheel 二进制上传或下载；真正的字节链路是 OS → Backend → OSS。

被调研的遗留 Backend 快照提供 M1-Legacy 所需接口：

| 能力 | 方法与路径 | 鉴权 | 当前语义 |
|---|---|---|---|
| 上传 token | `GET /api/v1/lab/storage/token` | Lab AK/SK | 返回预签名 PUT、对象键和 Content-Type |
| 发布模板 | `POST /api/v1/lab/resource` | Lab AK/SK | 在当前凭据所属实验室内按 `(lab_id, name)` upsert |
| 包列表 | `GET /api/v1/lab/square/packages` | 无 | 只聚合权威实验室中的设备模板 |
| 包详情 | `GET /api/v1/lab/square/packages/{name}` | 无 | 返回包元信息和设备模板 UUID，不返回完整摘要 |
| 模板详情 | `GET /api/v1/lab/square/detail/{uuid}` | 无 | 返回完整 `package_info`、`source_registry` |
| 下载 | `GET /api/v1/lab/square/packages/releases/{uuid}/download` | 无 | 参数实际是模板 UUID，302 到 OSS 短效 URL |
| Graph 解析 | `POST /api/v1/lab/square/community-packages/resolve` | Lab AK/SK | 按 `community.*` class 解析权威实验室中的包 |

关键限制：`POST /lab/resource` 写入“当前凭据所属实验室”，设备广场却只读取配置中的
“权威实验室”。因此 HTTP 200 不等于“已进入设备广场”。现有接口也没有独立的发布权限、
release 记录或同版本不可变约束。

### 2.3 既有 Backend 兼容性与 G0 门禁

Core Backend 快照中的以下迁移已经删除资源模板上的包元数据：

- `migrations/postgres/000035_remove_resource_registry_metadata.up.sql`；
- `migrations/sqlite/000035_remove_resource_registry_metadata.up.sql`。

删除字段是 `resource_template.package_info` 和 `resource_template.source_registry`；该快照中也没有找到
与遗留 square/package route 等价的正式替代接口。这说明“遗留 Backend 有接口”与“Core Backend
已收敛同一接口”是两个不同事实。

本期不要求 Backend owner 选择或实现新协议，但 OS 实现前必须完成只读 G0：

1. 明确 test/UAT/prod 实际部署仓库、分支、提交和数据库 migration 版本；
2. 对目标环境探测 storage token、资源发布、包列表/详情/resolve/download 的方法、路径、鉴权和信封；
3. 把当前遗留 DTO 固化为 OS 测试 fixture，覆盖 `package_info`、`source_registry`、Action/Handle
   Schema 和摘要字段；
4. 仅在全部必需能力通过时选择 `LegacyTemplateBackendAdapter`，禁止根据单个 HTTP 200 猜测能力；
5. 任一必要路由或字段缺失时返回 `backend_incompatible`。这会阻止该环境的本期功能，但不会把
   Backend/Cloud 改造重新纳入本期。

OS 领域接口不得暴露模板 JSONB 的布局。遗留 DTO 只存在于兼容 Adapter 内，后续可以被 v2 Adapter
替换而不改 CLI、Catalog、构建和缓存。

### 2.4 已有旁支验证证据

`feature/electron-device-provisioning` 曾实现 `CachedArchiveSource`、设备模板 UUID 下载、
Artifact/Catalog 校验、缓存和 stdin 一次性鉴权。2026-08-07 的 SZLab mock UAT 证明：

- wheel 经 UAT OSS 上传后可以按模板 UUID 下载；
- 上传与下载 Artifact SHA-256 完全一致；
- 下载后的软件包目录（PackageCatalog）可以驱动真实 OS 启动；
- 单点动作（Action）和工作流（Workflow）均成功。

这是可迁移证据，不是当前分支现状。旁支使用旧平铺模块，仍有错误 MIME、`models` scene、
调用方手工传摘要/FQID、业务码未检查等问题，不能直接合并。

### 2.5 最新 Uni-Lab-SZLab 示例

`Uni-Lab-SZLab@origin/main@1016dd30` 已经具备 #147 要求的设备包外形：

- 仓库根只有一个 `pyproject.toml`，distribution 是 `szlab-poly-studio`；
- 唯一常规顶层 import package 是 `szlab_poly_studio`，没有 `packages/` 中间层；
- `package.yaml` 显式登记工作流源码和稳定 `workflow_uuid`；
- 设备、资源和工作流分别位于 import package 下，模型归属到相应
  `devices/<device_id>/models/` 或 `resources/<resource_id>/models/`；
- 没有 `unilabos.model_bundles`、运行时 Profile 或第二个模型发现协议；
- `deployment/` 中的 Graph 独占实例身份、连接/初始化配置和拓扑，不进入装饰器扫描根。

该提交的测试源码期望软件包目录（PackageCatalog）包含 9 个设备定义、20 个资源模板定义、
18 个工作流定义和 98 个动作（Action）。本次因环境缺少 `rfc8785`、`networkx` 和 pytest，
未重新运行这些测试；这里引用的是最新仓库测试合同，不把它写成新验收结果。

SZLab 当前仍使用 `version = "0.1.0"`。一旦开始向设备广场发布不可变版本，每次改变 wheel
内容都必须提升版本，或由 CI 证明同一版本的 Artifact 摘要未变化；否则会触发本设计的
`version_conflict`。

## 3. #147 的已接受约束

以下内容来自已人工批准的 #147 规格，应视为本设计的硬约束：

1. 仓库根 = 一个 Python distribution root = 一个 Package Workspace；只有一个顶层 import package。
2. 定义 FQID 固定为 `community.<import_package>.<definition_id>`；不新增 `package_id`。
3. 软件包管理器（Package Manager）是唯一发现接缝：

   ```text
   PackageSource -> PackageCatalog
   ```

4. `WorkspaceSource`、`InstalledDistributionSource`、`CachedArchiveSource` 必须产生同一种
   source-neutral PackageCatalog。
5. 工作流（Workflow）成员由根 `package.yaml` 显式授权，清单 `workflow_uuid` 必须与源码声明一致；
   递归 AST 扫描不能替代工作流来源身份合同。
6. #147 正式公共命令包含 `inspect`、`build`、`upload` 和 `install`；本设计新增的 `download`
   是候选传输命令，不得改变既有命令语义。
7. `upload` 必须复用 `build` 的同一已审计 wheel；不得重扫工作区或接受外部下载 URL 绕过。
8. 下载进入可丢弃缓存；不建立持久 Package Inventory。
9. 发现定义不等于创建设备实例，更不等于连接硬件。
10. 物理图（Graph）独占设备实例、初始化配置和连接拓扑。
11. PackageCatalog 可以描述资源模板和工作流源码，但不得创建或覆盖具体物料（Material）实例。
12. 包相关实现集中在 `unilabos/package_manager/`；`app/main.py` 只负责组合和依赖注入。

## 4. 范围与非目标

### 4.1 本期 OS-only 范围

本期只交付 Uni-Lab-OS 改动，并且仅适用于 G0 确认目标环境已经支持
`legacy-template-package/v1` 的情况。

- CLI 从 Package Workspace 构建并上传已审计 wheel；
- 发布包内设备定义和资源模板定义到现有 `/lab/resource`；
- CLI 按设备模板 UUID 或包名从设备广场解析并下载 wheel；
- 校验 Artifact、Catalog 和内容三类身份后写入受管缓存；
- 可选地从已验证 wheel 安全导出一个可重新检查和构建的派生开发工作区；
- 提供稳定 JSON 输出、明确退出码、脱敏诊断和重试语义；
- 把遗留启动期下载逐步适配到同一 acquisition/cache 实现。

#147 已接受的 `install` 仍保留为后续命令，但不属于本期“先完成上传和下载”的完成定义。

### 4.2 非目标

- 下载后自动修改物理图、创建设备实例或连接硬件；
- 上传或下载具体物料实例、库存或库位；
- 通过设备广场接口发布工作流模板；
- 自动扫描全部 site-packages；
- 自动安装 Python 依赖；
- 在 OS 内保存远端包的持久 Inventory；
- 允许任意 URL、任意 wheel 路径或调用方自报的摘要绕过 Backend 描述。
- 通过源码导出还原原始 Git 仓库、Git 历史、未进入 wheel 的测试/文档或开发者本地文件；
- 为源码导出上传独立的 `source.tar.gz`，或在 Backend/Cloud 中新增源码 Artifact 字段和路由；
- 新增或修改 Backend 表、migration、路由、鉴权和响应 DTO；
- 修改 Cloud 设备广场页面、类型或上传入口；
- Package Release v2（本期不实现），以及把 `install` 纳入本期完成门禁。

### 4.3 跨仓改动判定

| 仓库 | 本期是否修改 | 本期用途 | 后续方向 |
|---|---|---|---|
| Uni-Lab-OS | **是，唯一代码改动仓库** | 上传硬化、acquire/cache、`download`、鉴权组合与 OS 测试 | 后续接 `install` 或 v2 Adapter |
| Uni-Lab-SZLab | 否 | 只读示例/测试 fixture；本期不改变版本或包结构 | 正式持续发布时再补版本不可变纪律与 CI |
| 遗留 uni-lab-backend | **否** | 作为既有 `legacy-template-package/v1` 外部服务；只做 capability probe | 长期是否替换由独立决策处理 |
| Core/规范 uni-lab-backend | **否** | 若缺少遗留能力则明确不兼容，不在本期补路由 | Package Release v2 另立项目 |
| Uni-Lab-Cloud | **否** | 继续读取既有设备广场数据；不参与 wheel 字节传输 | v2 时再切换 Release DTO |
| Uni-Lab-Core | 否 | #147 仅作为架构约束，本设计记录扩展关系 | v2 需要独立 ADR/Issue |

本期 UAT 使用已有 mock/SZLab wheel fixture。若设备广场已存在相同版本且 Artifact 摘要不同，CLI
必须返回 `version_conflict`；本期不通过修改 SZLab 版本来掩盖该冲突。

## 5. 领域对象与权威

| 对象 | 身份 | 权威 | 生命周期 | 持久事实 | 失败语义 |
|---|---|---|---|---|---|
| 软件包目录（PackageCatalog） | `catalog_digest` + namespace | OS 唯一静态编译器 | 每次来源观察重新产生 | canonical JSON、定义、资产摘要 | 任一诊断使整包失败，不返回部分目录 |
| 已审计 wheel | `artifact_digest` | Package Build | build 成功后不可变 | wheel 字节、RECORD、内嵌 Catalog | 摘要、闭包或重编译不一致即不可上传/缓存 |
| 设备广场兼容发布描述 | 包名、版本、namespace、三摘要 | M1-Legacy 为模板 `package_info`；M2-Canonical 为 Backend release | 已上传、已发布、已弃用 | 对象键、定义 FQID、摘要 | M1-Legacy 可能部分成功；M2-Canonical 必须原子发布 |
| 资源模板（ResourceTemplate） | 遗留 Backend UUID；规范来源为 `source_fqid` | 权威实验室 Backend | upsert/展示/复制 | 模板、Action/Handle、来源包 | 不得反向成为 PackageCatalog 权威 |
| 可丢弃缓存条目 | `artifact_digest` | OS package distribution | 下载、校验、命中、删除 | wheel 与验证元数据 | 可整体删除；损坏即丢弃并重新下载 |
| 设备实例 | Graph 节点 ID/UUID | 物理图（Graph） | 用户配置后由运行时激活 | definition FQID、config、topology | 本设计不创建、不激活 |
| 物料（Material）实例 | 既有物料身份 | 库存（Inventory）/物料存储 | 既有领域规则 | 实例数量、状态、库位 | 与设备包传输无关 |

M1-Legacy 中“发布描述”只是遗留兼容投影，不新增本地持久对象。M2-Canonical 的 release 是 Backend
远端发布事实，不违反 OS 不建立 Package Inventory 的约束。

## 6. 目标模块

```text
unilabos/package_manager/
├── package_catalog/
│   ├── sources.py              # 本期增加 CachedArchiveSource；InstalledDistributionSource 后续补齐
│   ├── model.py                # canonical bytes 严格反序列化
│   └── compilers/python/       # 三种来源进入同一编译入口
├── package_distribution/
│   ├── publication.py          # 已审计 BuildArtifact -> 发布 DTO/顺序
│   ├── acquisition.py          # 远端描述 -> 下载 -> 校验
│   ├── cache.py                # 内容寻址、锁、原子发布、清理
│   ├── workspace_export.py     # 已验证 wheel -> 派生 Package Workspace
│   ├── release_models.py       # 远端描述与稳定结果 DTO
│   └── adapters/cloud.py       # Backend/OSS 的最小 HTTP Adapter
└── cli.py                      # 解析、调用、JSON/人类输出

app/main.py                     # 凭据/地址解析并注入 Adapter
app/community_packages.py       # 迁移期薄兼容层，最终删除重复下载实现
```

依赖方向保持：`package_catalog <- package_distribution <- composition root`。
`package_catalog` 不导入 HTTP、OSS、Graph、设备运行时或 Backend DTO。

当前 `package_distribution/build.py` 已超过 500 行；实现本设计时应把可复用的 wheel
成员校验和来源重编译能力提取到明确所有者，不再继续扩大该文件。

### 6.1 深模块与接缝

#147 已接受且对包消费者稳定公开的接缝仍然是：

```python
catalog = compile_package_source(source)
```

其稳定导出包括软件包目录（PackageCatalog）、三种显式 Package Source 和资产解析器。
发布、远端获取、CLI 与 Backend DTO 均不是该稳定公共接缝。

在软件包管理器（Package Manager）内部，CLI 只跨两个小接口，不能直接调用缓存、HTTP、OSS
或资源模板 DTO：

```python
PackagePublisher.publish(artifact: PackageBuildArtifact) -> PublishResult
PackageAcquirer.acquire(request: AcquireRequest) -> AcquiredPackage
```

- `PackagePublisher` 隐藏上传能力探测、对象传输、模板/release 发布、后置对账、幂等和部分失败语义；
- `PackageAcquirer` 隐藏远端解析、短效 URL、流式下载、安全审计、Catalog parity 和原子缓存；
- 可选源码导出是 acquisition 成功后的本地派生步骤；CLI 只在 `AcquireRequest` 中提供目标目录，
  `workspace_export.py` 负责成员选择、规范化清单验证、临时目录和原子提交；
- Backend 地址和鉴权在组合根构造 Adapter 时注入，不作为每次调用的秘密参数穿过 CLI；
- 本期只有 `LegacyTemplateBackendAdapter` 和测试用 `InMemoryPackageBackendAdapter`；未来
  `PackageReleaseBackendAdapter` 可以满足同一远端传输接缝，但不在本期实现；
- 文件系统、锁和摘要计算是 acquisition 实现的内部接缝，不泄漏到 `PackageAcquirer` 的接口；
- 测试通过上述接口断言可观察结果和错误码，不依赖内部调用顺序或临时文件布局。

删除 `PackagePublisher` 或 `PackageAcquirer` 会迫使 CLI、Graph 启动链和未来 `install` 分别复制协议、
安全和失败处理；把这些行为收口后，调用方只需理解请求、结果和稳定错误合同。

## 7. CLI 合同

### 7.1 云端环境选择

设备软件包的上传、下载和发布后对账必须绑定到本次命令解析出的同一个环境。省略 `--addr` 时固定使用
正式环境；显式 `--addr` 可以覆盖该默认值。公共环境合同冻结如下；Web 入口用于浏览器访问，CLI 必须
使用带 `/api/v1` 的 API 根地址：

| 环境 | CLI 别名 | Web 入口 | Backend API 根 |
|---|---|---|---|
| 测试 | `test` | `https://leap-lab.test.bohrium.com/` | `https://leap-lab.test.bohrium.com/api/v1` |
| UAT | `uat` | `https://leap-lab.uat.bohrium.com/` | `https://leap-lab.uat.bohrium.com/api/v1` |
| 正式（默认） | `prod`、`production` | `https://leap-lab.bohrium.com/` | `https://leap-lab.bohrium.com/api/v1` |

三种环境既可以使用别名，也可以显式传完整 API 根。例如：

```bash
# 未传 --addr，使用正式环境
unilab package upload --path . --json

unilab --addr test package upload --path . --json
unilab --addr uat package download --package unilab-szlab-mock --version 0.1.0 --json
unilab --addr prod package upload --path . --json
```

规则：

- 未传 `--addr`、显式 `--addr prod` 和显式 `--addr production` 必须得到完全相同的正式 API 根；
  不得根据 session、当前网页或上一次命令把默认环境改变为 test/UAT。
- `--addr` 接受上述别名、对应的完整 API 根和显式本地测试地址；Cloud Web 根地址不会被猜测或自动
  补成 API 根，传完整 URL 时调用方必须带 `/api/v1`。
- 一次命令只解析一次环境。Backend capability probe、token、OSS 上传/下载、模板发布和发布后广场
  对账必须使用同一环境，禁止失败后静默回退到其他环境。
- 人类输出在第一个远端副作用前显示实际环境；`--json` 的最终文档必须包含稳定的 `environment`
  字段，使省略 `--addr` 的自动化调用也能证明实际操作的是正式环境。
- AK/SK、login session 和缓存来源环境必须与所选环境一致；环境不匹配时失败关闭，不尝试跨环境复用。
- 当前分支的 `uat` 别名仍指向 `https://uni-lab.uat.bohrium.com/api/v1`，且包命令的独立地址解析器未
  完整处理 `prod/production`。两者均是本期必须修复并由精确映射测试锁定的实现差异，不能作为正式合同。
- 地址映射成功不等于上传/下载能力已经验收。每个目标环境仍须通过第 2.3 节 G0 capability probe；
  路由、信封或必要字段不兼容时返回 `backend_incompatible`。

### 7.2 上传

当前状态：命令已注册，但只是待硬化的上传骨架。以下是本期完成后的目标合同，不代表当前代码已经
支持全部参数和成功语义。

```bash
unilab login --ak "$AK" --sk "$SK"
unilab --addr uat package upload --path . --out ../dist --json
```

非交互集成可使用关闭的 stdin 合同：

```bash
unilab --addr uat package upload --path . --auth-stdin --json
```

stdin：

```json
{"schema_version":"unilab-package-upload-auth/v1","ak":"...","sk":"..."}
```

规则：

- 普通 CLI 凭据优先级为显式 stdin > login session > 本地配置；不鼓励在 argv 传秘密。
- `--auth-stdin` 必须在加载 Python `local_config.py` 前处理。
- 删除并继续拒绝 `--download-url`；上传一定消费本次 `build` 的 wheel。
- `--json` stdout 只输出一个最终 JSON 文档；进度和诊断进入 stderr。

### 7.3 下载（候选扩展）

当前状态：命令不存在；本期在 OS CLI 新增。

精确模板选择：

```bash
unilab --addr uat package download \
  --template-uuid 11e27cf5-3ec8-4cfb-bb17-db941426e94e \
  --json
```

按包选择：

```bash
unilab --addr uat package download \
  --package unilab-szlab-mock \
  --version 0.1.0 \
  --out ./downloads \
  --json
```

二次开发时导出一个新的 Package Workspace：

```bash
unilab --addr uat package download \
  --package unilab-szlab-mock \
  --version 0.1.0 \
  --extract-source ./unilab-szlab-mock \
  --json
```

规则：

- `--template-uuid` 与 `--package` 互斥；调用方不再手工提供 FQID 或摘要。
- CLI 必须重新读取 Backend 详情取得 `source_fqid` 和三类摘要，不信任 UI/argv 复制值。
- `--package` 在 M1-Legacy 会读取包详情，再逐个读取设备详情；只有候选模板全部指向同一
  `(name, version, namespace, artifact_digest, catalog_digest, content_digest)` 时才可选择。
- 同包混有多个发布、缺少摘要或没有设备模板时失败关闭，提示改用精确模板 UUID 或重新发布。
- 默认写受管缓存；`--out DIR` 只额外复制一份已经校验的 wheel，不改变缓存权威。
- `--extract-source DIR` 不改变下载和缓存权威；它只在 wheel 完成全部校验后，导出一个派生开发工作区。
- 导出目录必须尚不存在，禁止覆盖或合并用户已有目录。实现先写同一父目录内的随机临时目录，完成
  `WorkspaceSource` parity 后再原子改名。
- 导出内容包括唯一顶层 import package、包内资产、规范化 `pyproject.toml`、根 `package.yaml`
  （若原包需要）和 `.unilab-package-origin.json`。后者只记录环境别名、包身份和摘要，不保存
  AK/SK、签名 URL 或绝对缓存路径。
- 导出的是可重新 `inspect/build/upload` 的规范化工作区，不承诺还原未进入发布物的测试、文档、
  Git 历史和原始 `pyproject.toml` 中与构建无关的工具配置。
- 老 wheel 缺少规范化开发工作区清单时，明确返回 `source_export_unavailable`；已经验证的 wheel 仍可
  保留在缓存，但命令因未满足显式导出请求而返回非零。
- 下载不修改依赖锁、物理图或设备实例，也不执行 `pip install`。

`download` 是本次“只把设备包安全取到本地”的新增产品入口，不属于 #147 已接受命令清单；
本期已经选择该独立命令，不再以 `install --download-only` 代替。

### 7.4 安装（#147 已接受入口）

```bash
unilab --addr uat package install \
  community.szlab_poly_studio@0.1.1 \
  --json
```

`install` 必须复用同一获取结果：可信 resolve → 下载 → Artifact/Catalog parity → 明确目标环境安装
→ `InstalledDistributionSource` 重编译 → 再次 parity。安装不写持久 Package Inventory，也不修改
Graph、不创建设备实例、不连接硬件。普通 pip/Git spec 仍必须在显式目标环境内处理，不得扫描全部
site-packages。

本节用于保持与 #147 的接口方向一致；本期不实现 `install`，也不把它列入验收门禁。

### 7.5 JSON 输出

上传成功：

```json
{
  "schema_version": "unilab-package-command/v1",
  "command": "package.upload",
  "environment": "uat",
  "status": "published",
  "distribution": "unilab-szlab-mock",
  "version": "0.1.0",
  "namespace": "community.unilab_szlab_mock",
  "artifact_digest": "sha256:...",
  "catalog_digest": "sha256:...",
  "content_digest": "sha256:...",
  "definition_fqids": ["community.unilab_szlab_mock.mock_s08_cap_station"],
  "square_verified": true
}
```

下载成功：

```json
{
  "schema_version": "unilab-package-command/v1",
  "command": "package.download",
  "environment": "uat",
  "status": "package_cached",
  "cache_hit": false,
  "cache_key": "community.unilab_szlab_mock@0.1.0#sha256:...",
  "distribution": "unilab-szlab-mock",
  "version": "0.1.0",
  "namespace": "community.unilab_szlab_mock",
  "artifact_digest": "sha256:...",
  "catalog_digest": "sha256:...",
  "content_digest": "sha256:..."
}
```

下载并成功导出开发工作区时，`status` 为 `package_cached_and_source_exported`，并额外返回：

```json
{
  "source_exported": true,
  "source_output": "./unilab-szlab-mock",
  "source_kind": "derived_workspace"
}
```

失败 JSON 必须含稳定错误码、是否可重试和安全消息，不含 AK/SK、Authorization、预签名
URL 或响应正文中的秘密。

## 8. M1-Legacy 上传流程

```text
CLI
  -> Package Build: staging + wheel + self-audit
  -> Backend storage/token: 取得预签名 PUT
  -> OSS: 流式 PUT 同一 wheel
  -> Backend /lab/resource: 发布完整设备/资源模板投影
  -> public square packages/detail: 验证设备广场可见性和三摘要
  -> 输出 published / already_published / uploaded_not_in_square
```

详细约束：

1. **预检。** 只接受规范 Package Workspace；设备广场上传至少包含一个设备定义。
2. **版本不可变预检。** 若广场已有同名同版本：
   - 三摘要完全一致：返回 `already_published`，不重复上传；
   - Artifact 摘要不同：返回 `version_conflict`，要求提升版本。
3. **构建。** 只调用一次 `build_workspace_package()`；后续只消费 `PackageBuildArtifact`。构建结果的
   wheel 必须嵌入第 9.3 节的规范化开发工作区清单，使同一已审计 Artifact 可以在 OS 单侧导出可重建
   工作区，而不额外上传源码包。
4. **OSS token。** M1-Legacy 使用 Backend 已接受的 `scene=file`、
   `content_type=application/octet-stream`，
   可用 `sub_path=packages/<normalized_name>/<version>` 做可读分组。
5. **PUT。** 文件流式发送，不携带 Lab Authorization；限制重定向和超时。
6. **模板投影。** 设备和资源模板来自同一 Catalog；工作流与资产只保留在 wheel 中。
7. **业务响应。** 同时要求 HTTP 200/201、JSON 信封 `code == 0`；HTML 或非法 JSON 也失败。
8. **后置验证。** 重新查询同一环境公开设备广场；每个新定义的包身份和三摘要必须一致。
9. **错误实验室。** 模板上传成功但广场不可见时返回非零并标记
   `uploaded_not_in_square`。提示凭据可能不属于权威实验室；不得宣称发布成功。

M1-Legacy 的 Artifact 上传早于数据库事务。若模板发布失败会留下 OSS 孤儿对象；命令必须报告
`artifact_uploaded=true` 和对象键用于运维清理，但不得自动删除未知远端对象。

## 9. 发布字段合同

### 9.1 `package_info`

必须由已审计 BuildArtifact 生成：

- `name`、`normalized_name`、`version`；
- `class_namespace`、`module_prefix=community`；
- `source_type=community`、`install_spec`、`dependencies`；
- `artifact_digest`，且遗留 `sha256` 必须与它相等；
- `catalog_digest`、`content_digest`；
- `oss_object_key`；
- `download_url` 只作遗留兼容，不作为下载权威。

### 9.2 每个资源模板 DTO

必须至少保留：

- `id`、`source_fqid`、`content_hash`、`registry_type`、`version`；
- `displayname`、`description`、`icon`、`category`、`manufacturer`；
- `model`、`scene`、`device_params`；
- 完整 `class`、设备级 `handles`、`init_param_schema`、`init_param_enforce`；
- 同一对象的 `package_info`；
- 完整 `source_registry`。

`source_registry` 内必须重复保存 `id`、`source_fqid`、`content_hash` 和完整静态注册表合同，
因为遗留 Backend 不持久化未知顶层字段，但会原样持久化 `source_registry`。

动作（Action）合同不得从已移除的 `parameters` 字段重建。发布层应原样投影当前 Catalog 的
`schema`、`goal`、`goal_default`、`result`、`feedback`、Handle 和传输类型。必须以跨仓
fixture 同时验证 Backend 创建 Action 模板和 Cloud 编辑器的字段候选。

资源定义发布的是资源模板，不是物料实例；不得调用 Material 或 Inventory 写接口。

### 9.3 wheel 内的开发工作区导出清单

为支持 OS-only 的二次开发导出，新构建的 wheel 在自身 `.dist-info` 下携带
`unilab_workspace/manifest.json`，至少冻结：

- `schema_version=unilab-derived-workspace/v1`；
- distribution、version、namespace 和唯一顶层 import package；
- 可导出的 Python 源码与资产成员列表，以及每个成员的大小和 SHA-256；
- 用于生成最小可构建 `pyproject.toml` 的 build-system、项目元数据和依赖；
- 根 `package.yaml` 的 canonical bytes（若包声明工作流）；
- 生成该清单时的 `catalog_digest` 和 `content_digest`。

清单属于 wheel 字节，因此受 `artifact_digest`、RECORD 和现有 wheel 自审计共同保护。它只描述构建所需
的规范化工作区，不收录 `.git`、本地虚拟环境、缓存、构建输出、凭据文件或未显式授权的仓库成员。
导出器不得扫描 wheel 外部路径，也不得把任意未知 wheel 成员复制到开发工作区。

## 10. M1-Legacy 下载流程

```text
CLI
  -> Backend package/detail: 解析唯一远端候选
  -> Backend release download endpoint: 请求模板 UUID
  <- 302 OSS signed URL
  -> OSS: 流式下载到受管缓存内临时文件
  -> verify artifact_digest
  -> CachedArchiveSource -> 同一 PackageCatalog compiler
  -> compare name/version/namespace/catalog_digest/content_digest/source_fqid
  -> atomic cache publish
  -> optional verified copy to --out
  -> optional derived workspace export to --extract-source
```

下载端必须：

1. 只访问由 Backend 根地址构造的下载 route，不直接接受任意 `download_url` 参数。
2. 手工处理最多一次 302；跳转目标仅允许 HTTPS，`--addr local` 测试模式可显式允许 HTTP。
3. 跳转到 OSS 时移除 Authorization、Cookie 和追踪头。
4. 流式下载并限制压缩文件大小、成员数、单成员大小、解压总量、压缩比、重复成员、加密成员、
   symlink 和路径逃逸。
5. 先验证 Artifact SHA-256，再读取内嵌 Catalog。
6. 用 `CachedArchiveSource` 从 wheel 内源码和资产重新编译，不只信任内嵌 JSON。
7. 要求远端描述、内嵌 Catalog 和重编译 Catalog 的发行名、版本、namespace、
   `catalog_digest`、`content_digest` 全部相同。
8. 若按模板下载，还要求 `source_registry.source_fqid` 在 Catalog 中唯一存在且为设备定义。
9. 全部校验通过后才原子移动到正式缓存并更新派生元数据；失败不留下可用缓存条目。
10. 仅在缓存提交成功后处理 `--extract-source`；验证开发工作区清单及每个成员摘要，生成最小
    `pyproject.toml`、根 `package.yaml` 和来源记录，再用 `WorkspaceSource` 重新编译。
11. 导出工作区的 name、version、namespace、`catalog_digest` 和 `content_digest` 必须与下载 wheel
    完全一致；不一致时删除临时导出目录并返回 `source_export_incompatible`，不得覆盖目标目录。

## 11. 缓存设计

默认根位于当前受管工作目录：

```text
.unilabos/package-cache/v1/
├── objects/sha256/<hex>.whl
├── verification/<hex>.json
└── locks/<hex>.lock
```

- wheel 以 Artifact digest 内容寻址；同一字节天然幂等。
- `verification/*.json` 只是可重建缓存元数据，不是 Package Inventory，也不保存“当前最新版”。
- 每次打开缓存仍验证普通文件、大小和摘要；需要 Catalog 时重新验证或读取明确版本的验证记录。
- 下载先写同一文件系统内随机临时文件，fsync 后原子替换。
- 并发进程按 digest 取文件锁；等待者在获得锁后重新检查缓存。
- 整个 `package-cache/v1` 可以安全删除；删除不改变 Graph、Inventory 或远端事实。
- 为兼容现有启动链，可在迁移期输出旧格式 cache key，但权威仍是 Artifact digest。

## 12. 鉴权与安全

- 地址必须是无 userinfo、query、fragment 的 HTTP(S) API 根；test/UAT/prod 环境选择固定到一次命令。
- AK/SK 只在短生命周期内编码为 `base64(ak:sk)`；Base64 不是加密，不得记录。
- 上传使用专用短生命周期传输 Adapter，不复用会打印 `auth_secret` 的旧全局 HTTP client。
- 诊断文件默认关闭；显式开启时只保存脱敏请求摘要、状态码、Backend request ID 和错误码。
- stdout JSON 不输出对象键、预签名 URL 或本地绝对缓存路径；人类模式也不打印签名参数。
- 不自动执行下载 wheel 中的代码；PackageCatalog 编译必须 AST-only。
- `--extract-source` 只复制清单授权的普通文件并生成规范化文本；禁止 symlink、硬链接、设备文件、
  路径逃逸和覆盖已有目录，导出校验过程同样不得 import 或执行包代码。
- 下载成功不自动安装依赖。依赖安装属于独立、需要明确授权的操作。

## 13. 幂等、并发和失败语义

| 阶段 | 可安全重试 | M1-Legacy 处理 |
|---|---|---|
| 本地 build 失败 | 是 | 不产生远端副作用 |
| storage token 失败 | 是 | 重新申请 |
| OSS PUT 超时且结果未知 | 有条件 | 重新申请新 token；报告可能的孤儿对象 |
| `/lab/resource` 业务失败 | 有条件 | 不重建 wheel；重试模板发布前重新查广场 |
| 上传后广场不可见 | 否，需先诊断权限 | 返回 `uploaded_not_in_square` |
| 同版本同摘要已存在 | 是 | `already_published` |
| 同版本不同摘要 | 否 | `version_conflict`，禁止覆盖 |
| 下载网络失败 | 是 | 删除临时文件，保留旧已验证缓存 |
| 摘要/Catalog 不一致 | 否 | `remote_package_incompatible`，要求重新发布 |
| 缓存损坏 | 是 | 隔离损坏文件并重新下载 |
| 显式源码导出但 wheel 无清单 | 否 | wheel 可保留缓存，返回 `source_export_unavailable` |
| 源码导出目标已存在 | 否 | 返回 `source_output_exists`，绝不合并或覆盖 |
| 导出后 Workspace parity 失败 | 否 | 删除临时导出目录，返回 `source_export_incompatible` |

M1-Legacy 无法彻底解决两个竞态：发布预检与 upsert 之间的同版本竞争，以及新版本删除定义后旧模板残留。
它们由 M2-Canonical 的 Backend 唯一约束和原子 replace 解决。

## 14. 后续方向（非本期）：M2-Canonical Package Release

本节不属于本期实施范围，不允许据此修改 Backend 或 Cloud。它只说明未来如何替换遗留 Adapter，
并记录遗留协议无法在 OS 单侧彻底解决的问题。

### 14.1 Backend 数据

候选 `package_release` 至少保存：

- `uuid`、`authoritative_lab_id`；
- distribution name、normalized name、version、namespace；
- Artifact/Catalog/content 三摘要、Artifact size、OSS object key；
- 完整 canonical PackageCatalog JSON 和编译器/schema 版本；该发布事实包含设备、资源、动作、
  工作流和资产身份，Cloud 可以只投影其中一部分，但不能反向删减发布权威；
- `status=staging|published|deprecated`、发布人和时间；
- 唯一约束 `(authoritative_lab_id, normalized_name, version)`。

候选 `package_release_definition` 保存 release 与 `source_fqid`、定义种类、内容摘要、模板 UUID
的关系。它不替代 ResourceTemplate；ResourceTemplate 仍是当前可展示/可复制投影。

### 14.2 API

```text
POST /api/v1/lab/square/package-releases/prepare
POST /api/v1/lab/square/package-releases/{releaseUuid}/publish
GET  /api/v1/lab/square/package-releases/resolve?name=...&version=...
GET  /api/v1/lab/square/packages/releases/{releaseUuid}/download
```

- `prepare` 在签 PUT 前检查权威实验室发布权限和版本冲突，返回 release UUID 与预签名 PUT。
- `publish` 验证同一 release 身份，在一个数据库事务中 replace 完整定义集合并置为 published。
- `resolve` 返回安全描述与相对下载 route，不返回对象键或长期 URL。
- 下载 route 只按真实 release UUID 签发短效 GET。
- 现有模板 UUID 下载可保留一段兼容期，响应头标记 legacy；新客户端优先真实 release。

Cloud 的包列表/详情最终应从 published release 聚合，模板只负责展示定义。这样包版本、
设备数量和下载 Artifact 不再由多行 `package_info` 猜测。

## 15. 实施顺序

以下 R0–R4 全部由 Uni-Lab-OS 仓库承担；对 Backend 和 Cloud 只调用已有接口，不提交代码改动。

### R0：锁定既有协议能力

- 按第 7.1 节冻结“省略 `--addr` → 正式环境”以及 `test`、`uat`、`prod/production` 到三个 API 根
  的精确映射，修正当前 UAT 域名和 package 独立解析器缺少正式环境别名的问题；
- 记录实际部署仓库、提交、migration 和路由；
- 实现只读 capability probe 与遗留 DTO fixture；
- 只接受 `legacy-template-package/v1`，不在本期协商或实现 v2；
- 不兼容环境稳定返回 `backend_incompatible`。

### R1：来源与下载内核

- 实现严格的 `PackageCatalog.from_canonical_bytes()`；
- 增加 `CachedArchiveSource`，复用当前 wheel 安全审计；
- 证明 Workspace 与 CachedArchive 两种来源 canonical parity；
- 实现内容寻址缓存、锁和原子发布。
- 在新 wheel 中写入规范化开发工作区清单，并实现安全、原子的派生工作区导出。

### R2：远端解析与下载 CLI

- 实现 Backend package/detail 解析 Adapter；
- 增加候选 `package download` 和稳定 JSON；
- 增加 `--extract-source DIR`，并保证导出结果可以再次通过 `inspect`、`build` 和 Catalog parity；
- 把旁支 UAT fixture 迁移到当前 Catalog 模型；
- 让遗留 `app/community_packages.py` 委托新 acquisition，而不是重复解压。

### R3：上传硬化

- 拆分纯 publication 与 HTTP Adapter；
- 修正 scene/MIME/流式 PUT/业务码；
- 冻结 ResourceTemplate/Action/Handle 字段合同；
- 接入 session 与 `--auth-stdin`；
- 增加上传后设备广场验证。

### R4：OS 合同与既有环境 UAT

- OS 内用固定遗留 Backend 响应 fixture 做合同测试；
- test/UAT/prod 分别执行只读 capability probe，证明选择结果不会跨环境回退；
- test/UAT 真实 AK/SK + OSS 上传下载；
- 正式环境默认只做只读探测；真实上传下载必须使用正式发布凭据，并由独立发布审批授权；
- 同一 wheel 上传前后 SHA-256 对账；
- 下载缓存后从 Catalog 查询设备、资源、工作流定义；
- 不连接真实硬件的 mock device 启动与 Action smoke test。

### Future-R5：Backend release 升级（不在本期）

- 新表、新 API、权限和原子发布；
- Cloud 改从 release 聚合；
- CLI 优先 v2，保留有期限的 v1 兼容；
- 清理模板 UUID 作为 release 身份的遗留语义。

## 16. 测试与验收

### 16.1 OS 单元/合同测试

- Workspace、构建 wheel、下载 wheel 的 PackageCatalog canonical bytes 完全相同；
- Artifact/Catalog/content 任一摘要错误均不发布缓存；
- wheel 路径逃逸、symlink、重复成员、ZIP bomb、额外顶层 import root 被拒绝；
- 上传只构建一次，且没有外部 URL 绕行参数；
- 省略 `--addr`、`--addr prod` 和 `--addr production` 都精确解析为正式 API 根；`test`、`uat` 精确
  解析为第 7.1 节对应 API 根；完整 URL 不自动补 `/api/v1`，且任何阶段不得静默切换环境；
- HTTP 200 + `code != 0` 被判失败；
- OSS PUT 不携带 Lab Authorization；
- stdout JSON 只有一行，stderr 不含凭据或签名 URL；
- 缓存命中不访问网络；并发下载只提交一个对象；
- `--extract-source` 只从已验证缓存对象导出；目标存在、成员摘要错误、路径逃逸或 parity 失败均不
  留下目标目录；
- 导出工作区重新 `inspect/build` 后的 Catalog canonical bytes 与原发布完全一致；
- 下载不修改 Graph、依赖锁、Inventory 或物料数据。

### 16.2 OS 内的外部协议 fixture 与 UAT

- OS 发布 fixture 经 Backend `ResourceReq` 绑定后，顶层 class、Handle、初始化 Schema、
  package_info、source_registry 无字段丢失；
- Backend 生成的模板详情可被 Cloud `PackageInfo` 和编辑器读取；
- Cloud 对动作输入候选能读取 `schema.properties` / `goal.properties`；
- Backend 下载 route 仅对权威实验室且有 `oss_object_key` 的发布返回 302；
- 包名解析遇到同包混合 Artifact 时失败关闭。

### 16.3 本期 OS-only 完成定义

1. 当前分支 CLI 可上传 mock 设备包到指定 UAT 环境；
2. 上传后公开设备广场可见，模板中的 `source_fqid` 与三摘要完整；
3. 清空本地缓存后，CLI 只凭模板 UUID 或包名下载同一 wheel；
4. 下载 SHA-256 与上传 Artifact 完全一致；
5. `CachedArchiveSource` 重编译得到与上传前完全相同的 PackageCatalog；
6. 重复上传/下载得到幂等结果；
7. 全程不创建物料实例、不改 Graph、不连接硬件；
8. 无权限、错误实验室、业务码错误和旧包字段缺失都有稳定非零退出与安全诊断；
9. 对本期新构建并上传的 wheel，`download --extract-source DIR` 可得到一个能够再次通过
   `inspect/build` 和 Catalog parity 的派生 Package Workspace；
10. 交付 diff 不包含 uni-lab-backend、Uni-Lab-Cloud 或 Uni-Lab-SZLab 代码改动。

## 17. 已冻结决策与待确认项

已冻结：

- 本期唯一代码改动仓库是 Uni-Lab-OS；Backend、Cloud 和 SZLab 均不修改。
- 当前 upload 只是已注册骨架，download 尚不存在；本期完成后两者统一从 OS CLI 提供。
- 公共环境地址冻结为第 7.1 节的 test、UAT 和正式 API 根；省略 `--addr` 默认正式环境，`prod` 与
  `production` 是等价的显式写法；当前代码中的 UAT 旧域名和包命令正式环境别名缺口必须修复。
- OS 侧传输端口、构建/审计/缓存内核不依赖具体 Backend DTO。
- 本期只实现 `legacy-template-package/v1` Adapter；M2-Canonical 和 `install` 均延期。
- 上传 Artifact 只能是本次已审计 wheel。
- 下载选择器不接受外部 URL、调用方自报摘要或自报 FQID。
- 下载始终先进入可丢弃缓存，不安装、不激活；显式 `--extract-source DIR` 可以额外导出派生开发
  工作区，但不改变缓存权威。
- 本期不上传第二个源码 Artifact；二次开发工作区由 wheel 内受审计清单和已打包源码在 OS 单侧生成。
- 设备广场上传至少包含一个设备定义；资源模板随包发布，工作流不走 `/lab/resource`。
- 同版本不同 Artifact 失败关闭。

进入真实环境 UAT 前仍需获得或探测以下运行条件；它们不会触发本期 Backend/Cloud 改造：

- test/UAT/prod 的权威 Backend 仓库、提交和 migration 版本；
- 哪些 Lab AK/SK 有权写权威实验室，M1-Legacy 是否允许普通用户看到上传入口；
- 资源定义随设备包上传后，是否需要进入资源广场的独立审核流程；
- 目标环境是否确实保留模板 `package_info/source_registry` 与模板 UUID 下载路由。

## 18. 证据索引

- [调研记录](../research/2026-08-10-device-package-upload-download-research.md)
- [与 Core #147 / SZLab 的符合性复核](../research/2026-08-10-device-package-architecture-conformance-review.md)
- [Uni-Lab-Core#147](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/147)
- `unilabos/package_manager/package_distribution/build.py`
- `unilabos/package_manager/package_distribution/adapters/cloud.py`
- `unilabos/package_manager/package_distribution/legacy_projection.py`
- `unilabos/app/community_packages.py`
- `unilabos/app/web/client.py`
- `/home/sunmenglei/Uni-Lab-Cloud/web/src/services/square.ts`
- `/home/sunmenglei/uni-lab-backend/pkg/web/router.go`
- `/home/sunmenglei/uni-lab-backend/pkg/core/environment/laboratory/laboratory.go`
- `/home/sunmenglei/uni-lab-backend/pkg/core/square/square/square.go`
- `/home/changjunhan/Uni-Lab-Core/uni-lab-backend/migrations/postgres/000035_remove_resource_registry_metadata.up.sql`
- `/home/changjunhan/Uni-Lab-Core/uni-lab-backend/migrations/sqlite/000035_remove_resource_registry_metadata.up.sql`
- `/home/sunmenglei/acceptance-artifacts/szlab-mock-uat-20260807/acceptance-report.md`
