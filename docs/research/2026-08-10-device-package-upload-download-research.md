# 设备包上传/下载：当前实现与设计约束调研

> 日期：2026-08-10
> 性质：一手代码与 Issue 调研记录，不是已冻结 API 规范
> OS 基线：`product/durable-scheduler-kernel@aee69e0364b8f034daf9edd1a2d5c97df056f702`

> 2026-08-10 范围决策：第一阶段只修改 Uni-Lab-OS，在 OS CLI 中硬化上传并新增下载；
> uni-lab-backend 与 Uni-Lab-Cloud 不修改。目标环境若不具备被调研的遗留接口，OS 返回不兼容，
> Package Release v2 留到后续独立项目。下载可显式使用 `--extract-source DIR`，从已验证 wheel
> 在 OS 单侧导出派生软件包工作区（Derived Package Workspace），不上传第二个源码 Artifact。

## 1. 结论摘要

1. 当前 OS 的软件包扫描、确定性软件包目录（Package Catalog）、wheel 构建与自审计已有完整主干：作者代码仅做 AST 静态解析，构建后会从 wheel 重编译并校验目录 parity、资产闭包和 `RECORD`。
2. `unilab package upload` 已存在，且正确复用一次 `build`的已审计 wheel；但它目前只适配旧的 OSS token + `/lab/resource` 协议。OS 发送 `scene=models`，而被调研的 Backend 只接受 `model`、`file` 等枚举；OS 还把 wheel 固定标为 `application/gzip` 并整件读入内存。因此上传编排已有，现有真实端到端集成不能判定为可用。
3. 当前 Package CLI 没有 `download` 或 `install`。`unilabos/app/community_packages.py` 有一条启动时的旧下载/解压链，但它不经过软件包管理器（Package Manager）、没有 `CachedArchiveSource`、允许缺失 SHA-256 时跳过校验，也没有 wheel/目录 parity 审计，不应直接成为新 CLI 的实现。
4. Uni-Lab-Cloud 当前是设备广场/设备包元数据的展示和模板复制消费者，没有设备包产物上传服务，也没有包产物下载入口。
5. `/home/sunmenglei/uni-lab-backend` 的 `test` 分支快照有设备包广场、resolve、OSS 签名和公开 302 下载，但它没有独立的包发布（Package Release）聚合：`package_info` 被重复存在每个资源模板上，公开路由中的 `releaseUUID` 实际是模板 UUID。更新的 Uni-Lab-Core Backend 迁移又删除了 `package_info`/`source_registry`，因此旧 Backend 协议只能视为兼容参考，不能默认为已收敛的云端合同。
6. Core Issue #147 已明确了本地包管理架构和安全边界，但没有冻结云端 endpoint、鉴权、OSS 协议、幂等语义或广场 DTO。它仍是 Open / `stage:testing`，不应把候选代码误写成已发布产品合同。
7. 历史旁支 `feature/electron-device-provisioning@6a9a549` 已实现受控下载、Catalog 校验、设备图写入和 Electron 接入，并有 2026-08-07 真实 UAT Cloud/OSS/Edge 验收记录。这证明旧 Backend 兼容协议可跑通，但该代码不在当前 durable 分支，且使用旧 Package Manager 布局；它是已验证供体，不是当前实现。
8. 本期新增的派生软件包工作区导出是目标设计，不是当前实现。它要求新 wheel 内嵌受 `RECORD` 和 Artifact 摘要保护的规范化工作区清单；老 wheel 仍可下载和缓存，但缺少清单时不能承诺可重建源码工作区。

## 2. 范围与证据快照

| 来源 | 调研快照 | 用途 |
| --- | --- | --- |
| Uni-Lab-OS | `product/durable-scheduler-kernel@aee69e0` | 当前 CLI、Package Catalog、build/upload 和启动旧下载 |
| Core Issue #147 | 2026-08-10 通过 authenticated `gh api` 读取；Open，`stage:testing` | 目标架构和已批准边界 |
| Uni-Lab-Cloud | `main@6d69d27` | 设备广场 Web 消费端 |
| uni-lab-backend | 本地 `test@2d94a64`（本地分支 ahead 10 / behind 18） | 旧广场、OSS 和模板落库协议；仅作兼容快照 |
| Uni-Lab-Core/uni-lab-backend | `origin/main@d5520789`、`origin/feat/workflow@fa269e9d` | 当前 Core Backend 数据模型收敛方向；两者均无 Package Release 路由 |
| OS/FE 设备接入旁支 | OS `feature/electron-device-provisioning@6a9a549`；FE 同名分支 2026-08-07 记录 | 已验证行为与测试供体；不代表 durable 现状 |

本文只使用上述代码、测试和 GitHub Issue/评论。代码路径后的行号基于上表快照，后续提交可能使行号漂移。

## 3. 能力状态矩阵

| 能力 | 当前实现 | 性质 | 判断 |
| --- | --- | --- | --- |
| `package inspect` | CLI → `WorkspaceSource` → AST compiler → Package Catalog/遗留投影 | 当前主干 | 已实现 |
| `package build` | stage → compile → wheel → `RECORD`/闭包审计 → clean-wheel 重编译 parity | 当前主干 | 已实现 |
| `package upload` 编排 | 构建一次、上传同一 wheel、投影资源模板 | 当前代码 + 旧协议 Adapter | 局部实现 |
| `package upload` 真实云集成 | `scene=models` + `application/gzip` + `/lab/resource` | 未验证兼容链 | 存在已知协议错配 |
| CLI 下载 | 无 `download`、无 `install` 子命令 | 当前主干 | 未实现 |
| 派生软件包工作区导出 | 当前无 `--extract-source`，wheel 无规范化工作区导出清单 | 本期目标设计 | 未实现；仅 OS 改动 |
| Graph 启动时远程包准备 | `app/community_packages.py` resolve/下载/解压/交给旧 Registry 扫描 | 遗留兼容 | 存在，不满足新 Package Manager 安全合同 |
| `CachedArchiveSource` | 当前 `sources.py` 只有 `WorkspaceSource` | Issue #147 目标 | 未实现 |
| `InstalledDistributionSource` | 当前无 | Issue #147 目标 | 未实现 |
| 云端包列表/详情 | Backend 按 `package_info.name` 聚合，Cloud 展示 | 遗留兼容 | 已实现 |
| 云端包发布聚合 | 无独立表/状态机，元数据重复在资源模板 | 遗留兼容 | 未实现 |

## 4. 当前 OS 软件包管理器

### 4.1 CLI 与发现 seam

`unilabos/package_manager/cli.py:115-200` 只注册 `inspect`/`build`/`upload`/`add`/`update`/`remove`，分派闭合也仅包含这些动作（同文件 `:220-283`）。因此“有启动时远程包下载代码”不等于“有 Package CLI 下载”。

`unilabos/package_manager/package_catalog/sources.py:11-135` 当前只定义 `WorkspaceSource`，它对根目录、相对路径、符号链接、越界和普通文件类型做封闭检查。`workspace_runtime/discovery.py:22-35,415-449` 的编译入口仍直接消费 `WorkspaceSource`，说明 Issue #147 所要求的三种 source-neutral 来源还没有在本分支完整落地。

### 4.2 Package Catalog 已经拥有的事实

`unilabos/package_manager/package_catalog/model.py:133-380` 的 schema v1 包含：

- distribution：原始名、规范名、版本、描述、许可、主页、Python 版本和依赖；
- `import_package` 与 `namespace`；
- device/resource/workflow definition：`id`/FQID、module、symbol、declaring file、content hash、版本和 `details`；
- 资产逻辑路径、SHA-256 和大小；
- `content_digest` 与排除自身字段后以 RFC 8785 规范 JSON 复算的 `catalog_digest`。

这里的 resource 是包内定义，不是已入库的物料（Material）实例。物料的库位、数量、形态等变化态事实不应进入 Package Catalog；包只声明定义，Graph 才选择运行时实例及连接配置。

### 4.3 构建与自审计

`unilabos/package_manager/package_distribution/build.py:94-202` 负责一次完整构建：在临时 staging 中编译 Catalog、写入 `_generated/package.catalog.json`、构建标准 wheel、计算产物摘要、从最终 wheel 自审计，最后才原子发布投影和 wheel。`build.py:216-581` 还检查：

- wheel 总大小、成员数、单成员大小、解压总大小和压缩比；
- 重复成员、路径逃逸、符号链接/非普通成员；
- wheel 顶层载荷和唯一 `RECORD`，以及每个成员的摘要/大小；
- 定义和资产闭包；
- 从 wheel 重建干净工作区后的 distribution/import package/namespace/digest/Catalog parity。

当前限额是 wheel 2 GiB、单成员 1 GiB、解压总量 4 GiB、50,000 成员和 1000 压缩比（`build.py:50-54,450-519`）。`build.py:649-680` 把 `artifact_digest`/`catalog_digest`/`content_digest` 放入遗留 `package_info`，使云端 Adapter 可以携带同一产物世代的证明。

当前构建结果还没有在 `.dist-info/unilab_workspace/manifest.json` 保存规范化工作区导出清单，
当前 CLI 也没有 `--extract-source`。因此从 wheel 导出可再次 `inspect/build/upload` 的工作区必须
标记为本期目标设计，不能把现有 clean-wheel 自审计临时目录误写成已交付的用户功能。

### 4.4 当前上传链

实际调用链是：

```text
package upload
  → build_package
  → build_workspace_package（已审计 wheel）
  → GET /api/v1/lab/storage/token
  → PUT 预签名 OSS URL
  → POST /api/v1/lab/resource
```

`package_distribution/adapters/cloud.py:16-81` 用 `PublicationPort` 分离“上传产物”和“发布资源投影”。`cloud.py:84-176` 先上传 wheel，再把 `download_url`/`oss_object_key` 写回同一 `package_info`，附加到每个资源 DTO，然后调用 `/lab/resource`。它不重新扫描或重新构建，这是正确的深模块边界。

但传输 Adapter 有明确错配：

- `cloud.py:59-68` 固定传 `scene="models"`；旧 Backend `pkg/repo/storage_token.go:9-27` 只接受 `default/job/model/image/file/record/device_icon`，不接受 `models`。
- `app/web/client.py:489-529` 固定 `application/gzip`，注释也假设产物是 `tar.gz`；当前 builder 上传的却是 wheel/ZIP 容器。
- 同一方法 `file.read()` 后才 `requests.put`，没有流式上传。
- OSS PUT 成功后才 POST 模板，两个系统间没有 publish session/finalize/幂等键；第二步失败会留下孤儿对象。
- 上传命令仍通过旧 Web client 和运行时组合根取 AK/SK，没有复用 `app/cli/auth_resolver.py` 已有的 CLI > session > local config 统一鉴权解析。

### 4.5 遗留下载链及不可直接复用的原因

`unilabos/app/community_packages.py:89-214` 只在 Graph 出现 `community.*` 引用时解析缺失 namespace，并把结果作为旧 Registry 的额外扫描目录。`community_packages.py:217-318` 通过鉴权 resolve 获得 `package_info.download_url`，以 namespace/version/sha256 做 `community_devices/manifest.json` 缓存。

`community_packages.py:321-383,465-491` 的具体下载/解压存在这些缺口：

- 没有 Content-Length/实际下载大小上限；
- `sha256` 缺失时明确跳过校验；
- 解压只检查成员路径 containment，没有检查符号链接、特殊文件、重复成员、成员数/大小/压缩比；
- 落盘前会直接 `shutil.rmtree(target_root)`，不是不可见的原子切换；
- 从解压结果中选排序后的第一个 `pyproject.toml`，再只复制其父目录；
- 没有读取内嵌 Package Catalog，没有运行 CachedArchiveSource 编译，没有校验 artifact/catalog/content digest 和 definition FQID。

进一步的代码推断：新 builder 把项目文件嵌入 `<import_package>/_generated/pyproject.toml`，而旧 downloader 会把“第一个 `pyproject.toml` 的父目录”当作整包。对新 wheel，它可能只复制 `_generated`，丢失真实 Python 载荷。所以当前 upload 所产生的 wheel 不能假设可被这条旧链可靠激活。

### 4.6 测试现状

- `tests/package_manager/test_package_build_audit.py:313-391` 证明 upload 只构建一次，并上传同一已审计 wheel；它使用 mock HTTP client。
- `tests/package_manager/test_package_distribution_module_layout.py:364-620` 验证产物上传先于资源发布、URL/object key 传播、传输异常语义和禁止外部 `download_url` 绕过。
- `tests/package_manager/test_package_dependency_cli.py:193-218` 确认 parser 拒绝 `--download-url`。
- `tests/package_manager/test_package_cli_subprocess_contract.py` 覆盖 inspect/build/add/update/remove 的真子进程合同，没有真实网络 upload 或 download 端到端测试。

本次调研尝试重跑相关测试，但当前环境只有未安装 pytest 的系统 `python3`，`python3 -m pytest --version` 在收集前即因缺少模块失败。因此上述结论来自测试代码审读，不声称本次已运行通过。

### 4.7 旁支已验证实现和 UAT 证据（非当前 durable 分支）

OS 历史旁支 `feature/electron-device-provisioning@6a9a549` 包含 `package_manager/community.py`、`CachedArchiveSource`、`package download`、缓存索引、digest/Catalog/namespace/definition FQID 校验和设备图接入；`tests/package_manager/test_device_package_download.py` 覆盖下载、缓存命中、摘要不匹配、definition 缺失、配置 Schema 和 CLI JSON。当前 `product/durable-scheduler-kernel@aee69e0` 中不存在这些下载入口。

`/home/sunmenglei/uni-lab-fe/docs/architecture/cloud-electron-device-package-transfer.md:787-849` 记录了该旁支与 Electron 的验证结果：

- 2026-08-06 将 `community.unilab_szlab_mock@0.1.0` 上传到 UAT，同一 UAT 广场发现后经 OSS 下载、配置、原子写图并启动 Edge；
- 截至 2026-08-07，文档记录 OS Package Manager `97 passed`、OS 全量 `2647 passed, 7 skipped`、专项 OS `26 passed`；
- 真实 UAT Playwright 用例 `device-square-uat-real-edge.spec.ts` 记录 `1 passed`，覆盖广场、OSS 下载、写图、Edge health 和 4 个 Action 对账。

这些是仓库内的历史 UAT 记录，本次未重跑 UAT；其证明“旧 Backend 协议 + 受管 wheel 下载 + Catalog 验证 + Graph 激活”在指定候选分支和 mock 设备上曾端到端跑通，不证明 durable 分支已拥有该功能，也不替代物理仪器连接和真实业务 Action 验收。

## 5. Core Issue #147 的目标合同

规范来源：[Uni-Lab-Core #147](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/147)。当前标题为“[Wayfinder] 领域设备包仓库架构：根单包、Workspace 自动发现与 Package Catalog”，状态 Open，label 是 `stage:testing` 和 `wayfinder:map`。Issue 正文中残留的 `stage:implementation`/`stage:protocol-definition` 是历史文字，应以当前 label 和最新评论为准。

与上传/下载直接相关的约束是：

1. 仓库根 = Python distribution root = OS package workspace root；一仓库一个 distribution、一个顶层 import package。
2. 软件包管理器是唯一发现 seam：`PackageSource -> PackageCatalog`。`WorkspaceSource`、`InstalledDistributionSource`、`CachedArchiveSource` 必须生成同一种与来源无关的 Catalog。
3. 职责集中在 `unilabos/package_manager/`；`app/main.py` 只是组合根和依赖注入点。
4. 目标 CLI 是 `inspect --path .`、`build --path .`、`upload --path .`、`install <spec>`；`upload` 必须复用 `build`。`install` 是安装 + Catalog 检查，但 OS 不建立持久 Package Inventory。
5. 上传必须经过 `WorkspaceSource -> strict compile -> staged wheel build -> clean-wheel recompile/Catalog parity -> publication`。
6. 下载/安装必须经过 `community resolve/download -> digest verification -> disposable cache -> CachedArchiveSource -> 同一 compiler/Catalog parity -> install/Graph resolve`。
7. 不新增独立 `package_id`，不建持久 Package Inventory；cached archive 是可丢弃缓存。稳定定义身份是 `community.<normalized_distribution>.<definition_id>`。
8. Graph 独占运行时实例 ID、definition FQID、连接/初始化 config 和 topology；Catalog 只拥有 definition。发现、下载、安装不得自动 import、实例化或连接硬件。
9. 安全门禁包括 AST-only、重复/动态 ID fail-fast、资产闭包、路径 containment/符号链接/文件类型、wheel package-data/`RECORD`，以及 workspace/clean-wheel/cached-archive 身份与 Catalog parity。
10. `download --extract-source` 是 #147 之外的候选分发扩展。导出结果必须重新经过
    `WorkspaceSource -> PackageCatalog` 并与原 wheel 保持 Catalog parity；它不新增 Package Source、
    不建立持久 Package Inventory，也不等于还原原始 Git 仓库。

核心安全句是：**发现全部定义 ≠ 实例化全部设备 ≠ 连接全部硬件**。

关键评论证据：

- [Package Workspace 与 Lab Workspace、Catalog definition 与 Material instance](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/147#issuecomment-5149754930)
- [Package Manager 代码归属](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/147#issuecomment-5150181143)
- [Graph 独占实例配置与删除 Profile 主路](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/147#issuecomment-5150557029)
- [完整规范人工批准](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/147#issuecomment-5150783258)
- [R1 compiler/inspect 完成记录](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/147#issuecomment-5150915313)
- [最新本地候选与测试记录](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/147#issuecomment-5209717285)

Issue #147 没有冻结 HTTP endpoint、鉴权、OSS 对象协议、上传会话/幂等键、签名下载 URL 或广场 DTO，这些不能从 Issue 中杜撰。

## 6. Cloud 和旧 Backend 现状

### 6.1 Uni-Lab-Cloud 只是广场消费端

`web/src/services/square.ts:340-379` 提供模板复制、包列表和包详情，没有包产物 upload/download API。`web/src/services/storage.ts:8-35` 的通用 OSS scene 与旧 Backend 一致，也没有 `package` 或 `models`。

`web/src/types/square.ts:98-116,739-772` 仍是遗留 `PackageInfo`：它有 name/version/source/install/download_url/sha256/class_namespace 等字段，但没有 `artifact_digest`、`catalog_digest`、`content_digest`、`oss_object_key`、`source_fqid`。包列/详情页展示安装命令和设备列表，不是产物发布或下载权威。

### 6.2 旧 Backend 协议和鉴权

`pkg/web/router.go:112-147,170-177` 暴露的相关路由是：

| 路由 | 鉴权 | 当前用途 |
| --- | --- | --- |
| `GET /api/v1/lab/square/packages` | 公开 | 按 `package_info.name` 聚合包列表 |
| `GET /api/v1/lab/square/packages/:packageName` | 公开 | 包详情和包内设备 |
| `GET /api/v1/lab/square/packages/releases/:releaseUUID/download` | 公开 | 校验权威实验室模板后 302 到 OSS |
| `POST /api/v1/lab/square/community-packages/resolve` | `AuthWeb` | Graph 按 `community.*` 解析包 |
| `GET /api/v1/lab/storage/token` | `AuthWeb` | 签发 OSS PUT URL |
| `POST /api/v1/lab/resource` | `AuthWeb` | upsert 资源/设备/动作模板 |

`pkg/middleware/auth/middleware.go:103-122,144-179,220-248` 显示 `AuthWeb` 支持 Bearer、Lab 和 API 三种 header。OS 现用 `Authorization: Lab <base64(ak:sk)>`，Backend 解码后校验 AK/SK。公开 302 本身不需 Lab header，并在 `pkg/core/square/square/square.go:1222-1264` 对缺失/JSON 无效的 `package_info`、非权威模板、缺失 `oss_object_key` 或签名失败采取 fail-closed，不回退到库中 `download_url`。

鉴权 resolve 的语义不同：`square.go:1147-1219,1277-1283` 按 namespace 查模板并取组内第一个非空 `package_info`，优先签 OSS GET URL，签名不可用时回退已存 `download_url`。

### 6.3 OSS 与持久化模型

`pkg/core/storage/storage/storage.go:75-153` 按 `{scene}/{optional_sub_path}/{uuid}/{filename}` 生成对象键，把 Content-Type 纳入 PUT 签名，返回 URL/path/public_url/content_type/expires，并保存 StorageToken 记录。这是通用对象上传 token，没有包特有 scene、发布状态或不可变版本约束。

`pkg/core/environment/laboratory/laboratory.go:420-504` 把资源节点模板、handle、action 和 action handle 放在一个数据库事务中。`pkg/repo/environment/environment.go:136-180` 用 `(lab_id,name)` upsert 模板并更新 `package_info`/`source_registry`。这保证了本地数据库写的事务性，但还有四个包级缺口：

- 没有包发布或包版本表，`package_info` 重复于每个模板；
- resolve 只取组内首个元数据，没有包内模板间一致性约束；
- 重复上传不会删除新版本已移除的旧定义/handle/action，存在 stale projection；
- OSS 上传和模板数据库事务无法原子提交。

`pkg/core/square/model.go:361` 已明确 `release_uuid` 在 MVP 阶段即 template UUID，因此不应在 CLI 或缓存索引中把它当作稳定的包发布身份。

### 6.4 与当前 Core Backend 的分歧

`/home/changjunhan/Uni-Lab-Core/uni-lab-backend/migrations/{postgres,sqlite}/000035_remove_resource_registry_metadata.up.sql` 在最新读取的 `origin/main@d5520789` 和 `origin/feat/workflow@fa269e9d` 中均删除 `resource_template.package_info` 和 `source_registry`，且两个远端快照中都未找到上述 square/package route 或 Package Release 替代路由。这与旧 Backend 通过模板 JSONB 承载包元数据的做法直接分歧。

因此设计必须明确选择：

- 兼容模式：对实际部署且支持旧路由的 Backend 保留协议 Adapter/能力探测；
- 目标模式：为包发布建立与 ResourceTemplate 分离的云端聚合和 API，模板只是 Package Catalog definition 的广场投影。

不应把旧 JSONB 布局固化到新 Package Manager 的领域接口中。

## 7. 对 CLI 上传/下载设计的约束与建议

### 7.1 软件包管理器内的深接口

建议把云端交互收口为两个端口，而不让 CLI 或 `app/community_packages.py` 知道 Backend DTO：

```text
PackagePublisher.publish(audited_build, auth_context) -> PublishedDescriptor
PackageAcquirer.acquire(selector, auth_context, cache_policy) -> AcquiredPackage
```

- `PackagePublisher` 只消费 `PackageBuildArtifact`，不接受作者自填 `download_url`、digest 或 Catalog。
- `PackageAcquirer` 先从可信 Backend 解析 selector 为发布描述，然后下载并校验；不允许默认消费任意用户 URL。
- Backend-specific HTTP/OSS/DTO 封在 Adapter 中；Package Catalog 编译器和缓存不依赖 Cloud/Backend。
- `app/main.py` 只组合 Adapter、鉴权和命令，不实现包协议。

### 7.2 上传流程

```text
WorkspaceSource
  → strict compile/build/self-audit
  → 产生唯一 publication manifest
  → 申请上传会话/预签名 PUT
  → 流式 PUT 同一 wheel
  → publish/finalize（包身份 + digest + Catalog + 模板投影）
  → 读回已发布 descriptor 并对账
```

必须保留的包身份至少包括 distribution normalized name、version、namespace、`artifact_digest`、`catalog_digest`、`content_digest`。definition 以 FQID 引用。不新增 OS 本地 `package_id` 或持久 Package Inventory。

对当前旧 Backend 的最小兼容修正是：

1. 先做 endpoint/capability probe；不得在不支持的 Backend 上静默退化。
2. 在 Backend 还没有 package scene 时，使用其已接受的 `file` + `application/octet-stream`，可用 `sub_path=device-packages/<namespace>/<version>` 分层；目标协议应增加明确 `package` scene。
3. 使用文件流 PUT，限制连接/读取超时，不把整个 wheel 读入内存。
4. 只从已审计 build 生成顶层 `package_info` 和资源投影，附带三个 digest 和每个 definition FQID。
5. POST `/lab/resource` 失败时明确报告“产物已上传、发布未完成”，记录 object key 供重试/回收；不误报为全量成功。

后续 v2 若另行授权 Backend 改造，应建独立包发布聚合，示意字段是：`release_uuid`、publisher/lab、distribution/namespace/version、object key、artifact/catalog/content digest、Catalog JSON、`pending|published|deprecated` 状态和时间戳；模板以 release/definition FQID 关联，不再重复存整块 `package_info`。这是非本期的云端发布索引，不违反 Issue #147 “OS 不建持久 Package Inventory”的本地边界。

### 7.3 下载流程

```text
selector
  → Backend resolve 得到可信 descriptor
  → 同文件系统临时文件流式下载
  → 大小 + artifact SHA-256
  → wheel 安全审计/RECORD
  → CachedArchiveSource 用同一 compiler 编译
  → distribution/version/namespace/catalog/content digest/FQID parity
  → fsync + atomic rename 进入可丢弃缓存
  → 可选：按受审计清单原子导出派生软件包工作区
  → 返回 Catalog/产物路径，不 import、不实例化、不连接硬件
```

用户所需“下载设备包”定义为 acquire/cache，不应偷渡成运行时激活。本期已经选择独立
`unilab package download <selector>`；Issue #147 的 `install <spec>` 延期，并在未来复用同一审计结果。

当前旧 Backend 只能以 template UUID 构造公开 302，但 CLI 不应只拿这个 URL 就下载：它还必须通过包详情/resolve 获得期望的 namespace、version 和 digest。目标 API 应让 `release_uuid` 直接解析到完整 descriptor，而不是复用任意模板 UUID。

缓存索引可重建，不是 Inventory。缓存键应包含 namespace/version/artifact digest，并用进程锁 + 原子 rename 防止并发下载暴露半成品。

显式 `--extract-source DIR` 只能在缓存提交后执行。新 wheel 的 `.dist-info` 清单必须列出唯一 import
package、资产、最小 `pyproject.toml` 元数据、根 `package.yaml`（若需要）和逐成员摘要；导出器只复制
清单授权的普通文件，在临时目录内重新运行 `WorkspaceSource` parity 后原子改名。目标目录已存在、
清单缺失、成员摘要不一致或 Catalog parity 失败都返回稳定非零错误，绝不合并或覆盖用户目录。

### 7.4 失败语义和重试

- 获取 descriptor 失败：可对短暂 5xx/超时有界重试；401/403/404 不盲重试。
- 预签名 URL 过期：回到 Backend 重新 resolve，不重放已过期 URL。
- 下载中断：临时文件不进入缓存索引；续传只能在 Backend/OSS 明确支持 Range 和 ETag 时启用。
- artifact/Catalog/content digest 或 identity parity 不匹配：终止、删除/隔离临时文件，不回退旧缓存并继续激活。
- 同 namespace/version 再发布不同 digest：目标 Backend 应返回 409，除非显式支持新 release；不应静默改写旧版本。
- 模板投影失败：包发布不进入 `published`，并保留可重试/回收的 object key。

## 8. 建议测试矩阵

| 层级 | 必测场景 |
| --- | --- |
| Package Manager 单测试 | `CachedArchiveSource` 路径/符号链接/特殊文件；同一 compiler；本期 Workspace/CachedArchive Catalog parity |
| 上传单测试 | 仅 build 一次；流式 PUT；Content-Type/scene；幂等键；上传成功但 finalize 失败 |
| 下载单测试 | 302/URL 过期；Content-Length 超限；实际字节超限；SHA/Catalog/identity/FQID 不匹配；并发缓存命中 |
| 工作区导出测试 | 清单/成员摘要；老 wheel 无清单；目标已存在；路径逃逸；临时目录清理；Workspace/CachedArchive Catalog parity |
| 安全测试 | ZIP slip；symlink/hardlink/special file；duplicate member；zip bomb；伪造 `RECORD`；任意 URL/SSRF 绕过 |
| Backend 合同测试 | Lab/Bearer/API 鉴权；scene 枚举；PUT Content-Type 签名；302 fail-closed；409 版本冲突；publish/finalize |
| 集成测试 | 本地 Backend + MinIO/OSS：u4e0a传一个已审计 wheel → 广场可见 → 新工作目录下载 → Catalog parity |
| CLI 端到端 | login/session 鉴权、JSON 输出、退出码、无交互模式、秘密不出现在日志/命令行 |

历史候选 `feature/electron-device-provisioning@6a9a549` 已有 `community.py`/`CachedArchiveSource` 和 `tests/package_manager/test_device_package_download.py`，可作为行为与测试用例供体；它使用旧 Package Manager 模块布局，不应整块 cherry-pick 回当前主干。

## 9. 已决策与剩余问题

已决策：

1. 第一阶段只修改 Uni-Lab-OS，使用 `legacy-template-package/v1` capability probe；不兼容环境返回
   `backend_incompatible`，不在本期补 Backend 路由。
2. Uni-Lab-Cloud 本期不修改；Package Release v2 延期。
3. 二次开发只导出派生软件包工作区，不上传独立 `source.tar.gz`，也不新增远端源码字段。
4. 同一版本不同 Artifact 摘要失败关闭，返回 `version_conflict`，本期不允许覆盖。

剩余运行/产品问题：

1. 下载是公开还是登录后可见？当前“详情公开 + 302 公开 + resolve 需 Lab 鉴权”的可见性是否是产品决策？
2. 谁可发布权威广场？publisher、lab、namespace 和模板修改权限如何绑定？
3. 孤儿 OSS 对象的保留期和 GC 责任归谁？失败上传是否可由 CLI 取消/删除？
4. 后续 v2 是否让工作流（Workflow）定义进入云端广场，或继续只由包产物/Catalog 保留？
5. 后续是否要求发布者签名、SBOM 和代码审查？SHA-256 只证明字节一致，不证明发布者身份或代码可信。

## 10. 建议的交付分界

第一阶段已经选择 OS-only 遗留兼容路径，可由 Uni-Lab-OS 独立交付的是：

- `CachedArchiveSource` 与 Workspace/CachedArchive Catalog parity；`InstalledDistributionSource` 随后续 `install` 补齐；
- 可丢弃、有界、并发安全的 wheel cache；
- 新 wheel 的规范化工作区导出清单，以及安全、原子的 `--extract-source`；
- `PackagePublisher`/`PackageAcquirer` 传输端口和 fake Adapter 测试；
- CLI 参数、鉴权解析、JSON 输出与稳定错误码；
- 对当前旧 Backend 的显式兼容 Adapter，但不将其 DTO 泄漏到 Package Catalog/缓存域。

独立 Package Release、原子发布身份、服务端不可变约束、上传会话/幂等、模板投影关系和孤儿对象 GC
仍是遗留协议已知限制，留给后续 Backend/Cloud v2 项目；它们不进入本期 OS CLI 兼容闭环的完成门禁。
