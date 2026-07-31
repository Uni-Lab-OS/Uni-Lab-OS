# Round 02B1：Parameter Annotation 最终模块安全复审

日期：2026-07-31

评审分支：`review/02b1-module-safety-final`

基线：`ca6083badf9ac7db299b30c4f2999f1f32f6a445`

固定 production/test 候选：
`c591f94d2a84486e730ad3ccd0e26d6be2376179`

含最终决策、合同复审与趋势文档的评审快照：
`715eef583b2c3c21b509be477940bcd9acca36c0`

评审角色：最终顺序独立复审 2/3，Module safety / Standards reviewer。
Reviewer 未参与本轮 production 或测试编写；本报告只新增评审文档，不修改
production、测试、前端或 Backend，也没有启动其他 subagent。

## 1. 结论

**Blocking 数为 0；Non-blocking 数为 1。固定候选允许进入最终顺序复审 3/3。**

| 评审轴 | Blocking | Non-blocking | 结论 |
|---|---:|---:|---|
| Module / safety | 0 | 1 | 通过 |
| Repository Standards | 0 | 0 | 通过 |

M-01、M-02、M-03、S-01 以及最终风险 B01/B02 均为
`accepted-fixed`。唯一 non-blocking 仍是既有 NB-01：未来
Registry/Compiler 生产 caller 必须从真实 module AST 构造只含模块作用域、能识别
名称遮蔽的 import map。02B1 按停止线没有生产 caller，该风险当前不可达；后续接线
round 必须先将它测试化并关闭。

`c591f94..715eef5` 在 `unilabos/` 与 `tests/` 下没有差异，后续提交只更新文档，
因此本次检查的 production/test 固定点没有漂移。

## 2. 最坏复杂度与 collision safety

**Disposition：`accepted-fixed`**

Annotation 层在严格判定 scalar family 后，对临时副本排序并比较相邻项；Workflow
Schema 层先严格规范化及检查每个 enum member，再排序 `(原索引, 值)` 并比较相邻
项。两层都不依赖 Python numeric hash，最坏比较次数为 O(n log n)，额外内存为
O(n)。排序只作用于临时副本，输出仍使用原 `values` / `normalized` list。

独立对抗脚本确认 2,048 和 8,192 个相异整数具有完全相同的 Python hash，并从两层
公开 parser 测得：

| 层 | 2,048 项 | 8,192 项 | 增长 |
|---|---:|---:|---:|
| Parameter Annotation | 0.023159 s | 0.094432 s | 4.08x |
| Workflow Schema | 0.019633 s | 0.076859 s | 3.92x |

四倍碰撞输入保持约四倍增长，没有恢复 O(n²)。正式测试另以两次取最小值和
`8x + 0.02s` 宽松阈值守护同一性质；两层均通过。

严格语义和顺序没有因排序改变：

- Annotation 在排序前拒绝 boolean/integer 混族和非有限 float；
- Workflow integer/number 在排序前完成 exact-type、finite、constraint
  normalization；
- `1`/`1.0` 与 `-0.0`/`0` 继续按冻结的 number 数值等价语义判重；
- `[2, 1, 2, 1]` 稳定报告 `/parameters/0/schema/enum/2`；
- `[1, 1, 1]` 稳定报告 `/parameters/0/schema/enum/1`；
- 多个碰撞重复组仍取原声明中最早的第二次出现位置；
- 成功 descriptor 与 render 完整保留声明顺序。

排序只接收已经归一为 exact `str`、`bool`、`int` 或 finite `float` 的成员，不会调用
不可信对象自定义的 `__hash__` 或比较方法。可信 canonical 任意大整数的单次比较成本
随其实际位数增长，但比较次数仍为 O(n log n)，没有与输入规模无关的隐藏平方扫描。

## 3. 4096 位 Authoring 工作预算

**Disposition：`accepted-fixed`**

`_literal_value()` 是所有 Authoring literal 的唯一读取 seam。`ast.literal_eval()`
成功后，它使用显式工作栈检查：

- `Literal` member；
- 参数 default；
- `Field` bound；
- 嵌套 list/tuple/set member；
- dict key 与 value。

因此完整 literal 树中的 exact `int` 均受 canonical 十进制绝对值最多 4,096 位的
工作预算约束。实现用数学值比较，不依赖源码 token 长度；大型十六进制、八进制和
二进制不能绕过预算。

独立复核确认：

```text
10**4095：
  接受；parse -> render -> ast.unparse -> reparse descriptor 相同

10**4096：
  拒绝；稳定 AnnotationSchemaError 与原位置 path

嵌套 dict key / tuple / set：
  同一工作栈可到达并拒绝超预算 integer
```

正式测试还覆盖正负边界、Literal/default/Field/nested JSON 四类位置以及大型
十六进制源码。每项操作前后 `sys.get_int_max_str_digits()` 均未改变；
production 没有调用 `sys.set_int_max_str_digits()`。

该预算只存在于不可信 AST Authoring adapter。独立脚本直接通过
`parse_input_contract()` 输入 10,000 位 integer enum/default，descriptor 原值
不变；正式测试的 5,000 位用例也通过。可信 canonical Workflow integer 没有被
缩窄。

## 4. M-01、M-02 与 S-01 复核

### M-01：parser-only Authority

**Disposition：`accepted-fixed`**

`ParsedParameter` 使用 `init=False`、拒绝普通 `__new__`、模块私有 token 与
`_from_canonical()`。普通 caller 无法通过 nominal class 构造空 contract、错误
metadata 或 contract/metadata 不匹配的对象。合法 parser 结果仍支持比较、哈希、
独立 `to_dict()` 与确定性 render；Module Interface 没有因关闭伪造入口而扩大。

### M-02：异常隔离与递归安全

**Disposition：`accepted-fixed`**

`ast.literal_eval()` seam 只捕获 `RecursionError`、`TypeError` 和 `ValueError`，
将不受支持或过深 AST 稳定映射为 `AnnotationSchemaError`。2,500/3,000 层手工
AST 从公开 parser 连续调用都稳定失败，没有 Python 递归异常越界。

后续 integer 遍历使用显式工作栈，不引入新的 Python 递归。把
`ast.literal_eval()` 替换为 `RuntimeError("sentinel")` 时，异常原样透传，说明
实现没有使用宽泛 `except` 掩盖程序错误；`MemoryError` 等资源异常同样不在捕获
范围。

### S-01：类型与仓库 Standards

**Disposition：`accepted-fixed`**

`AnnotationSchemaError.__init__` 与 `AllowedResourceTemplates.__init__` 均显式
标注 `-> None`。新增/修改 production 函数的参数、返回类型完整，注释、docstring
和运行时错误使用简体中文。Ruff E/F/I 与 format 门禁均通过。

## 5. 深模块、资源预算与删除测试

外部 Interface 仍集中为：

```text
parse_parameter_annotation(...) -> ParsedParameter
render_parameter_annotation(ParsedParameter) -> ast.expr
```

caller 无需理解有限 Python 类型语法、nullable、Literal、Field、presentation、
ResourceTemplate identity、default canonicalization、稳定诊断或确定性逆向 AST。
删除该模块会迫使 Registry 与 Workflow compiler 各自重建这些规则，deletion test
成立；Implementation 规模没有等量泄漏给 caller。

Annotation 与 Workflow Schema 的两处判重分别守卫 AST adapter 和可被直接调用的
canonical Authority。删除任一处都会让一种合法入口失去唯一性检查；它们的错误
类型、路径精度与 normalization 前置条件也不同，因此不是应抽成新公共抽象的
Duplicated Code。

资源行为与输入成比例：

- literal 值遍历为 O(n) 工作栈；
- enum 判重为 O(n log n) 比较、O(n) 临时内存；
- 没有无界递归、后台任务、I/O、全局 cache 或进程级配置修改；
- 没有为规避风险新增未经决策的 enum 数量 cap；
- 可信 canonical 任意大整数继续由 Workflow Schema Authority 保存。

重新应用深模块与 Fowler baseline 后，没有发现 Mysterious Name、Feature Envy、
Message Chains、Shotgun Surgery、Speculative Generality 或需要升级为 finding 的
Repeated Switches。parser-only factory、literal 工作栈和排序 helper 都在原
Interface 后关闭已复现风险，没有形成新的公共 seam。

## 6. 测试边界与 scope

最终三组 159 个 Parameter Annotation cases 均从公开 parse/render/descriptor/
异常 Interface 观察行为。最终风险文件覆盖 collision、最早重复索引、完整保序、
4096/4097 边界、非十进制绕过、全局状态不变与可信 canonical 大整数；没有断言
具体排序容器、私有 token 值或局部变量，未过拟合某一种实现。

本候选没有接入旧 Registry scanner、Compiler、HTTP、Catalog、SQLite、FE 或
Backend，也没有改变 Draft/Candidate/Apply 与 D-117 单编辑权交互，满足 02B1
停止线。NB-01 不应在本轮通过猜测未来 caller 结构扩大 Module Interface。

## 7. 实际门禁

本 reviewer 使用：

```text
/home/changjunhan/.micromamba/envs/unilab/bin/python
```

实际结果：

```text
Parameter Annotation 三组目标：
  159 passed in 1.35s

02A Schema/route 累计：
  212 passed, 2 warnings in 2.31s

Registry：
  185 passed in 3.16s

Workflow：
  644 passed, 3 warnings in 27.32s

独立 collision/顺序/预算/异常/公开签名脚本：
  passed

Ruff E/F/I：
  All checks passed

Ruff format --check：
  6 files already formatted

git diff --check ca6083b...c591f94：
  passed

git diff --check c591f94...715eef5：
  passed
```

warnings 仅来自既有 FastAPI/TestClient、lifespan deprecated 与
`param_resolver.py` escape 提示，所有测试正常退出。主执行者已在同一固定
production/test SHA 登记正式完整测试：

```text
1215 passed, 3 skipped, 18 warnings
```

该完整结果只作为主执行者门禁证据引用，本 reviewer 没有冒充重复执行。

## 8. 顺序复审门禁

固定 production/test 候选
`c591f94d2a84486e730ad3ccd0e26d6be2376179` 的最终模块安全复审结论为：

```text
blocking:     0
non-blocking: 1（NB-01，后续 caller 接线前必须关闭）
```

允许进入最终顺序复审 3/3。若后续修改任何 production 或测试，必须固定新的候选
SHA，并使本报告失效后重新开始三名 reviewer 的顺序复核。
