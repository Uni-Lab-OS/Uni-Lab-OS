# 设备包云端传输设计与 Core #147 / SZLab 符合性复核

> 日期：2026-08-10
> 状态：候选符合性评审（Candidate Conformance Review），不是新架构批准记录
> OS：`product/durable-scheduler-kernel@aee69e03`
> SZLab：`origin/main@1016dd30d7dbc044535cc7cc12b727d725aec89f`
> Core Backend：`origin/main@d5520789`、`origin/feat/workflow@fa269e9d`
> 权威规格：[Uni-Lab-Core #147](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/147)

## 1. 结论

2026-08-10 已冻结第一阶段范围：**仅修改 Uni-Lab-OS，在 OS CLI 中提供上传和下载；不修改
uni-lab-backend、Uni-Lab-Cloud 或 Uni-Lab-SZLab。** 当前 OS 的 `package upload` 只是待硬化骨架，
当前没有 `package download`；因此“用户通过 OS CLI 上传和下载”是本期目标，不是当前完整现状。
本期使用遗留 Backend 现有接口并在 OS 侧做 capability probe；不兼容环境失败关闭，规范 Package
Release v2 延期。本期新增的 `download --extract-source DIR` 从已验证 wheel 在 OS 单侧导出
派生软件包工作区（Derived Package Workspace），不上传第二个源码 Artifact，也不修改 Backend/Cloud。

现有云端上传/下载设计的核心领域边界符合 #147，但需要五项校准后才能称为“与当前设备包
架构一致”：

1. 稳定公共发现接缝仍是 `compile_package_source(source) -> PackageCatalog`；
   `PackagePublisher` / `PackageAcquirer` 只能是软件包管理器（Package Manager）内部接口，
   不能形成第二套公共发现协议。
2. #147 已接受的公共命令是 `inspect/build/upload/install`。`download` 可以满足本次产品需求，
   但必须标记为候选“只获取到可丢弃缓存”命令；它不安装、不激活、不改 Graph。
3. 工作流（Workflow）来源不是任意递归 `.py`：根 `package.yaml` 必须显式登记源码与稳定
   `workflow_uuid`，并与源码装饰器一致。
4. 完整软件包目录（PackageCatalog）仍是发布和审计单位。遗留 Backend 可以只投影设备/资源模板，
   但规范 Package Release 必须保存完整 canonical Catalog，Cloud 展示裁剪不能反向成为包权威。
5. 派生软件包工作区不是第四种 Package Source，也不是原始 Git 仓库。它必须重新经
   `WorkspaceSource -> PackageCatalog` 编译，并与下载 wheel 的 Catalog 保持 parity。

满足以上校准后，上传、下载、缓存和 Release 是对 #147 的分发能力扩展，不会改变 #147 已接受的
包身份、发现、激活、Graph 或物料（Material）权威。

## 2. 一手证据

### 2.1 #147 已接受合同

正式规格在 OS 历史提交 `5641c746`：

- `docs/features/F006-package-catalog-workspace/requirement.md`；
- `docs/features/F006-package-catalog-workspace/interface-design.md`；
- `docs/features/F006-package-catalog-workspace/feature-list.json`。

核心约束来自 [Issue 正文](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/147) 及以下确认：

- [Package Workspace、Lab Workspace、模板与物料实例分离](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/147#issuecomment-5149754930)；
- [package-specific 能力集中到 `unilabos/package_manager/`](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/147#issuecomment-5150181143)；
- [Graph 独占实例配置与激活](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/147#issuecomment-5150557029)；
- [完整规格人工批准](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/147#issuecomment-5150783258)；
- [2026-08-07 本地最终候选](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/147#issuecomment-5209717285)。

### 2.2 最新 SZLab 示例

[Uni-Lab-SZLab@1016dd30](https://github.com/Uni-Lab-OS/Uni-Lab-SZLab/commit/1016dd30d7dbc044535cc7cc12b727d725aec89f)
体现当前示例外形：

- [根 `pyproject.toml`](https://github.com/Uni-Lab-OS/Uni-Lab-SZLab/blob/1016dd30d7dbc044535cc7cc12b727d725aec89f/pyproject.toml)
  声明 `szlab-poly-studio`，只包含 `szlab_poly_studio*`；
- [根 `package.yaml`](https://github.com/Uni-Lab-OS/Uni-Lab-SZLab/blob/1016dd30d7dbc044535cc7cc12b727d725aec89f/package.yaml)
  显式登记 18 个工作流来源和 UUID；
- [一致性合同](https://github.com/Uni-Lab-OS/Uni-Lab-SZLab/blob/1016dd30d7dbc044535cc7cc12b727d725aec89f/docs/CONFORMANCE.md)
  冻结无 `packages/`、无模型第二协议、Graph-only activation；
- [Catalog 测试](https://github.com/Uni-Lab-OS/Uni-Lab-SZLab/blob/1016dd30d7dbc044535cc7cc12b727d725aec89f/tests/test_package_catalog.py)
  当前断言 9 个设备、20 个资源、18 个工作流、98 个动作（Action）；
- [仓库卫生测试](https://github.com/Uni-Lab-OS/Uni-Lab-SZLab/blob/1016dd30d7dbc044535cc7cc12b727d725aec89f/tests/test_repository_hygiene.py)
  约束无 Profile、无 `unilabos.model_bundles`，Graph config 与 Catalog 合同一致。

本次未重跑 SZLab 测试：系统 Python 缺 `rfc8785`、`networkx` 和 pytest。上述数量是最新测试源码
中的期望值，不是本次新产生的通过记录。

## 3. 符合性矩阵

| #147 决策 | 当前设计 | 判定 | 所需校准 |
|---|---|---|---|
| 根 = distribution root = Package Workspace | 上传从 `--path` 构建 | 符合 | 用最新 SZLab 做 fixture |
| 一个 distribution / 一个 import package | 复用现有 build 校验 | 符合 | 不增加云端 `package_id` |
| A 身份 `community.<import_package>.<id>` | descriptor 保存 namespace/FQID | 符合 | Backend 不生成第二种 definition 身份 |
| 无持久 Package Inventory | 下载只进入可丢弃缓存 | 符合 | Release 是云端发布事实，不是 OS Inventory |
| 不增加第二套发现协议 | wheel 可选导出派生软件包工作区 | 符合目标 | 只生成规范工作区，再经 WorkspaceSource 编译 |
| 唯一 `PackageSource -> PackageCatalog` | 下载后使用 `CachedArchiveSource` | 符合目标 | 本期补 Cached；Installed 随后续 install 补齐 |
| 工作流来源由 `package.yaml` 授权 | 原设计未突出 | 部分符合 | 已补为硬门禁 |
| upload 复用 build | 只消费已审计 `PackageBuildArtifact` | 符合 | 继续禁止外部 URL/wheel 绕过 |
| 正式 CLI 含 `install` | 本期主写 `download` | 部分符合 | install 保留为后续；download 是候选只缓存入口 |
| Graph 独占实例配置/激活 | 下载不改 Graph、不实例化 | 符合 | 不引入 Profile/Deployment Preset |
| PackageCatalog 不拥有 Material instance | 传输不写 Material/Inventory | 符合 | 资源定义只能投影为 ResourceTemplate |
| 完整 Catalog 是审计单位 | M1 只投影设备/资源 | 遗留可兼容 | M2 Release 保存完整 canonical Catalog |
| package-specific locality | 模块均在 `package_manager/` | 符合 | 删除 `app/community_packages.py` 的重复实现 |

## 4. 本期需要修改的仓库和部分

### 4.1 Uni-Lab-OS：必须修改

1. `unilabos/package_manager/package_catalog/sources.py`
   - 本期增加 `CachedArchiveSource`；`InstalledDistributionSource` 随后续 `install` 补齐；
   - Workspace 与 CachedArchive 进入同一个 compiler，证明 canonical Catalog parity。
2. `unilabos/package_manager/package_distribution/`
   - 新增 acquisition/cache/release DTO；
   - 内容寻址、大小限制、锁、临时文件和原子发布；
   - 新 wheel 写入受 `RECORD`/Artifact 摘要保护的工作区导出清单；
   - `workspace_export.py` 从已验证 wheel 原子生成派生软件包工作区；
   - 复用 build 中的 wheel、`RECORD`、资产闭包和 clean-source 审计。
3. `package_distribution/adapters/cloud.py`
   - 修正 `scene=models`、gzip MIME、整文件内存 PUT、业务信封检查；
   - 增加 Backend capability probe、resolve/download 和遗留 v1 Adapter；v2 不在本期；
   - OSS 跳转时移除鉴权，不信任任意 `download_url`。
4. `unilabos/package_manager/cli.py`
   - 接通候选 `download`；`install` 保持 #147 后续方向，不纳入本期；
   - 增加显式 `--extract-source DIR`，目标存在、清单缺失或 parity 失败均失败关闭；
   - JSON 输出、稳定错误码、非交互鉴权；
   - 不在本期接入安装或运行时激活。
5. `unilabos/app/main.py`
   - 只在组合根注入 Backend 地址、凭据和具体 Adapter；
   - 不实现包协议。
6. `unilabos/app/community_packages.py`、`unilabos/app/web/client.py`
   - 前者迁移为薄兼容层后删除重复下载/解压；
   - 设备包传输不复用会记录 secret、固定 gzip、整包读内存的旧通用上传路径。
7. `tests/package_manager/`
   - Workspace/CachedArchive parity、安全归档、上传部分失败、302/短效 URL、并发缓存；
   - 工作区导出清单、逐成员摘要、路径逃逸、原子目录、老 wheel 和导出后 build parity；
   - 以 SZLab wheel/Catalog 做跨仓 fixture，不 import 未选设备。

稳定公共发现接缝保持 `compile_package_source(source)`；`PackagePublisher` 和
`PackageAcquirer` 是内部深接口，不加入必须长期兼容的发现 API。

### 4.2 Uni-Lab-SZLab：本期不修改

- 仅把当前 SZLab wheel/Catalog 当作只读架构示例和测试 fixture；
- 不增加云端专用 manifest、entry point、Profile 或 `package_id`；
- 同版本不同 Artifact 由 OS 返回 `version_conflict`，本期不靠修改 SZLab 版本规避；
- 正式持续发布所需的版本提升、CI/UAT 和过期文档数量更新留到 SZLab 自己的后续交付。

### 4.3 uni-lab-backend：本期不修改

遗留 `/home/sunmenglei/uni-lab-backend@test@2d94a64` 可以支持一次兼容试运行，但它把
`package_info/source_registry` 重复放在每个资源模板上，且下载路径中的 `releaseUUID` 实际是
模板 UUID。这只能标记为遗留兼容（Legacy Compatibility）。

本次尝试更新该遗留仓库的 `origin/test`，远端 `git.dp.tech` TLS 握手失败，因此不能把本地
`2d94a64` 称为最新远端合同；兼容试运行前必须对实际部署环境重新做 capability probe。

Core Backend 最新读取的 `origin/main@d5520789` 与 `origin/feat/workflow@fa269e9d` 中，migration
`000035_remove_resource_registry_metadata` 都已删除这两个模板字段，也都没有等价 Package Release
路由。因此如果 Core Backend 是实际目标环境且没有遗留等价路由，本期 OS CLI 必须返回
`backend_incompatible`，而不是在本期修改 Backend。以下能力属于后续 Package Release v2 项目：

- `package_release` 与 definition 关系持久模型；
- publisher/权威实验室授权；
- prepare/upload/finalize 或等价发布会话；
- `(publisher, normalized_name, version)` 不可变唯一约束和幂等键；
- 保存完整 canonical Catalog、三个摘要、对象键、大小、状态和 provenance；
- resolve/list/detail/download 接口，下载使用真实 release UUID 和短效签名 URL；
- orphan object、失败 staging 和 deprecated release 的 GC/保留策略；
- ResourceTemplate 只是 published release 的设备/资源展示投影，不承载包权威。

本期 G0 只探测实际部署是否兼容遗留协议。长期 canonical Backend 的选择和演进放到 v2 独立决策，
不要让两个 Backend 仓库各自演进一套新协议。

### 4.4 Uni-Lab-Cloud：本期不修改

第一阶段只要求 OS CLI 上传/下载，并继续使用遗留 Backend，Cloud 不修改；它不是 Artifact
字节传输方，只继续读取现有设备广场数据。

以下修改全部延期到规范 Package Release v2/设备广场后续项目：

- `web/src/services/square.ts`：消费 release list/detail/resolve，而不是从模板 JSON 猜包；
- `web/src/types/square.ts`：增加 release UUID、版本、状态、publisher、Artifact size 和三个摘要；
- 包列表/详情页：明确版本选择、deprecated 状态、设备/资源/工作流数量和 CLI 命令；
- 设备详情：通过 definition FQID/release 关联模板，不把模板 UUID 展示成 release UUID；
- 权限界面：只有产品要求浏览器发起发布时才增加上传入口。首版仍建议由 CLI 上传 wheel，Cloud
  不持有 AK/SK，也不直接 PUT 包产物。

### 4.5 Uni-Lab-Core：本期不修改

#147 冻结的是本地设备包仓库、发现、构建、安装和激活边界，没有冻结云端 Release HTTP、权限、
幂等或 GC。未来可以新建 Package Release ADR/Issue 并关联 #147、Backend 和 Cloud delivery；
本期只把 #147 作为 PackageCatalog 与三来源 parity 的上游约束。

## 5. 推荐交付顺序

1. OS-R0：对目标环境只读探测 `legacy-template-package/v1`；不兼容则稳定失败。
2. OS-R1：补 `CachedArchiveSource`、缓存、工作区导出清单和 Workspace/CachedArchive Catalog parity。
3. OS-R2：实现远端解析、流式下载、三摘要校验、`package download` 和 `--extract-source`。
4. OS-R3：硬化现有 `package upload` 的 scene、MIME、流式 PUT、业务码、鉴权与广场对账。
5. OS-R4：用固定协议 fixture 和已有 UAT 环境验证上传同一 wheel、清缓存下载、派生工作区再构建及完整 Catalog 对账。
6. 后续独立项目再考虑 `install`、Backend Package Release v2 和 Cloud Release DTO。

## 6. 最小产品选择

- **本期已选择**：只修改 OS，使用遗留 Backend Adapter，Cloud/SZLab/Core 不改；明确不宣称
  canonical Package Release；派生软件包工作区只从已验证 wheel 生成，不新增远端 Artifact。
- **后续而非本期**：若要消除模板 UUID、部分发布和版本竞争问题，再单独设计 Backend Package
  Release v2；Cloud 是否切换 Release DTO 随该项目决定。
