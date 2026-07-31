# Round 02B2：Action named result record 趋势与策略报告

日期：2026-07-31

分支：`migration/02b2-action-results`

基线：`5b7534d`

当前 production/test 候选：`3c8dd02`

状态：**独立 RED、实现和正式测试全绿，等待合同、模块安全、最终风险三名
reviewer 顺序复核。**

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
| 生产代码 | 2 | 416 | 0 |
| 独立合同/安全测试 | 3 | 1348 | 0 |
| 设计与 canonical 示例 | 1 | 265 | 0 |

新增生产代码中 340 行属于 result declaration 的闭合形状 parser，76 行属于 02B1
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

## 4. 门禁结果

```text
02B2 Action result：99 passed
02B1 Annotation：167 passed
Registry：292 passed
02A Schema/route：212 passed
Workflow：644 passed
正式 tests：1322 passed, 3 skipped, 19 warnings
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
| 正式全量 | 0 个产品回归 | 0 | 0 |

canonical 示例最初省略了 `implicit: false`。独立 test author 在写测试时指出既有
`parse_output_contract` 必然物化该字段；设计文档已校正，没有修改 02A Authority
或生产行为。

与 02B1 的多轮对抗 hardening 相比，02B2 当前没有发现新的产品 blocking，问题数量
和范围都在下降。但“首版全绿”还不能等价于可合并：340 行闭合 parser 仍需要
reviewer 独立检查错误优先级、宽字段复杂度、parser-only 构造和三种声明是否真正
同构。

## 6. 策略调整

1. reviewer 必须执行 deletion test：确认三种声明的公共部分全部下沉到
   `parse_result_annotation`，没有在 340 行模块中复制类型系统。
2. 模块安全 reviewer 重点检查 class body/error path 的分支复杂度，以及宽字段表
   是否存在隐藏 O(n²)。
3. 最终风险 reviewer 重点尝试 AST 手工构造的不等长 dict、重复字段、伪造 class
   shape 和异常泄漏；发现 blocking 必须先新增独立 RED。
4. 02B1 NB-01 仍未关闭。下一轮 production caller 接线前，先实现真实 module AST、
   module-scope、shadow-aware 的 import/definition resolver，不能复用旧
   `ast.walk()` map。
5. Catalog identity、compiler、transform、generate-python 继续拆成独立 round；
   这些 production Interface 可合并后才启动前端单编辑权实现与 FE-OS 联调。

## 7. 前端与 Backend 覆盖

- 前端：**未覆盖、未修改**；
- Backend：**未覆盖、未修改**；
- 本轮只完善 OS 内部 Action result declaration Interface。

前端触发条件仍未满足，因为生产
`Catalog/compile/transform/generate-python` 尚未形成可合并链路。
