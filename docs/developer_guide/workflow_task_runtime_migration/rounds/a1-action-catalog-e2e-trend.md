# Round A1：Action Catalog 到原 FE 编辑器 E2E 趋势与验收报告

日期：2026-08-01

OS 实现分支：`migration/a1-action-catalog-e2e`

OS integration 基线：`5b33d891e12857d6d5412950ded9eab380d1f254`

FE integration 基线：`12d8d990f6f25c1740b9dd11b0fd69936f32cf3b`

最终已审查候选：

- OS：`93ec0f3b515ef00e8ee1aefe3e0e5d68706b5860`；
- FE：`7afd4119308a96d432f5c2a9b1e482f34f8d3bed`；
- SZLab 固定 fixture：`975e9b12282aeb68282022631d4ff5e30af3f0e9`。

状态：**`@action → PackageCatalog → Registry canonical record →
TemplateCatalog → HTTP → 原 Persistent Workflow 编辑器` 已形成 production E2E；唯一独立
reviewer 对后续 OS 加固候选最终确认 Standards/Spec 均无 blocking，允许 non-squash 本地
合入。此前以 `77e79f5` 为最终 OS 候选的结论已被本报告后续复审记录取代。**

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
| OS 加固首轮独立 RED | 唯一 test-author，`/root/test_a1_review_fixes` | 原始 `039e7a6`；合入 `c60121a` | 冻结 Backend-shaped HTTP、Registry identity、ROS result flow 与 rollback 缺口 |
| OS 加固首轮实现与复审 | 主代理；`/root/review_a1_action_catalog` | `734ccd8` | 关闭首批 catalog/HTTP/result/identity 问题；`CHANGES_REQUIRED`；5 blocking |
| OS 加固二轮独立 RED | 同一 test-author | 原始 `1f95aab`、`79fef87`、`1025406`；合入 `79b88c8`、`4eb884d`、`012fb72` | 冻结 authority、双 Registry snapshot、deep JSON 与真实 Candidate result flow |
| OS 加固二轮实现与复审 | 主代理；`/root/review_a1_action_catalog` | `e0173374bb3a6f83f4a728bcd8c2e1d24415f5e5` | 关闭前轮主体问题；`CHANGES_REQUIRED`；4 类 blocking |
| OS 加固三轮独立 RED | 同一 test-author | 原始 `d030d29`、`a37565e`；合入 `e565650`、`b644237` | `8 RED/22 GREEN`，并纠正 3 条旧 authority 断言；纠正后在旧候选上保持 RED |
| OS 加固最终实现 | 主代理 | `93ec0f3b515ef00e8ee1aefe3e0e5d68706b5860` | optional nil UUID、reserved metadata、Candidate binding typing、deep template default 全部关闭 |
| OS 加固最终复核 | `/root/review_a1_action_catalog` | `93ec0f3b515ef00e8ee1aefe3e0e5d68706b5860` | `ACCEPT`；0 blocking；低层 adversarial validation opt-out 为 non-blocking |

独立测试提交没有 squash；没有删除、skip 或 xfail。三条旧 ordinary binding 断言由同一
test-author 按最终 authority 合同修正，并在修正前候选上证明为 RED；其余 Finding 回归是在
初始独立测试之后追加的精确边界用例。

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

随后针对已验收 OS 候选进行独立加固复审，发现并关闭以下问题：

1. NodeTemplate 列表的 optional nil UUID 必须匹配冻结 Backend：query 返回 200/空集合，
   path identity 仍返回 400；同时补齐空 UUID、TrimSpace、int64 和稳定排序矩阵；
2. production composition 必须同时持有完成态 Device Registry 与 Resource Registry snapshot；
   缺失 resource snapshot 且无显式 resolver 时在打开数据库前 fail closed，不能从 Action
   声明反向自授权；
3. ordinary Graph PUT 对新旧 Node/Edge 均不能创建 compiler-owned `unilab` metadata；
   `input_contract`/`input_bindings`/`executor_binding` 仍只由 trusted Candidate Apply 写入；
4. Python compile、public validate、Candidate bundle 和 semantic Apply 共用 Workflow parameter
   到 target Handle 的类型兼容证明；nullable、integer→number、ResourceSlot 和 array 正向控制
   继续通过；
5. Task input、plan、Job param 与 template `goal_default → goal` 回退全部改用公共非递归 JSON
   codec clone；深度超过 Python recursion limit、但仍处于 Backend JSON budget 内的值可以
   save/read/Task round trip 且无 alias。

最终 reviewer 接受保留一个低层
`validate_input_binding_schema=False` adversarial seam：production Service、Engine、Candidate 和
Apply 调用点均显式启用严格检查；该 opt-out 仅用于构造损坏 snapshot，证明 Task preflight
仍 fail closed。后续可收紧命名或可见性，不阻塞 A1。

## 4. 最终门禁

```text
A1 OS focused：                 683 passed
OS 加固 finding/round3：        30 passed
OS 完整 tests/：                2178 passed, 4 skipped, 49 warnings
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
修改 Python 文件 py_compile：   passed
git diff --check：             passed
OS 加固独立 reviewer：         ACCEPT；0 blocking；1 low-level seam non-blocking
```

历史 browser gate 使用 Core
`12ef72cc9a72e77581dae3e9eda7b6828ddee674`、OS
`77e79f53c868a28fb7a18a64b64a3d170611e66c`、FE
`7afd4119308a96d432f5c2a9b1e482f34f8d3bed`。真实 SZLab fixture 固定为
`975e9b12282aeb68282022631d4ff5e30af3f0e9`。`77e79f5 → 93ec0f3` 只加固 OS
HTTP、authority validation 与 JSON clone，不修改 FE/SZLab；最终 OS 精确候选另行通过上述
683 focused、2178 full tests 与独立复审。

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
不改变已审查代码；OS 最终行为候选是 `93ec0f3`，后续 ledger-only 提交不得改动
`unilabos/` 或 `tests/`。本轮历史须 non-squash 合入 OS/FE 各自 integration。没有 push。
