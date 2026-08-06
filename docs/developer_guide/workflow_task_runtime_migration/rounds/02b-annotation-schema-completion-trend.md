# Round 02B：Annotation Schema production caller 趋势与策略报告

日期：2026-08-01

实现分支：`migration/02b-annotation-schema-completion`

基线：`5e49b5f78f137bf0fd391c5353bfe359afb97405`

固定 production/test 候选：`468bd30f550ec364ed93d490572f83930c872b92`

状态：**目标、累计、完整测试与质量门全绿；唯一独立 reviewer 最终 0 blocking，
允许本地合并。**

## 1. 本轮交付

02B1、02B2、02B3 已分别提供 Parameter Annotation、Action named result 和静态
module scope 三个深 Module；本轮新增唯一 production facade：

```text
真实 defining module AST + Action function AST
                     │
                     ▼
          parse_action_contract(...)
                     │
      ┌──────────────┼──────────────┐
      ▼              ▼              ▼
module scope    parameter parser   result parser
      └──────────────┼──────────────┘
                     ▼
 ordered canonical input/output contracts
 + static ResourceTemplate symbol groups
```

它完成：

- module/function 所属关系与 method receiver 识别；
- positional-only、ordinary positional、keyword-only 参数和 Python default 对齐；
- `self`/`cls`、`sample_uuids` framework-owned 参数排除；
- 全签名在排除前去重，拒绝 `ast.parse()` 可产生但 `compile()` 才会拒绝的重复参数；
- D-088 docstring title/description 与 Field precedence；
- return `Name` 只解析到同模块最终、未遮蔽的 `ClassDef`；
- 02B1/02B2/02B3 错误到稳定 Action Contract code/path；
- 深层真实 annotation 的 `RecursionError` 隔离，同时让 `MemoryError`、普通
  `RuntimeError` 和进程控制类异常继续透传；
- immutable canonical dump 与按参数/字段顺序保存的 template symbols。

本轮没有修改旧 scanner/YAML 发布，没有解析 Catalog UUID/fingerprint，没有合成
implicit ResourceSlot output，也没有实现 Compiler、HTTP、SQLite、SSE、前端或
Backend。

## 2. 独立 TDD 与评审 provenance

| 角色 | subagent | 独立提交/报告 | 结果 |
|---|---|---|---|
| Test author | `round02b_completion_test` | 原提交 `2633177c`，合入为 `6a0c420` | 35 个 RED，统一缺少 public seam |
| Test author harness fix | 同一作者 | 原提交 `09f887f`，合入为 `bdba34d` | 只收窄 monkeypatch 生命周期，不减断言 |
| Review finding tests | 同一作者 | 原提交 `e02a8bff`，合入为 `7ccce15` | 4 RED、1 control green |
| Reviewer | `round02b_completion_review` | 原报告 `7af62934`，合入为 `01146f8` | Standards 0 blocking/1 NB；Spec 2 blocking |
| Same reviewer confirm | 同一 reviewer | 原报告 `d89673c3`，合入为 `9325e8c` | 三项均 `accepted-fixed`；最终 0/0、0/0 |

初始 RED：

```text
tests/registry/test_action_contract_schema_v1.py
35 failed
统一首因：ModuleNotFoundError: unilabos.registry.action_contract_schema
```

评审 finding RED：

```text
tests/registry/test_action_contract_schema_review_regressions.py
4 failed, 1 passed
P-B01：3 个重复 framework-owned/cross-group 签名错误成功
P-B02：真实 512-member union 泄漏裸 RecursionError
```

## 3. 代码与测试规模

相对 `5e49b5f` 的 production/test 净变化：

| 类别 | 文件数 | 新增 | 删除 | 净增 |
|---|---:|---:|---:|---:|
| Production | 1 | 452 | 0 | 452 |
| Tests | 2 | 856 | 0 | 856 |
| 合计 | 3 | 1308 | 0 | 1308 |

Production 对外 surface 只有 `parse_action_contract`、`ParsedActionContract` 和
`ActionContractError`；其余 02B 组合、AST guard、default/doc/result 聚合与错误
定位都留在同一 Module 内。测试行数较多来自完整 accepted/rejected matrix、真实
AST 删除测试、无执行补丁及 reviewer finding 回归，不是复制 production 算法。

## 4. 最终门禁

固定候选 `468bd30`：

```text
目标（原合同 + finding）：             40 passed
tests/registry + tests/workflow：       1059 passed
完整 tests/：                          1445 passed, 3 skipped
本轮 3 个 Python 文件完整 Ruff：        passed
Ruff format --check：                   3 files already formatted
git diff 5e49b5f..468bd30 --check：     passed
```

3 个 skip 和 19 个 warning 均为既有可选依赖、收集命名及 FastAPI lifespan 警告。
仓库全目录 Ruff 基线仍有大量未改文件旧债；本轮没有把 02B 扩大成全仓格式迁移，
但对全部新增/修改 Python 文件运行了配置的完整 Ruff，并通过。

同一 reviewer 对最终候选另外验证：

- duplicate 参数精确错误 locality；
- parameter/result/module 三个 RecursionError seam；
- `MemoryError`、`RuntimeError`、自定义 `BaseException` 同实例透传；
- 同一深 AST 连续 5 次得到完全相同的稳定错误。

## 5. 问题趋势

| Round | Review blocking | Non-blocking | 最终未关闭 |
|---|---:|---:|---:|
| 02B1 | 7 | 1 | 1 个 NB-01 后移 |
| 02B2 | 2 | 1 | 同一 NB-01 后移 |
| 02B3 | 4 | 1 | 0 |
| 02B completion | 2 | 1 | 0 |

问题发现数不是单调下降，但本轮从 02B3 的 4 个 blocking 降为 2 个，且都集中在
同一个 facade 的输入失败关闭，没有扩张到 Catalog、Compiler、持久化或跨组件
Authority。两个 blocking 均先由独立测试作者变成 RED，再修复并由原 reviewer确认。

因此总体趋势仍是**问题面收敛**：02B1 的类型/literal/parser 问题、02B2 的 result
shape、02B3 的 module shadow 和本轮的 caller error locality 已依次关闭；当前没有
遗留 02B blocking 或 non-blocking。

## 6. 策略调整

1. 今后直接把总计划字母切片作为工程 Round，不再自动生成数字子轮次；历史编号只
   保留 provenance。
2. 每轮继续严格顺序使用 1 个独立 test subagent、主执行和 1 个独立 review
   subagent；production/test SHA 变化后由同一 reviewer 定向复核。
3. AST-facing facade 的测试除 accepted/rejected matrix 外，固定增加：真实源码深度、
   重复调用稳定性、错误类别白名单和“不得吞资源/进程控制异常”探针。
4. Round 02C 只消费本轮 canonical contract 和 template symbols，完成
   authority-scoped Catalog snapshot；不得重新解析 annotation、重新引入旧 scanner
   fallback 或提前实现 Compiler。
5. production Catalog、compile、transform、generate-python Interface 可合并后，
   触发前端单编辑权实现和 FE-OS 联调；02C 本身尚不修改前端。

## 7. 前端、Backend 与 Wayfinder

- 前端：**未覆盖、未修改**；
- Backend：**未覆盖、未修改**；
- FE-OS 联调：**尚未触发**；
- Wayfinder：本轮没有新增产品语义，只落实 D-088～D-092/D-100 和既有单编辑权
  触发条件；未把本地工程进度冒充远端 issue 同步。

## 8. 合并结论与下一步

```text
Standards blocking:      0
Standards non-blocking:  0
Spec blocking:           0
Spec non-blocking:       0
```

Round 02B 允许本地合入 `integration/workflow-task-runtime`。合并后在 integration
再次运行目标、完整 tests、Ruff、format 和 diff-check；通过后直接创建
`migration/02c-template-catalog`，进入 Round 02C，不再等待单独确认。
