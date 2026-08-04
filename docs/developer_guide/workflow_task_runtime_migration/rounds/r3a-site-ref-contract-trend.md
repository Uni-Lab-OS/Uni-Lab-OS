# R3A SiteRef typed contract 变更记录

日期：2026-08-04

## 1. 工作区与恢复点

- 工作目录：`/home/zhangshixiang/Uni-Lab-Core/Uni-Lab-OS`；
- 基线：detached `fef34d2ccee250eba8f03612dfd83cb196c2b56b`；
- 没有创建新的实现分支，没有 merge，没有 push；
- 切换前旧工作 stash：`stash@{0}`，对象
  `4bdb5896b7c37b8f2334ff0be51d2bc57753a5db`；
- 未跟踪 `unilabos_local_ui/` 保持原状。

## 2. Test provenance

唯一独立 test-author：`/root/r3a_site_ref_tests`。

独立 tests-only commit：`8dd5d06770c2f053ab8e3a691014c4c0e034d17d`，提交信息
`test(workflow): specify SiteRef typed contract`。它在当前 detached 历史中保留为
`1154c249`；按用户要求没有为此创建新分支。测试文件随后只经过 Ruff formatter，不改变断言语义。

生产实现前 RED：`11 failed, 1 passed`。失败分别证明以下能力此前不存在：

- 公共 `SiteRef` 类型；
- canonical schema 与 Handle type；
- Annotation/Action Contract/Catalog 往返；
- SiteRef/ResourceSlot 类型隔离；
- Site resolver port、strict `{uuid}`、canonicalization/freeze 与 identity substitution 防护。

## 3. 当前实现结果

- `SiteRef` 已作为独立 public typed value 加入；
- Workflow v1、Annotation、Action Catalog、Handle 与 Graph validator 已贯通；
- Task preflight 已接入显式 `SiteRefResolver`；
- SiteRef 不继承 ResourceSlot implicit pass-through/allowlist；
- legacy device runtime 在 backend 未实现前继续 fail closed；
- 领域词汇已写入 `CONTEXT.md`。

## 4. 验证结果

| 门禁 | 结果 |
|---|---:|
| 独立 R3A acceptance | `12 passed, 1 warning` |
| Annotation/Action/Catalog/I/O/Task/Authoring/DeviceAction 相关回归 | `382 passed, 1 warning` |
| 完整 `pytest -q -rs tests` | `2613 passed, 7 skipped, 68 warnings` |
| SiteRef Pydantic exact-object smoke | passed |
| changed Python Ruff E/F/I（排除 E501） | passed |
| changed production/test `compileall` | passed |
| `git diff --check` | passed |

7 个 skip 是三个需显式联网变量的进程测试、一个需显式 Phoenix executable 的 integration 测试，
以及三个只在 Windows 运行的 ReplaceFileW 合同测试。warnings 为既有 FastAPI/TestClient
deprecation、ROS 测试类收集提示与 on_event deprecation。

整文件 `ruff format --check` 仍会报告 `authoring_engine.py`、`graph_validation.py` 和
`task_input.py` 中不属于 R3A 的既有格式差异；本轮没有为消除该提示批量改写无关代码。新增 R3A
测试文件已单独执行 formatter。最终独立 review 尚未执行；结果会在本文件继续追加。
