# Round 02B：Annotation Schema production caller 最终确认

日期：2026-08-01

评审分支：`review/02b-annotation-schema-completion-confirm`

固定基点：`5e49b5f78f137bf0fd391c5353bfe359afb97405`

原评审候选：`caa02bc560ab64280401ba97968ba5119426d735`

最终候选：`468bd30f550ec364ed93d490572f83930c872b92`

评审角色：Round 02B 同一且唯一独立 reviewer。没有创建或调用其他 subagent。本
报告只新增中文确认文档，不修改 production、测试、既有评审、前端或 Backend。

## 1. 结论

**原评审的 P-B01、P-B02 与 S-NB01 全部关闭；最终候选 0 blocking、0
non-blocking，允许本地合并。**

| 评审轴 | Blocking | Non-blocking | 结论 |
|---|---:|---:|---|
| Repository Standards | 0 | 0 | 通过 |
| 02B / D-088～D-092 / D-100 Spec | 0 | 0 | 通过 |

本确认精确固定在 `468bd30`。任何 production、相关测试、rebase 或候选 SHA 变化都
使本确认失效，必须按 D-096 重跑门禁并由同一 reviewer 复核。

## 2. 定向修复范围

相对原候选 `caa02bc`，新候选增加 3 个提交：

```text
468bd30 fix(registry): close action contract review findings
7ccce15 test(registry): cover action contract review findings
01146f8 docs(workflow): review round 02b completion
```

修复 diff 只包含：

- `action_contract_schema.py`：全签名名称预检和三个窄 `RecursionError` seam；
- `test_action_contract_schema_review_regressions.py`：5 个 public-seam 回归；
- `README.md`：旧 `multi-review` 措辞修正；
- 原独立评审报告的 provenance。

没有接入 Catalog UUID/fingerprint、旧 Registry/YAML 发布、implicit ResourceSlot
output、Compiler、HTTP、SQLite、SSE、前端或 Backend。

## 3. Standards 轴

**Standards blocking 0，non-blocking 0。**

### S-NB01：README 门禁措辞——`accepted-fixed`

README 已把 `test-author/full-suite/multi-review gate` 改为
`test-author/full-suite/independent-review gate`，与 D-096 和具体计划的“一名独立
reviewer、双轴覆盖”一致。原 non-blocking 已关闭。

### 3.1 Module depth 与 smell 复核

公共 Interface 没有增加参数、类型、hook 或 adapter；仍只有既定 parser、错误和冻结
结果。修复只在 facade 内关闭两个已复现输入边界，没有把调用复杂度推给 02C/02D。

全签名预检与聚合阶段仍各有一次 name guard：前者守卫 Python signature，包括会被
合同过滤的 framework 参数；后者守卫过滤后的 canonical descriptor。最终调用路径上
后者在正常情况下是防御性重复，但两层分别保护 raw AST 与 canonical 聚合边界，没有
复制 annotation/default 业务规则；本次不形成 Duplicated Code 或 Speculative
Generality finding。

重新逐项检查 Mysterious Name、Duplicated Code、Feature Envy、Data Clumps、
Primitive Obsession、Repeated Switches、Shotgun Surgery、Divergent Change、
Speculative Generality、Message Chains、Middle Man、Refused Bequest；没有新增
finding。错误 code/path 继续使用仓库已冻结的 primitive wire contract；Ruff/format
已覆盖事项不重复报告。

## 4. Spec 轴

**Spec blocking 0，non-blocking 0。**

### P-B01：framework-owned 参数绕过去重——`accepted-fixed`

`_validate_action_shape()` 现在在任何 receiver/framework-owned 过滤前，按
positional-only、ordinary positional、keyword-only 的源码顺序检查 exact non-empty
`str` name 与全签名唯一性。只读 probes 得到：

| 重复形态 | 稳定结果 |
|---|---|
| positional-only receiver + ordinary `self` | `invalid_action_contract /parameters/1/name` |
| positional-only + ordinary `sample_uuids` | `invalid_action_contract /parameters/2/name` |
| ordinary + keyword-only `sample_uuids` | `invalid_action_contract /parameters/2/name` |
| positional-only + keyword-only business name | `invalid_action_contract /parameters/2/name` |

错误在 module resolver、doc parser 和合同过滤前发生；source index 保持稳定，不把被
过滤参数伪装成 canonical contract index。没有调用 `compile()`。独立回归覆盖四种
跨组/被排除名称，原 P-B01 完整关闭。

### P-B02：深 annotation 泄漏递归异常——`accepted-fixed`

facade 只在三个输入递归 seam 捕获 `RecursionError`：

| seam | 稳定结果 |
|---|---|
| `resolve_module_scope` | `invalid_action_contract /module` |
| `parse_parameter_annotation` | `invalid_annotation /parameters/{index}/annotation` |
| `parse_action_result_declaration` | `invalid_action_result /return` |

512 项真实左结合 annotation 经 `ast.parse()` 后连续调用 3 次的
`code/path/message` 完全相同；reviewer 扩展到连续 5 次仍只有一个结果，输入 AST 可
复用。这里没有新增任意 AST 数量上限或把异常改成合法 schema。

reviewer 分别替换上述三个 seam，确认每处行为均为：

```text
RecursionError        -> 稳定 ActionContractError
MemoryError           -> 原样传播同一实例
RuntimeError          -> 原样传播同一实例
自定义 BaseException -> 原样传播同一实例
```

实现没有 `except Exception`/裸捕获，没有吞掉资源耗尽、无关实现错误或进程控制类
异常。原 P-B02 完整关闭。

## 5. 回归测试质量与相邻边界

新增测试只调用 `parse_action_contract()` public seam，不 mock resolver、Parameter
parser 或 Result parser；删除修复会分别恢复非法成功或裸 `RecursionError`，不是对
内部实现写法的断言。重复参数矩阵使用 `ast.parse()` 能产生但 `compile()` 会拒绝的
真实签名，直接冻结 facade 必须承担的 AST-only validation。深度测试既检查稳定错误，
也检查同一 AST 重复调用不漂移。

深度测试没有把精确 `code/path` 固定为某一种内部递归发生点，只要求 public error
非空且重复稳定；这是合理的非过拟合边界，因为 resolver 与 Parameter parser 都可能
先识别该不支持表达式，而 Spec 冻结的是稳定 Action Contract diagnostic，不是某个
Python 递归栈位置。Reviewer 已单独确认当前三个 seam 的精确错误 locality 和异常
隔离，不存在相邻错误。

原 35 个 public-seam 测试继续通过，说明 canonical contract、default 对齐、D-088
doc metadata、result Name/shadow、返回不变性、无 import/执行/反射和旧 fallback 删除
测试均未回归。最终 production/tests diff 仍真正组合 02B1/02B2/02B3，没有复制其
类型规则，也没有冒充 Catalog 或 Registry 发布。

## 6. 门禁证据

主执行者提供的最终候选证据：

```text
目标：                         40 passed
tests/registry + tests/workflow：1059 passed
完整 tests/：                 1445 passed, 3 skipped
本轮三个 Python 文件 Ruff：   passed
Ruff format --check：          passed
git diff --check：             passed
```

reviewer 在同一固定候选复跑：

```text
两个 Action Contract 目标文件：40 passed
三个本轮 Python 文件 Ruff：   passed
三个本轮 Python 文件 format： passed
固定完整 diff check：          passed
```

另完成上述 duplicate locality、三个异常 seam、非目标异常 identity 与五次重复调用
只读 probes；没有修改仓库文件。

## 7. 最终 disposition 与合并许可

| 原 finding | 最终 disposition | 证据 |
|---|---|---|
| S-NB01 README `multi-review` | `accepted-fixed` | 改为 `independent-review`，与 D-096 一致 |
| P-B01 framework-owned duplicate bypass | `accepted-fixed` | raw signature 全量预检 + 4 个 public-seam 回归 |
| P-B02 bare `RecursionError` | `accepted-fixed` | 3 个窄捕获点 + 真实深 AST 重复回归 + 异常隔离 probes |

```text
Standards blocking:      0
Standards non-blocking:  0
Spec blocking:           0
Spec non-blocking:       0
```

**最终候选 `468bd30f550ec364ed93d490572f83930c872b92` 允许本地合并到
`integration/workflow-task-runtime`。** 合并后仍须按 Round gate 在 integration 上
重跑目标、累计、完整 tests、Ruff、format 与 diff check，并完成本轮中文趋势/策略
报告；未经明确授权不得 push。
