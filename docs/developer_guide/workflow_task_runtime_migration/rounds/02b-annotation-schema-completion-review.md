# Round 02B：Annotation Schema production caller 独立评审

日期：2026-08-01

评审分支：`review/02b-annotation-schema-completion`

固定基点：`5e49b5f78f137bf0fd391c5353bfe359afb97405`

固定候选：`caa02bc560ab64280401ba97968ba5119426d735`

评审角色：Round 02B 唯一独立 reviewer；同一 reviewer 分别完成 Standards 与
Spec 两轴，没有创建或调用其他 subagent。本报告只新增评审文档，不修改
production、测试、既有设计/决策、前端或 Backend。

## 1. 结论

**固定候选存在 2 个 Spec blocking，当前不允许合并。** Standards 轴另有 1 个
non-blocking 文档一致性 finding。

| 评审轴 | Blocking | Non-blocking | 结论 |
|---|---:|---:|---|
| Repository Standards | 0 | 1 | 代码结构可继续复核；README 有旧门禁措辞 |
| 02B / D-088～D-092 / D-100 Spec | 2 | 0 | 不通过；必须修复并用同一 reviewer 复审 |

本报告中的 blocking 使用门禁允许的 `accepted-fixed` disposition：表示 finding
应被接受并在新候选中修复；**不是说固定候选已经修复**。候选 SHA、production 或
相关测试一旦变化，本报告的合并许可仍为无效，必须按 D-096 重跑相关测试和复审。

## 2. 固定范围与已读证据

固定 diff 非空：4 个提交、6 个文件，净增 1359 行、删除 24 行。

```text
caa02bc feat(registry): compose canonical action contracts
bdba34d test(registry): restore safety patches before report
6a0c420 test(registry): cover action contract facade
907980e docs(workflow): freeze round 02b completion seam
```

已逐行阅读 production、747 行独立测试、全部文档 diff 与 commit list。Spec 依据为：

- `rounds/02b-annotation-schema-completion-design.md`；
- D-088～D-092、D-100；
- 02B1、02B2、02B3 的 design/trend 及既有最终评审；
- `02-authoring-schema-plan.md` 的 Round 02B 完成条件和停止线。

Standards 依据为仓库 `AGENTS.md`、`CONTEXT.md`、D-096 和总计划 Round gate。工作树
上层声明的 `../AGENTS.md` 在该 worktree 路径不存在，因此没有臆造额外规则。

## 3. Standards 轴

### S-NB01：README 仍把当前门禁称为 `multi-review`

- 严重度：**non-blocking**
- Disposition：`non-blocking-follow-up`
- 位置：`workflow_task_runtime_migration/README.md:20`

本 diff 已把 D-096、具体计划和 README 后半段更新为“恰好一名独立 reviewer”，但
README 第 20 行仍写 `multi-review gate`。它不会改变 production 行为，但会使后续
agent 从迁移入口文档读到互相冲突的门禁。写本轮趋势/策略报告时应同步改成
`single-review` 或不含人数的 `independent-review gate`。

### 3.1 Deep Module 与 Fowler smell baseline

公共 surface 保持为一个 parser、一个稳定错误类型和一个不可伪造的冻结结果；caller
无需理解参数默认值对齐、doc metadata、result declaration、scope shadow 或
canonical schema 聚合。production 真实调用 02B1/02B2/02B3 的既有入口，没有复制
Literal、nullable、Field、ResourceSlot 或 result-record 规则，也没有接 Catalog、旧
Registry 发布、Compiler、HTTP、前端或 Backend。它不是 Middle Man。

`_validate_action_shape()` 与 `_parse_parameters()` 对 arguments 容器存在局部重复，
但前者在 module resolver 之前保证 action-local forged shape 的错误 locality，后者在
实际聚合点读取已经检查过的结构；当前没有出现两套类型/default 业务规则，故不升级为
Duplicated Code finding。后续若参数结构再扩展，应返回一个私有已验证快照，避免两处
同步修改。

逐项检查 Mysterious Name、Duplicated Code、Feature Envy、Data Clumps、Primitive
Obsession、Repeated Switches、Shotgun Surgery、Divergent Change、Speculative
Generality、Message Chains、Middle Man、Refused Bequest；除上述已说明的局部重复外，
没有形成新的 smell finding。错误 code/path 字符串沿用仓库既定公共合同，不按
Primitive Obsession 重复报告。Ruff/format 已强制的事项不重复计入。

## 4. Spec 轴

### P-B01：framework-owned 参数在全签名去重前被过滤

- 严重度：**blocking**
- Disposition：`accepted-fixed`（固定候选尚未修复）
- production：`unilabos/registry/action_contract_schema.py:303-317`
- 缺失测试：`tests/registry/test_action_contract_schema_v1.py`

收尾设计第 61～63 行要求 framework-owned 参数不进入合同，同时要求 forged
`ast.arguments` 失败关闭。当前实现只在参数未被 `self`/`cls`/`sample_uuids` 过滤后
才写入和检查 `seen_names`。CPython 的 `ast.parse()` 会产生重复参数 AST，重复名称在
后续 `compile()` 才被拒绝；本 Module 明确禁止通过 `compile()` 执行该校验。因此以下
两种真实 `ast.parse()` 输入被错误接受：

```python
class Device:
    def action(self, self: int) -> None: ...

class Device:
    def action(self, sample_uuids, sample_uuids) -> None: ...
```

只读 probe 分别得到含一个业务 `self` 的合同和空 input contract，而不是稳定
`ActionContractError`。普通业务参数重复已有守卫，但恰好被排除的参数绕过了它。

最小修复方向是在任何 framework-owned 过滤之前验证整个 Python signature 的名称
唯一性，并增加 positional-only、ordinary positional、keyword-only 跨组重复以及
被排除名称重复的 public-seam 回归测试；不得调用 `compile()`。

### P-B02：真实深层 annotation 从 facade 泄漏裸 `RecursionError`

- 严重度：**blocking**
- Disposition：`accepted-fixed`（固定候选尚未修复）
- production 入口：`unilabos/registry/action_contract_schema.py:407-410`
- 缺失测试：`tests/registry/test_action_contract_schema_v1.py`

收尾设计第 31～34 行规定输入是调用方 `ast.parse()` 得到的真实 module AST；第
85～94 行规定不支持/畸形 AST 必须形成稳定 `ActionContractError`，02B1/02B2 又明确
保留深 annotation 与递归异常隔离。当前 facade 在调用 `resolve_module_scope()` 时只
捕获 `ModuleScopeError`。resolver 为查找 definition-header binding effect 递归遍历
annotation，约 512 个左结合成员的普通源码即可越过 Python visitor 递归深度：

```python
def action(value: int | int | ... | int) -> None:
    ...
```

该源码约 3 KiB，`ast.parse()` 成功；256 项得到预期
`invalid_annotation /parameters/0/annotation`，512/1024/2048 项均泄漏裸
`RecursionError`。这不是 MemoryError 等应继续透传的资源耗尽异常，也不是手工构造
循环图；它来自合同明确接受的真实源码 AST，并使输入大小在很低阈值触发非稳定失败。

修复必须保持工作量随 AST 大小有界，并把这类输入稳定定位为 Action Contract
diagnostic；不能用顶层 `except Exception` 吞掉 `MemoryError`、`RuntimeError`、进程
控制异常或无关实现错误。回归测试应通过 public facade 覆盖临界深度和多次调用，而
不 mock resolver/parser 内部协作者。

## 5. 其余重点核对

| 核对项 | 结果 |
|---|---|
| Action 所属 module/class | 对正常 AST 按 identity 接受顶层 function 或顶层 class 的直接 method，外部/nested action 失败；是否发布被遮蔽 Action 属于未来 Catalog caller，本轮未越界 |
| default 对齐 | positional suffix 与 keyword-only index 对齐正确，过滤 framework 参数前已完成排程 |
| D-088 doc metadata | 只读 literal docstring；复用既有 Google-style parser；Field precedence 继续由 02B1 parser 决定 |
| result Name/shadow | 只接受同一 `scope.definitions` 最终证明的 module-scope `ClassDef`；import/assign/conditional/function shadow 均拒绝 |
| 异常路径 | 已覆盖的 parameter/result/schema 错误均重定位到 `/parameters/...` 或 `/return...`；P-B02 是尚未关闭的递归缺口 |
| 无 import/执行/反射 | production 无 `importlib`/`eval`/`exec`/`compile`/runtime reflection，也不读取文件或作者模块 |
| 返回不变性 | canonical dump 不共享容器；template groups 与 symbol 均冻结；输入 AST 不变 |
| 停止线 | 未接 Catalog UUID/fingerprint、implicit output、旧 Registry/YAML、Compiler、HTTP、SQLite、SSE、FE 或 Backend |

测试全部从 `parse_action_contract()` public seam 观察行为，不 mock 02B1/02B2/02B3
协作者；canonical、default、doc、shadow、forged container、mutation 和旧 fallback
删除意图均有效。安全测试先加载 facade 再安装 import/exec patches，不证明首次 import
阶段的全局纯度，但 production 静态检查确认不存在作者模块解析/导入路径，因此不形成
独立 finding。测试的实质缺口就是 P-B01 与 P-B02。

## 6. 门禁与只读 probes

主执行者提供的固定 SHA 门禁证据：

```text
目标：                         35 passed
tests/registry + tests/workflow：1054 passed
完整 tests/：                 1440 passed, 3 skipped
本轮两个 Python 文件 Ruff：   passed
Ruff format --check：          passed
git diff --check：             passed
```

reviewer 在同一固定候选复跑：

```text
tests/registry/test_action_contract_schema_v1.py：35 passed
两个本轮 Python 文件 Ruff：                   passed
两个本轮 Python 文件 format --check：          passed
固定 diff git diff --check：                    passed
```

额外只读 probes 复现 P-B01 两个错误成功结果，并复现 P-B02 在 512/1024/2048 项时
泄漏裸 `RecursionError`。这些 probes 没有修改仓库文件。

## 7. 合并结论

```text
Standards blocking:      0
Standards non-blocking:  1
Spec blocking:           2
Spec non-blocking:       0
```

**固定候选 `caa02bc` 不允许合并。** 主执行者应先为两个 Spec blocking 增加回归
测试并修复 production，重跑目标、累计、完整 tests、Ruff、format 和 diff check，
然后把新候选 SHA 交给本轮同一唯一 reviewer 定向复审。S-NB01 可在同一修复提交或
本轮趋势/策略报告中关闭；它单独不阻塞合并。
