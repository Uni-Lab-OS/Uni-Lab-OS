# Round 02B1：Parameter Annotation 修复后独立合同复核

日期：2026-07-31

评审分支：`review/02b1-contract-confirm`

基线：`ca6083badf9ac7db299b30c4f2999f1f32f6a445`

固定 production/test 候选：`a75e8fe113497d018cdce5c4da692a544f09667d`

独立安全测试提交：
`83549839f812c7e753f80ad079a57b307767d0bd`

评审角色：修复后顺序独立复核 1/3。旧候选
`097d0df26d4555be42bde0889153e5596d83f2dd` 的合同评审已因 production/test
发生变化而失效。本次 Reviewer 未参与实现或测试编写；本报告不修改 production、
测试、前端或 Backend，也没有启动其他 subagent。

## 1. 结论

**Blocking 数为 0；四个旧 blocking 均已关闭，固定候选可以进入顺序复核 2/3。**

| 评审轴 | Blocking | Non-blocking | 结论 |
|---|---:|---:|---|
| Spec / contract | 0 | 1 | 通过 |
| Repository Standards | 0 | 0 | 通过 |

唯一 non-blocking 仍是旧合同评审的 NB-01：未来 Registry/Compiler 接线必须提供
模块作用域且能识别名称遮蔽的 import map。本轮依照冻结停止线没有接生产 caller，
因此该风险仍不可达，不阻塞 02B1。

## 2. 修复 finding 复核

| Finding | 复核 disposition | 证据摘要 |
|---|---|---|
| M-01 `ParsedParameter` 可伪造 | `accepted-fixed` | parser-only 构造与模块 token；6 组正常构造伪造均被拒绝 |
| M-02 泄漏 `RecursionError` | `accepted-fixed` | 在 literal 读取边界稳定映射，未使用宽泛异常捕获 |
| M-03 enum 判重为 O(n²) | `accepted-fixed` | 两处均改为 set 判重、list 保序；宽输入回归通过 |
| S-01 构造器缺返回标注 | `accepted-fixed` | 两个新增 `__init__` 均显式 `-> None` |
| NB-01 import map 作用域 | `non-blocking-follow-up` | 当前仍没有生产 caller；留待 Registry/Compiler 接线 round |

### M-01：parser-only 值没有改变合法 parse/render 合同

`annotation_schema.py:49-76` 把 `ParsedParameter` 改为 `init=False`，正常公开构造
无条件抛出 `TypeError`；只有模块内 `_from_canonical(..., token=...)` 能创建值。
`annotation_schema.py:516-525` 仍先通过唯一的 `WorkflowInputContract` parser 得到
canonical contract，再使用私有 token 创建结果。

独立安全测试
`test_annotation_schema_safety_regressions.py:56-98` 覆盖空 contract、多参数
contract、metadata 与非 slot 错配、非法 symbol，以及看似完全合法的状态；六组都
证明 caller 不能通过正常构造路径伪造 parser 状态。这里不把
`object.__new__`、`object.__setattr__` 等绕过 Python 对象模型的行为误算作公共
Interface。

原有 127 个合同测试与本次只读对抗验证同时证明，解析器创建的合法值仍能
`to_dict()`、比较、哈希和确定性 render；例如
`Literal[2.5, 1, -0.0]` 的 enum 顺序与渲染文本均保持不变。因此本修复收窄了非法
构造入口，没有改变 D-082～D-092 的合法解析/渲染合同。

### M-02：递归错误稳定映射且没有宽泛吞错

`annotation_schema.py:123-127` 只在 `ast.literal_eval()` 的局部边界捕获
`RecursionError`、`TypeError` 和 `ValueError`，然后使用原有 `_fail(path)` 投影。
独立测试
`test_annotation_schema_safety_regressions.py:101-163` 分别覆盖深 default 与深
`Literal` member，并验证两次结果的 code、path、简体中文 message 完全稳定；
对应 path 分别保持 `/default` 与 `/annotation`。

额外只读 monkeypatch 验证：

```text
ast.literal_eval -> RecursionError
=> AnnotationSchemaError，path=/default

ast.literal_eval -> RuntimeError("sentinel")
=> RuntimeError("sentinel") 原样越过边界
```

实现没有 `except Exception` 或裸 `except`，不会吞掉
`WorkflowSchemaError` 以外的实现错误、资源错误或 render 错误；M-02 已按原 finding
要求关闭。

### M-03：set 判重保持严格类型、数值等价、顺序与 D-091

Annotation 路径在 `annotation_schema.py:139-168` 先使用 `type(value) is ...`
确定 scalar family、拒绝 bool/int 混族和非有限 float，再以 `seen` set 判重并把
原值追加到 `normalized` list。Workflow Schema 路径在
`workflow/schema.py:246-278` 先调用 `_normalize_scalar()` 完成严格类型与数值
规范化，再使用相同的 set/list 结构。

这两个顺序保证 Python 的 `True == 1` 不会放宽合同：

- Annotation 的 `Literal[True, 1]` 在判重前已经因混族失败；
- Workflow integer/number 在入 set 前拒绝 bool；
- Workflow integer 先把合法 integral float 规范化为 int；
- number 的 `1` 与 `1.0`、`-0.0` 与 `0` 仍按冻结数值等价语义判重；
- string、boolean、integer 和 number 的合法成员都是可哈希 scalar；
- NaN 与无穷在入 set 前被拒绝。

因此 set membership 与旧 `_enum_equal()` 的合法域等价，不会改变严格
bool/int/number duplicate 语义。输出仍来自 list，声明顺序没有变，符合 D-091 的
非空、唯一、保序要求。

独立测试
`test_annotation_schema_safety_regressions.py:165-233` 使用 1,000 与 4,000 个唯一
成员区分线性与二次增长，同时断言完整逆序输出没有被重排；重复成员仍在
`/annotation` 确定性失败。原有合同测试继续覆盖 `Literal[1, 1.0]`、
`Literal[True, 1]`、四种 scalar family 及 Workflow integer/number 的规范化。
额外只读矩阵也复核了上述重复、混族、负零和顺序边界。

### S-01：构造器返回标注完整

`annotation_schema.py:30-38` 的 `AnnotationSchemaError.__init__` 与
`annotations.py:12-19` 的 `AllowedResourceTemplates.__init__` 均已显式标注
`-> None`。独立测试
`test_annotation_schema_safety_regressions.py:236-251` 使用 public signature
守护两处标注，S-01 已关闭。

## 3. 原合同与停止线回归

新增 12 个独立安全用例由 6 个构造伪造参数、2 个深 literal 位置、1 个宽唯一
enum、1 个宽重复 enum 和 2 个构造器签名组成。测试直接针对旧实现的四项错误，
没有删除、跳过或放宽原有 127 个 D-082～D-092/D-100 合同测试。

固定候选相对旧候选的 production 变化只涉及上述 parser authority、异常边界、
enum 复杂度与类型标注修复；没有新增类型词汇，没有改变 nullable、default、
Field、ResourceTemplate symbol、canonical JSON 或确定性 render 规则。

候选也没有接 HTTP、Catalog、SQLite、Compiler、Registry 投影、前端或 Backend，
没有改变 D-100 的 result record 停止线。因此 NB-01 仍应留给未来接线 round：
caller 必须构造模块作用域且 shadow-aware 的 import map，并补嵌套 import 与名称
遮蔽集成测试；在那之前不能宣称完整模块静态名称解析已经交付。

## 4. Standards 复核

新增 production 函数与构造器类型标注完整，注释、docstring 和运行时错误信息使用
简体中文。parser authority、literal 异常隔离和 enum 去重均隐藏在原有深模块
Interface 内，没有把复杂度推给 caller，也没有复制 FE、Backend、HTTP 或持久层
职责。

Ruff、format 与两段 diff whitespace 检查全部通过，没有发现 Repository
Standards blocking。由于本轮要求顺序独立 reviewer 且禁止 subagent，本次在一个
Reviewer 内分别完成 Spec 与 Standards 两轴检查。

## 5. 门禁

使用固定解释器
`/home/changjunhan/.micromamba/envs/unilab/bin/python`：

```text
python -m pytest -q \
  tests/registry/test_annotation_schema_v1.py \
  tests/registry/test_annotation_schema_safety_regressions.py
=> 139 passed in 0.88s

python -m pytest -q \
  tests/workflow/test_route_body_budget.py \
  tests/workflow/test_json_resource_budget.py \
  tests/workflow/test_schema_codec_hardening.py \
  tests/workflow/test_value_schema_hardening.py \
  tests/workflow/test_value_schema_v1.py
=> 212 passed, 2 warnings in 2.33s

python -m pytest -q tests/registry
=> 165 passed in 2.61s

python -m ruff check --select E,F,I --ignore E501 \
  unilabos/registry/annotation_schema.py \
  unilabos/registry/annotations.py \
  unilabos/workflow/schema.py \
  tests/registry/test_annotation_schema_v1.py \
  tests/registry/test_annotation_schema_safety_regressions.py
=> All checks passed

python -m ruff format --check \
  unilabos/registry/annotation_schema.py \
  unilabos/registry/annotations.py \
  unilabos/workflow/schema.py \
  tests/registry/test_annotation_schema_v1.py \
  tests/registry/test_annotation_schema_safety_regressions.py
=> 5 files already formatted

git diff --check ca6083b...a75e8fe
=> passed

git diff --check 097d0df...a75e8fe
=> passed
```

另执行只读对抗脚本，覆盖合法 parse/render、公开构造拒绝、递归错误与非白名单
异常、annotation 与 Workflow schema 的严格 enum 矩阵，结果为
`只读对抗验证通过`。

父任务已在相同固定候选上运行完整测试：

```text
=> 1195 passed, 3 skipped, 19 warnings
```

该完整测试结果作为父任务门禁证据引用，本 Reviewer 没有重复运行。

## 6. 下一步

固定候选 `a75e8fe113497d018cdce5c4da692a544f09667d` 为 **0 blocking、
1 non-blocking**，可以进入顺序独立复核 2/3。任何 production 或测试修改都会生成
新的候选 SHA，并使本报告失效。
