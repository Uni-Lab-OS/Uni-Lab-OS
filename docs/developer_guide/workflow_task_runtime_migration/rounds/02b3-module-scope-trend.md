# Round 02B3：静态模块作用域趋势与策略报告

日期：2026-07-31

分支：`migration/02b3-module-scope`

基线：`044a740c68ab1de52da29daf31022118a3e18916`

最终 production/test 候选：`a80d471581949ac7311fd13356cc8dda09b57bf4`

状态：**目标、Registry、完整仓库与静态门禁全绿；同一唯一 reviewer 最终确认
0 blocking、0 non-blocking，允许本地合并。**

## 1. 本轮交付

本轮关闭 02B1/02B2 遗留 NB-01，新增一个 Registry 与未来 Compiler 可共同调用的
纯 AST 深模块：

- 只顺序读取真实 `ast.Module.body`；
- 区分真正 import identity、顶层 definition 与 opaque/ambiguous shadow；
- 处理 import/from/future、Assign/AnnAssign/AugAssign/NamedExpr、Delete、解构和
  if/while/for/with/try/match 可能绑定；
- 区分 module/class/function/lambda 的加载期执行边界；
- 用 `annotation_bindings` 阻止被遮蔽的 builtin/helper 回退成可信类型；
- 对 relative/wildcard import 和 forged AST 稳定失败关闭；
- 不 import 或执行作者模块、decorator、default、annotation 或其他表达式。

公共 surface 保持为一个入口、一个错误类型和一个不可变结果快照。旧
`ast_registry_scanner.py`、ImportManager、Catalog、Compiler、HTTP、持久化、FE 与
Backend 均未接线。

## 2. 分支、subagent 与门禁过程

遵守用户本轮更新后的门禁：只使用 1 个独立 test subagent 和 1 个独立 review
subagent，且始终顺序执行，没有并发修改共享分支。

| 角色 | subagent | 独立分支/工作树 | 结果 |
|---|---|---|---|
| Test author | `round02b3_test_author` | `test/02b3-module-scope` | 提交 `08f85ebd`，54 个 RED case |
| Reviewer | `round02b3_final_review` | 三个固定 SHA review 工作树 | 同一 reviewer 完成初评、两次定向确认；最终 0/0 |

独立测试提交以 `dae30d3` 保留在 round 分支历史中，没有 squash provenance。reviewer
没有编写 production 或测试，也没有启动其他 subagent。

## 3. 代码与文件增量

相对 `044a740`，本轮 production/test 的净增量为：

| 类别 | 文件数 | 新增行 | 删除行 |
|---|---:|---:|---:|
| Production | 1 | 746 | 0 |
| Tests | 2 | 859 | 0 |
| 设计与三阶段 review 文档（不含本报告） | 4 | 819 | 0 |

因此用户关注的“实现代码”口径是：**1 个 production 文件、746 行；测试为 2 个
文件、859 行。** Production 行数较大，但最终 reviewer 已做 deletion/smell 检查：
复杂度来自 Python 3.11 binding effect、执行期 class global、forged shape 和稳定错误
枚举，对外没有增加 speculative hook、第二套状态模型或 caller adapter。

## 4. TDD 与完整门禁

### 4.1 RED

独立 test author 在 production seam 不存在时得到：

```text
54 failed in 0.99s
统一首因：ModuleNotFoundError: unilabos.registry.module_scope
```

这表示一个计划新增 seam 尚不存在，不表示发现 54 个产品问题。

### 4.2 GREEN 与评审回归

首版实现让 54 项全绿。初评和第一次确认随后发现真实相邻语义，主执行者把每个
finding 先补成回归测试，再修改 production：

```text
首版目标：                 54 passed
第一次修复后目标：         69 passed
最终目标：                 76 passed
最终完整 Registry：       375 passed
最终完整 tests/：        1405 passed, 3 skipped, 18 warnings
Ruff E/F/I：               passed
Ruff format --check：      3 files already formatted
git diff --check：         passed
```

仓库根目录裸 `pytest` 曾误收集两个历史硬件脚本，在收集阶段遇到 Modbus 连接/
camera 相对 import 错误；正式仓库 gate 一直按 AGENTS 规定使用 `pytest tests/`，上述
硬件脚本不属于本轮或既定完整测试范围。

## 5. 问题趋势

测试 case 数不能当问题数。本轮独立问题变化为：

| 阶段 | 新发现的独立问题 | 本阶段关闭 | 阶段后未关闭 |
|---|---:|---:|---:|
| 继承 NB-01 | 1 个 follow-up | 0 | 1 |
| 独立 RED | 0 个产品问题 | 0 | 1 |
| 首个 reviewer | 将 NB-01 细化为 2 blocking、1 non-blocking | 0 | 3 |
| 第一次修复 | 关闭上述 3 项 | 3 | 0 |
| 第一次确认 | 2 个相邻 blocking | 0 | 2 |
| 第二次修复与最终确认 | 关闭 2 项；0/0 | 2 | 0 |

本轮累计新发现 **4 个 blocking、1 个 non-blocking**，全部关闭：

1. opaque/ambiguous shadow 丢失，使 `list = attacker` 后错误回退 builtin；
2. direct class body `global` 的模块加载期写入被忽略；
3. forged definition body 未守卫；
4. Delete target 求值 NamedExpr 与真正删除 effect 被混合；
5. nested class body 的加载期 `global` 写入被跳过。

跨轮数量是 02B1 的 7 个 blocking、02B2 的 2 个 blocking、02B3 的 4 个
blocking。因此问题发现数**不是单调下降**：02B3 比 02B2 多 2 个，但仍低于 02B1，
且全部集中在同一个静态名称解析 seam，没有扩张到 Authority、Catalog、持久化、
HTTP、运行时或 FE。更准确的趋势是：**跨组件未知项继续减少；单个深模块在正式
caller 前经历了更深入的 Python 语义收口，open backlog 从 1 降为 0。**

## 6. 策略调整

本轮暴露出“只收集一个 name set”不足以表示 Python binding effect。后续策略调整：

1. future Registry/Compiler caller 必须消费 `annotation_bindings`，不能把
   `import_identities` 直接当 parser environment，也不能重新扫描 AST；
2. caller 集成测试必须端到端调用 Parameter/Action Result parser，不能只断言 map
   entry 消失；
3. 遇到 effectful statement 时先区分 bind、unbind、target evaluation 和
   conditional ambiguity，再投影 proof；不再把所有 effect 压成一个 name set；
4. 执行边界按 module/class body、function/async/lambda body、default/decorator 分表
   测试，新增相邻 code block 时先补 deletion test；
5. 下一 round 只接一个真实 production caller vertical slice，建议从 Action 定义的
   Parameter + named result canonical contract 开始；不同时迁移整个旧 scanner、
   Catalog 发布或 Compiler；
6. 继续保持每轮 1 个 test subagent、1 个 review subagent、顺序执行，production/
   test SHA 改变后由同一 reviewer 定向复核；
7. 下一 round 仍需用户明确同意后才能创建分支。

## 7. 前端、Backend 与 Wayfinder

- 前端：**未覆盖、未修改**；
- Backend：**未覆盖、未修改**；
- FE-OS 联调：**未触发**。

前端触发条件仍未满足：production Catalog、compile、transform、generate-python 尚未
形成可合并链路。本轮只补齐这些 caller 的静态名称安全前置条件。

Wayfinder 决策本轮没有新增产品语义；它关闭的是既有 NB-01 工程前置项。当前主机
`gh` CLI 认证仍不可用，因此不能把进度评论同步到 OS delivery issue；本地报告与
Git 历史是本轮可审计记录，不冒充远端同步成功。

## 8. 合并结论

最终 reviewer 针对 `a80d471` 的结论：

```text
Standards blocking:      0
Standards non-blocking:  0
Spec blocking:           0
Spec non-blocking:       0
```

Round 02B3 允许本地合并。合并后必须在
`integration/workflow-task-runtime` 再运行目标、完整 tests、Ruff、format 和
`git diff --check`；通过后停止，不自行开启下一 round。
