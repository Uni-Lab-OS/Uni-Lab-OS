# Round 02B2：Action result record 最终复审

日期：2026-07-31

评审分支：`review/02b2-action-result-final`

基线：`5b7534d69522f302eaefc4a26681f0eda6eb708f`

固定 production/test 候选：
`9806d9a61699b2f17dcf4409353702f070df201a`

含最终趋势与合同文档的评审快照：
`e8bbafb5b5a930f79a3b8150136fbf642d5e93e0`

评审角色：Round 02B2 唯一独立最终 reviewer，覆盖 Module safety、Standards 与
Spec。当前仓库门禁为每轮恰好一名独立 test-author subagent 和一名独立 review
subagent；本 Reviewer 未参与 production 或测试编写，也没有启动其他 subagent。
本报告只新增中文文档，不修改 production、测试、前端或 Backend，不执行合并或
推送。

## 1. 结论

**Blocking 数为 0；Non-blocking 数为 1。固定候选允许进入 integration 合并。**

| 评审轴 | Blocking | Non-blocking | 结论 |
|---|---:|---:|---|
| D-100 / Spec | 0 | 1 | 通过 |
| Module / safety | 0 | 0 | 通过 |
| Repository Standards | 0 | 0 | 通过 |

AR-C01、AR-C02 均为 `accepted-fixed`。唯一 non-blocking 是 NB-01：未来
production caller 必须从真实 defining module AST 构造 module-scope、
shadow-aware 的 import/definition map。本轮按停止线没有 caller，该风险当前不可达；
后续接线 round 必须先测试化并关闭。

`9806d9a..e8bbafb` 在 `unilabos/` 与 `tests/` 下没有差异，候选后续只更新趋势与
合同文档，故 production/test 固定点没有漂移。

## 2. Deep Module 与 deletion test

`action_result_schema.py` 的外部 Interface 只有：

```text
parse_action_result_declaration(
    ast.expr | ast.ClassDef | None,
    imports=...
) -> ParsedActionResults
```

376 行 Implementation 集中隐藏：

- 标准 `TypedDict`、标准库 frozen dataclass、兼容 dict 三种有字段 shape；
- `-> None` 的零显式 output shape；
- class body、decorator、dict key/value 的闭合结构检查；
- 字段顺序、重复名称、稳定错误 path；
- 单字段 descriptor 与 ResourceTemplate symbols 的有序聚合。

模块不实现第二套字段类型系统。所有字段只经 `_parse_field()` 委托
`parse_result_annotation()`；scalar、list、nullable、`Literal`、`Field`、
`ResourceSlot`、allowlist、4096 位 integer 预算与 strict numeric 语义都继续由
02B1 annotation Module 和 `parse_output_contract()` 负责。

删除该 Module 会迫使 Registry/Compiler caller 分别重建三种 shape、decorator
option、字段提取、重复检查、path 重定位与 contract 聚合，复杂度只会散回多个
caller。deletion test 通过。把这些 shape 逻辑并入单字段 annotation Module 反而会
混合两个变化原因；当前 seam 保持了 locality 和小 Interface。

单字段与聚合阶段各调用 Output Contract Authority 也有不同职责：前者保证
`parse_result_annotation()` 可独立使用，后者保证完整 results contract。两处不是
可删除的 Duplicated Code。

## 3. Parser-only nominal、不可变与 symbols

`ParsedResult` 和 `ParsedActionResults` 均采用 `frozen=True`、`slots=True`、
`init=False`、拒绝普通 `__new__` 和模块私有 token factory。独立复核确认：

- 普通 constructor 无法伪造两类 nominal 值；
- 以错误 token 调用内部 factory 同样拒绝；
- `resource_templates` 不可重新赋值；
- 每次 `to_dict()` 返回独立 canonical 容器；
- 修改嵌套 schema 或向 outputs append 不影响后续 dump。

`ParsedResult.resource_templates` 是 frozen `ResourceTemplateSymbol` tuple；
`ParsedActionResults` 再用外层 tuple 按 output 顺序保存
`(name, symbols tuple)`。三种有字段声明的 contract、字段顺序、symbol 顺序与静态
identity 完全一致；`-> None` 产生空 contract 与空 tuple。对象不保存来源 shape、
class name、`slots` 或 `kw_only`，确定性成立。

## 4. 宽字段、重复名称与资源预算

class 和 compat dict 各使用按声明顺序增长的 descriptor list、template list 与
`set[str]` 名称表。name 在 membership 前已确认是 exact `str`；重复项稳定定位到
第二次声明位置，成功输出不由 set 重建。

每个字段只委托一次 annotation parser，最终 contract 只组合一次。对 n 个字段、
总 AST 大小 m，shape/duplicate 工作平均 O(n)，字段工作 O(m)，额外聚合内存 O(n)。
不存在 prefix rebuild、递归累积、后台任务或全局 cache。

新 worktree 的 256→1,024 字段独立观测：

| 指标 | 256 字段 | 1,024 字段 | 增长 |
|---|---:|---:|---:|
| 两次取最小解析时间 | 0.017559 s | 0.069207 s | 3.94x |
| parser 峰值额外内存 | 643,710 bytes | 2,621,900 bytes | 4.07x |

四倍字段保持约四倍时间和内存，1,024 个名称完整保序；宽字段复杂度与资源增长门禁
通过。

## 5. Forged shape 与异常 locality

Reviewer 在新 worktree 独立构造 25 个相邻 forged AST shape，每项连续调用两次，
全部得到稳定 `invalid_action_result`、冻结中文 message 与精确 `/return...` path：

- 四个 ClassDef container 各自为 `None`/tuple：8 项；
- base/target id 非 string、AnnAssign 缺 target/annotation：4 项；
- dict container、缺/非 string key、malformed annotation：6 项；
- decorator Call 缺 func/args/keywords、非 keyword element：4 项；
- keyword arg 为 list/dict、keyword value 缺失：3 项。

所有 membership、lookup、iteration 和 indexing 都有前置 shape guard，没有裸
`AttributeError`、`IndexError`、`TypeError` 或不可哈希错误。AR-C01/AR-C02 保持
关闭。

`_parse_field()` 的异常捕获只包围 `parse_result_annotation(...)` 调用：

```text
delegate AttributeError / IndexError / TypeError
  -> 稳定重定位到 /return/fields/0/annotation

MemoryError / RuntimeError / OverflowError
SystemExit / KeyboardInterrupt / 自定义 BaseException
  -> 原样传播同一实例

delegate 返回后 to_dict() 的内部 TypeError
  -> 原样传播
```

因此 malformed annotation AST 得到稳定输入诊断，但资源耗尽、进程控制和无关
Implementation 错误没有被吞。`_canonical_results()` 也只捕获明确的
`WorkflowSchemaError`；模块没有 `except Exception` 或裸 `except`。

错误检查顺序固定为 root/container → class base/decorator 或 dict key →
duplicate/name → field annotation → aggregate contract。设计没有冻结多重同时错误的
另一套优先级；单一错误和公开 path 均稳定。

## 6. Standards、Spec 与停止线

106 个目标 cases 继续证明：

- 三种有字段形式产生相同 ordered contract、`implicit: false` 与 symbols；
- nullable 表示字段存在但值可为 null，不产生 `default`/`required`；
- `-> None` 是零显式 outputs；
- parser 不 import、eval、exec、compile 或 runtime reflect；
- 有限类型与错误语义全部复用 02B1。

新增/修改 production 的类型标注完整，命名清楚，注释、docstring 与错误信息使用
简体中文。Fowler baseline 未发现需要升级为 finding 的 Mysterious Name、
Duplicated Code、Feature Envy、Repeated Switches、Shotgun Surgery、Divergent
Change、Speculative Generality、Message Chains 或 Middle Man。

候选没有接旧 Registry scanner、return `Name` resolver、Catalog/Handle UUID、
旧 `@action(handles=...)`、D-068 implicit outputs、`.pyi`、Compiler、HTTP、SQLite、
SSE、FE 或 Backend。NB-01 留待真实 caller round，停止线正确。

## 7. 实际门禁

本 reviewer 在新的独立 worktree 使用：

```text
/home/changjunhan/.micromamba/envs/unilab/bin/python
```

实际结果：

```text
02B2 Action result 目标：
  106 passed in 0.99s

02B1 Parameter Annotation：
  167 passed in 1.24s

Registry：
  299 passed in 3.41s

Workflow：
  644 passed, 4 warnings in 27.50s

parser-only/dump/symbol、25 forged、异常 locality、宽字段探针：
  passed

Ruff E/F/I：
  All checks passed

Ruff format --check：
  7 files already formatted

git diff --check 5b7534d...9806d9a：
  passed

git diff --check 9806d9a...e8bbafb：
  passed
```

Workflow warnings 来自既有 FastAPI/TestClient、`param_resolver.py` escape 与
lifespan deprecated 提示，所有测试正常退出。主执行者已在同一固定
production/test SHA 登记正式全量：

```text
1329 passed, 3 skipped, 19 warnings
```

该完整结果仅作为主执行者同 SHA 门禁证据引用，本 reviewer 没有冒充重复执行。

## 8. 最终合并门禁

固定 production/test 候选
`9806d9a61699b2f17dcf4409353702f070df201a` 的最终结论为：

```text
blocking:     0
non-blocking: 1（NB-01，未来 production caller 接线前必须关闭）
```

按当前每轮“一名独立 test author + 一名独立 reviewer”规则，**本轮无需再启动其他
reviewer，允许合并到 `integration/workflow-task-runtime`**。合并后仍应按 round
gate 再跑正式全量；若合并前修改任何 production 或测试，则本报告失效并必须重新
固定候选复审。
