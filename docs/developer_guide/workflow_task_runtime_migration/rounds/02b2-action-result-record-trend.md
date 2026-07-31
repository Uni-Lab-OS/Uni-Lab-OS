# Round 02B2：Action named result record 趋势与策略报告

日期：2026-07-31

分支：`migration/02b2-action-results`

基线：`5b7534d`

当前 production/test 候选：`9806d9a`

状态：**独立 test subagent、实现、正式测试与唯一独立 review
subagent 均已通过；0 blocking，允许合并。**

## 1. 本轮交付

本轮实现 D-100 已冻结的纯 AST Action named result record：

- 在 `annotation_schema.py` 增加单字段 `ParsedResult` 与
  `parse_result_annotation`，复用 02B1 的有限类型、约束、ResourceTemplate
  symbol、整数预算和异常隔离；
- 新增 `action_result_schema.py`，静态解析标准 `TypedDict`、标准库 frozen
  dataclass、兼容 dict annotation 和 `-> None`；
- 三种有字段的声明形式归一化为同一个有序 `WorkflowOutputContract`；
- canonical output 明确物化 `implicit: false`，不保存来源声明形式；
- parser 不 import、eval、exec、compile 或调用运行时 reflection。

本轮没有接旧 Registry scanner，没有解析 return `Name` 到 class，没有投影
HandleTemplate/Catalog UUID，没有合成隐式 ResourceSlot output，也没有修改 HTTP、
SQLite、前端或 Backend。

## 2. 代码与测试增量

| 类别 | 文件数 | 新增行 | 删除行 |
|---|---:|---:|---:|
| 生产代码 | 2 | 452 | 0 |
| 独立合同/安全测试 | 5 | 1526 | 0 |
| 设计与 canonical 示例 | 1 | 265 | 0 |

新增生产代码中 376 行属于 result declaration 的闭合形状 parser，76 行属于 02B1
共享 annotation 深模块的单字段 output seam。测试约为生产代码的 3.2 倍，主要用于
冻结三种接受形状和对应的拒绝矩阵，而不是重复测试业务实现。

## 3. RED → GREEN

独立 test author 在只有设计、没有 production API 的 `5153452` 上冻结 99 个
用例：

```text
99 failed
33 个首因：annotation_schema 缺少 parse_result_annotation
66 个首因：缺少 unilabos.registry.action_result_schema
```

这些失败表示两个计划新增 seam 尚不存在，不代表 99 个产品缺陷。首版实现没有修改
独立测试，直接得到：

```text
99 passed
```

既有 02B1 Annotation 167 个用例继续全绿。

首个合同 reviewer 在 `3c8dd02` 发现公共 AST seam 对 forged node container/field
泄漏裸 `TypeError` 或 `AttributeError`。独立 test author 新增 5 个用例，在旧候选
得到：

```text
5 failed
3 个首因：裸 TypeError
2 个首因：裸 AttributeError
```

最终候选显式验证 AST list container 和 required field，并只在委托给共享
annotation parser 的输入边界重定位 `AttributeError`、`IndexError`、`TypeError`。
它没有增加顶层 `except Exception`，也不捕获资源耗尽或进程控制异常：

```text
104 passed
```

合同 reviewer 对 `5327323` 重新复核时，又发现手工
`ast.keyword(arg=[])` 会在 dataclass option set membership 中泄漏不可哈希
`TypeError`。独立 test author 用 list/dict 两种不可哈希 name 冻结 2 个 RED：

```text
2 failed
统一首因：TypeError: unhashable type
```

最终候选在 membership 前要求 keyword name 是 exact `str`，两个用例转绿：

```text
106 passed
```

## 4. 门禁结果

```text
02B2 Action result：106 passed
02B1 Annotation：167 passed
Registry：299 passed
02A Schema/route：212 passed
Workflow：644 passed
正式 tests：1329 passed, 3 skipped, 19 warnings
Ruff：passed
Ruff format --check：passed
git diff --check：passed
```

warnings 来自既有 FastAPI、ROS test class、SOCKS 可选依赖与 lifespan
deprecated 提示；没有本轮新增 warning。

## 5. 问题趋势

| 阶段 | 新发现的独立问题 | 已关闭 | 尚未关闭 |
|---|---:|---:|---:|
| 设计冻结 | 0 个产品问题 | 0 | 0 |
| 独立 RED | 0 个产品问题 | 0 | 0 |
| canonical 示例核对 | 1 个文档遗漏 | 1 | 0 |
| 首版实现与目标门禁 | 0 个产品回归 | 0 | 0 |
| 首个合同评审 | 1 blocking、1 follow-up | 1 | 0 |
| forged AST 独立 RED | 0 个新增产品问题 | 0 | 0 |
| `5327323` 正式全量 | 0 个产品回归 | 0 | 0 |
| `5327323` 合同复核 | 1 blocking、1 follow-up | 1 | 0 |
| forged decorator 独立 RED | 0 个新增产品问题 | 0 | 0 |
| `9806d9a` 正式全量 | 0 个产品回归 | 0 | 0 |
| `9806d9a` 唯一最终 review | 0 blocking、1 follow-up | 0 | 0 blocking |

canonical 示例最初省略了 `implicit: false`。独立 test author 在写测试时指出既有
`parse_output_contract` 必然物化该字段；设计文档已校正，没有修改 02A Authority
或生产行为。

与 02B1 的七个 blocking 相比，02B2 当前发现并关闭 2 个 blocking，问题数量和
范围仍在下降。两个问题没有扩张业务模型，都属于公开纯 AST seam 的失败关闭边界；
第二个还是第一个 shape-validation 修复的相邻不可哈希属性遗漏。当前策略需要从
“补已知 shape”调整为“对进入 membership/lookup/iteration 的每个 AST 属性先做
exact type guard”。

唯一最终 reviewer 已针对 `9806d9a` 完成 deletion test、parser-only/
dump/symbol isolation、25 个相邻 forged shape、异常 locality 和宽表时间/
内存增长检查；结论 0 blocking。宽表从 256 增至 1024 字段时，
时间与峰值内存分别增长约 3.94 倍和 4.07 倍，未见隐藏 O(n²)。

## 6. 策略调整

1. 后续每轮按用户更新后的门禁执行：1 个独立 test subagent + 1 个独立
   review subagent；production/test SHA 变化后该 review 失效。
2. 02B1 NB-01 仍未关闭。下一轮 production caller 接线前，先实现真实 module AST、
   module-scope、shadow-aware 的 import/definition resolver，不能复用旧
   `ast.walk()` map。
3. Catalog identity、compiler、transform、generate-python 继续拆成独立 round；
   这些 production Interface 可合并后才启动前端单编辑权实现与 FE-OS 联调。
4. 本轮合并后停止；不自行开启 02B3，等待用户明确同意。

## 7. 前端与 Backend 覆盖

- 前端：**未覆盖、未修改**；
- Backend：**未覆盖、未修改**；
- 本轮只完善 OS 内部 Action result declaration Interface。

前端触发条件仍未满足，因为生产
`Catalog/compile/transform/generate-python` 尚未形成可合并链路。
