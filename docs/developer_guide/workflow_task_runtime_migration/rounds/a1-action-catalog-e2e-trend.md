# Round A1：Action Catalog 到原 FE 编辑器 E2E 趋势与验收报告

日期：2026-08-01

OS 实现分支：`migration/a1-action-catalog-e2e`

OS integration 基线：`5b33d891e12857d6d5412950ded9eab380d1f254`

FE integration 基线：`12d8d990f6f25c1740b9dd11b0fd69936f32cf3b`

最终已审查候选：

- OS：`77e79f53c868a28fb7a18a64b64a3d170611e66c`；
- FE：`7afd4119308a96d432f5c2a9b1e482f34f8d3bed`；
- SZLab 固定 fixture：`975e9b12282aeb68282022631d4ff5e30af3f0e9`。

状态：**`@action → PackageCatalog → Registry canonical record →
TemplateCatalog → HTTP → 原 Persistent Workflow 编辑器` 已形成 production E2E；唯一独立
reviewer 最终确认 Standards/Spec 均无 blocking，允许 non-squash 本地合入。**

## 1. 本轮交付

- PackageCatalog workspace、wheel、cache 与 legacy AST scanner 都只调用
  `parse_action_contract()`，并保存同一 canonical Action schema；
- Registry 只验证和投影 canonical record，不重新解析 annotation 字符串；legacy
  `goal_default` 与 `handles` 只是兼容 assertion，重复、缺失、额外或冲突 Handle 都会
  fail closed；
- Registry 完整构建后由单一 adapter 选择显式 typed `@action`，投影稳定 NodeTemplate、
  target/source Handle、ResourceSlot 隐式输出和 editor metadata，再以一次
  `TemplateCatalog.replace()` 原子发布；失败保留上一完整 snapshot；
- production startup 在 Authoring/HTTP ready 前完成 Registry snapshot 发布；Compiler、
  Apply snapshot guard 与四个 Backend-shaped GET 都读取同一持久 Catalog；
- 原 `PersistentWorkflowAuthoringPanel` 按真实 NodeTemplate/Handle UUID 新建节点、连接 Edge、
  渲染 typed 字段并显示 OS 字段诊断；409 后刷新 Catalog 并 rehydrate dirty graph；
- FE provider 身份使用 `(target_node_uuid, target_handle_uuid)`，同模板的多个 Node 实例可
  分别连接；required 诊断承认 literal、inbound Edge 和 Workflow input binding；
- 真实 SZLab 测试从固定提交建立临时 detached clone，不读取用户 dirty worktree，也不把
  个人绝对路径或 Local Bridge 写进测试。

本轮不引入 Profile；设备连接参数继续由 graph 承载。R6 已取消，未实现外部包的选择性
单设备导入。

## 2. RED、实现与审查 provenance

| 阶段 | 角色 | 提交 | 结果 |
|---|---|---|---|
| 冻结 OS spec | 人工评审后主代理 | `ac1e65e`、`102b2e7` | A1 唯一 parser seam、原子 publisher、HTTP/FE 边界获准实现 |
| 独立 OS RED | 唯一 test-author，`test/a1-action-catalog-e2e` | 原始 `1d55fe4`；合入 `30cf136` | 新增 PackageCatalog/Registry/TemplateCatalog/HTTP 合同，production 缺口保持 RED |
| 独立 FE RED/E2E | 同一 A1 test-author | `6ce5716` | 新增 service/editor 合同与真实 SZLab browser E2E |
| 首个 production 候选 | 主代理 | OS `741c6e3`；FE `fcd452a` | 打通初始 canonical catalog 与 typed editor |
| 初审 finding 修复 | 主代理 | OS `5e62076`；FE `35f11f4` | 收紧 parser、production composition、atomic adapter、真实 UUID/表单/409/E2E |
| 精确候选复审 | `/root/a1_action_catalog_reviewer` | OS `75b4ab0`；FE `6826f66` | Standards 0B；Spec 2B；保留 S-NB01 |
| finding RED 与修复 | 主代理 | OS `77e79f5`；FE `7afd411` | duplicate legacy Handle 与 FE multi-instance/provider diagnostic 先 RED 后转绿 |
| 同一 reviewer 最终复核 | `/root/a1_action_catalog_reviewer` | OS `77e79f53c868a28fb7a18a64b64a3d170611e66c`；FE `7afd4119308a96d432f5c2a9b1e482f34f8d3bed` | 0 blocking；允许 non-squash 本地合入 |

独立测试提交没有 squash；没有删除、skip、xfail 或放宽独立测试。Finding 回归是在初始
独立测试之后追加的精确边界用例。

## 3. Finding 收敛

初审的 Standards finding：

- S-B01：strict parser 曾接受 DeviceSlot、bare dict 与 `dict[str, Any]` 参数；已收回到
  冻结合同；
- S-B02：E2E 曾依赖本地路径与 dirty SZLab；已改成环境注入路径和固定 SHA 临时 clone；
- S-NB01：旧 PackageCatalog 直接投影 helper 仍公开，production 不经过它，保留为后续
  删除、内部化或改接 canonical adapter 的 non-blocking 债务；
- S-NB02：新增 OS 注释/docstring 已按仓库语言约定收口。

初审的 Spec finding P-B01～P-B07 均关闭：production Registry publisher、legacy diagnostic、
malformed/ROS fail-closed、真实 Handle UUID、409 rehydrate、ResourceSlot/严格表单/字段诊断、
真实 SZLab 操作链都已补齐。

首次精确候选复审又发现两个 blocker：

1. legacy Handle 用字典归并会吞掉重复等价或“先冲突、后等价”的声明；修复后在写入字典
   前拒绝重复 `(io_type, key)`，PackageCatalog 与 legacy Registry projection 都有回归；
2. FE 只按 HandleTemplate UUID 判断 provider，错误阻止同模板第二个 Node 连接；修复后使用
   Node/Handle 组合身份，并让 required 诊断识别 inbound Edge 与 input binding。

同一 reviewer 在最终精确 SHA 上确认两项 blocker 与伴随诊断问题全部关闭。

## 4. 最终门禁

```text
A1 OS focused：                 198 passed
OS 完整 tests/：                2139 passed, 4 skipped, 43 warnings
Workflow editor：              68 passed
FE 全仓单测：                   passed
FE 全仓 typecheck：            passed
FE Web build：                 passed
FE Desktop build：             passed
真实 SZLab A1 Playwright：     1 passed
SZLab catalog/workspace：      6 passed
精确 SHA workflow debug E2E：  8 passed
修改文件 Ruff E/F/I：          passed
Ruff format --check：          passed
git diff --check：             passed
独立 reviewer：                Standards 0B；Spec 0B；S-NB01 retained
```

精确 SHA browser gate 使用 Core
`12ef72cc9a72e77581dae3e9eda7b6828ddee674`、OS
`77e79f53c868a28fb7a18a64b64a3d170611e66c`、FE
`7afd4119308a96d432f5c2a9b1e482f34f8d3bed`。真实 SZLab fixture 固定为
`975e9b12282aeb68282022631d4ff5e30af3f0e9`。

仓库正式完整门是 `pytest tests`，已执行并通过。裸仓根 `pytest` 还会额外收集既有硬件/
示例脚本并尝试 Modbus、Camera 等外部依赖，不属于本轮新增失败。

## 5. 原子性与 E2E 证据

后端合同测试覆盖 workspace/wheel/cache schema 等价、显式 typed-only、signature 默认语义、
Literal/Field/ResourceSlot、TypedDict/dataclass/inline dict/`None`、legacy assertion、稳定 UUID/
fingerprint、非法发布 rollback、Compiler/HTTP 同 snapshot 与旧 Candidate 409。

真实浏览器 E2E 从 SZLab 固定提交启动 production PackageCatalog、Registry 和 composition，
在原编辑器新增节点、拖接真实 Handle、编辑 required 字段、保存，并验证 HTTP UUID；随后改变
Catalog fingerprint，确认旧 Candidate 409、刷新 rehydrate、保存、Apply、刷新以及
Python/JSON/DAG round trip 不漂移。

## 6. 交付状态

production/test 候选已经完整门禁和同一独立 reviewer 最终确认。趋势报告只记录审查事实，
不改变已审查代码；本轮历史须 non-squash 合入 OS/FE 各自 integration。没有 push。
