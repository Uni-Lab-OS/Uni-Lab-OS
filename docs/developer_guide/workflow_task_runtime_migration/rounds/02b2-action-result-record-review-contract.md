# Round 02B2：Action result record 合同复审

日期：2026-07-31

评审分支：`review/02b2-action-result-contract`

基线：`5b7534d69522f302eaefc4a26681f0eda6eb708f`

固定 production/test 候选：
`3c8dd0250af7253ab911079db4dae216e2b7420e`

含趋势文档的评审快照：
`f5447586b5db21e8b345c1e342acf95f8a510eaa`

评审角色：顺序独立复审 1/3，合同 / Spec reviewer。Reviewer 未参与本轮
production 或测试编写；本报告不修改 production、测试、前端或 Backend，也没有
启动其他 subagent。

## 1. 结论

**Blocking 数为 1；Non-blocking 数为 1。固定候选当前不允许进入顺序复审
2/3，也不可合并。**

| 评审轴 | Blocking | Non-blocking | 结论 |
|---|---:|---:|---|
| D-100 / 02B2 Spec | 1 | 1 | 不通过 |
| Repository Standards / scope | 0 | 0 | 通过 |

Blocking 是当前公开 AST Interface 对手工构造的非法节点形状会泄漏裸
`TypeError`/`AttributeError`，违反设计冻结的稳定错误合同。唯一 non-blocking
仍是 02B1 NB-01：未来 production caller 必须提供 module-scope、shadow-aware 的
import/definition map；本轮按停止线没有接 caller，该风险合理后移到接线 round。

`3c8dd02..f544758` 在 `unilabos/` 与 `tests/` 下没有差异，后续提交只更新趋势
文档，因此受审 production/test 固定点没有漂移。

## 2. Blocking finding

### AR-C01：非法 AST 容器/字段形状泄漏裸 Python 异常

**Disposition：`blocking-open`**

02B2 设计 §6 明确要求内部 Schema 错误必须重定位，且不得泄漏裸
`ValueError`、`TypeError`、`KeyError`、`IndexError` 或 AST 异常。设计 §7 又把
稳定诊断列为 Module Interface；99 个测试已经用手工构造的不等长 `ast.Dict`
验证不可由普通源码生成的 AST 边界，因此该合同不限于 `ast.parse()` 恰好生成的
完整节点。

当前实现有两处在验证容器/节点 shape 前直接使用属性：

- `action_result_schema.py:272-280` 对 `ast.Dict.keys/values` 调用 `len()` 和索引；
- `action_result_schema.py:144-161` 直接迭代 `ClassDef.body` 并读取
  `AnnAssign.target/simple/value`。

固定候选的最小复现：

```python
parse_action_result_declaration(
    ast.Dict(keys=None, values=[ast.Name(id="str")]),
    imports=MappingProxyType({}),
)
# TypeError: object of type 'NoneType' has no len()

parse_action_result_declaration(
    ast.ClassDef(
        name="Result",
        bases=[ast.Name(id="TypedDict")],
        keywords=[],
        body=None,
        decorator_list=[],
    ),
    imports=MappingProxyType({"TypedDict": "typing:TypedDict"}),
)
# TypeError: 'NoneType' object is not iterable

parse_action_result_declaration(
    ast.ClassDef(
        name="Result",
        bases=[ast.Name(id="TypedDict")],
        keywords=[],
        body=[ast.AnnAssign()],
        decorator_list=[],
    ),
    imports=MappingProxyType({"TypedDict": "typing:TypedDict"}),
)
# AttributeError: 'AnnAssign' object has no attribute 'target'
```

`ast.Dict(keys=[ast.Constant(value="value")], values=None)` 也泄漏同类
`TypeError`。这些输入均通过公开签名的 `ast.Dict | ast.ClassDef` 根类型检查，却没有
得到冻结的：

```text
code = invalid_action_result
message = Action 结果声明不符合 Workflow 版本 1 合同
path = /return... 或 /return/body/0
```

受影响 path 是 compat dict 根/字段 shape 的 `/return...`，以及 class body shape 的
`/return`、`/return/body/{index}`。现有 safety 测试只覆盖 `keys` 与 `values` 数量
不等但二者仍为 list 的一种 dict，未覆盖 forged class shape 或非 list AST 容器，
所以 99 个测试全绿不能关闭本项。

关闭条件应由独立测试作者先在 `3c8dd02` 冻结上述最小 seam 的稳定
code/path/message，再在 production 显式验证 AST 容器和必要字段。不能通过宽泛
捕获 `Exception` 修复，因为资源耗尽与进程控制类异常必须继续越过边界。本
Reviewer 不修改 production 或测试。

## 3. 已满足的 D-100 合同

除 AR-C01 外，未发现 D-100 行为偏差。

### 3.1 真正复用 D-082～D-091

`annotation_schema.py:582-617` 的 `parse_result_annotation()` 直接调用与 Parameter
相同的 `_parse_annotation()`，再把单字段 descriptor 交给唯一
`parse_output_contract()` Authority。`action_result_schema.py:107-129` 对每个字段
只调用该 seam，没有复制 scalar、nullable、`Literal`、`Field`、ResourceSlot、
ResourceTemplate identity、integer budget 或异常隔离规则。

18 组 accepted type cases 覆盖完整有限 scalar/object/slot/list、两种 nullable、
Literal 与 typing collection 输入；Field presentation/constraint 和模板 symbol 顺序
另有独立用例。全部 02B1 167 个测试继续通过。

### 3.2 三种声明、`-> None` 与闭合拒绝

实现只接受：

- 唯一 `typing:TypedDict` base、无 keyword/decorator、至少一个无 default 字段；
- 无 base/keyword、唯一标准库 `dataclasses:dataclass` decorator，显式
  `frozen=True`，可选且只接受 `slots=True`/`kw_only=True`；
- 非空 compat dict，唯一、非空、合法 literal string key 和受支持 annotation；
- `ast.Constant(None)` 表示 `-> None`，得到空显式 Output Contract。

99 个测试的闭合矩阵覆盖 TypedDict 的多 base、`total=False`、decorator、default、
方法、嵌套 class、`Required`/`NotRequired`/`ClassVar`；dataclass 的 mutable/dynamic
option、额外 decorator/base/keyword、field/default/method；compat dict 的空/计算/
重复 key、unpack、动态/不支持 value；未解析 Name、bare dict、Call 和缺失 return
均失败关闭。普通合法或非法 AST 的 code/path/message 两次调用保持稳定。

### 3.3 Canonical equality、顺序和业务身份

三种有字段形式得到完全相同的有序 `WorkflowOutputContract` 和同序
`(field name, ResourceTemplateSymbol tuple)`。`ParsedActionResults` 只持有 canonical
contract 与 symbol identity，不保存 TypedDict/dataclass/dict 来源标签；class name、
`slots`、`kw_only` 也不进入 canonical 数据。改变声明形式不会改变本轮交给后续
Catalog fingerprint 的业务输入。

显式 output descriptor 不写 default/required；`parse_output_contract()` 统一物化
`implicit: false`。nullable 仅表示该必然存在字段的值可为 null，不产生 optional/
missing key。`-> None` 是唯一零显式 output 形式。

`ParsedResult` 和 `ParsedActionResults` 均为 parser-only frozen value，普通构造被
拒绝；`to_dict()` dump 与内部 canonical bytes 不共享容器。

## 4. 设计示例修正

设计初稿的 canonical JSON 曾省略 Output Contract Authority 必然物化的
`implicit: false`。提交 `497c2b3` 已把两个示例 output 都修正为显式
`implicit: false`，并说明 parser 输入 descriptor 不主动写该字段、由
`parse_output_contract()` 规范化。

最终设计、实现和测试 `_expected_contract()` 三者一致。该修正没有改变 D-068：
未来隐式同名 ResourceSlot output 仍在本 parser 之后由 Registry/Catalog 投影合成。

## 5. 停止线与 NB-01

候选没有接 `ast_registry_scanner.py`、runtime import scanner、Catalog、Handle UUID、
fingerprint、旧 `@action(handles=...)`、D-068 implicit output、`.pyi`、Compiler、
HTTP、SQLite、SSE、前端或 Backend。没有解析 return `Name` 到 class，也没有把
module lookup 偷放入纯声明 parser。

因此 NB-01 仍是 non-blocking follow-up：下一 production caller round 必须先实现
真实 module AST、module-scope、shadow-aware 的 import/definition resolver；当前
不能用测试 helper 的简单 map 宣称该问题已经关闭。

## 6. Standards 与测试审计

两个 production 模块职责分明：共享 annotation 模块只新增单字段 output seam，
Action result 模块只处理三种 record shape 与合同合并。声明形式分派没有进入
canonical 值，没有发现类型系统复制、FE/Backend 职责泄漏或需要单列的 Fowler
smell。

新增代码的类型标注完整，注释、docstring 和运行时错误使用简体中文；Ruff 与
format 均通过。测试从公开 Interface 观察 canonical、symbol、错误、安全与增长，
没有 skip/xfail 或修改既有测试。但 forged AST shape 的测试缺口会让 AR-C01 的
错误实现通过，因此当前测试集尚不足以支持合并。

## 7. 实际门禁

全部使用：

```text
/home/changjunhan/.micromamba/envs/unilab/bin/python
```

结果：

```text
02B2 Action result 目标：
  99 passed in 0.97s

02B1 Parameter Annotation：
  167 passed in 1.32s

Registry：
  292 passed in 3.76s

Ruff E/F/I：
  All checks passed

Ruff format --check：
  5 files already formatted

git diff --check 5b7534d...3c8dd02：
  passed

git diff --check 3c8dd02...f544758：
  passed
```

主执行者已在同一固定 production/test SHA 运行正式全量：

```text
1322 passed, 3 skipped
```

该全量结果只作为同 SHA 门禁证据引用，本 Reviewer 没有冒充重复执行。自动门禁
全绿不能关闭 AR-C01，因为现有 99 个测试没有覆盖该 forged AST 失败面。

## 8. 顺序复审门禁

固定 production/test 候选
`3c8dd0250af7253ab911079db4dae216e2b7420e` 当前为：

```text
blocking:     1（AR-C01，非法 AST shape 泄漏裸 Python 异常）
non-blocking: 1（NB-01，未来 production caller 接线前关闭）
```

**不允许进入顺序复审 2/3，也不允许合并到
`integration/workflow-task-runtime`。**

关闭 AR-C01 后必须固定新的 production/test SHA，重跑目标与正式门禁，并由三名
reviewer 对新 SHA 重新开始顺序复审。任何 production/test 变化都会使本报告失效。
