# Round 02B1：Parameter Annotation 最终独立风险评审

日期：2026-07-31

评审分支：`review/02b1-final-risk`

基线：`ca6083badf9ac7db299b30c4f2999f1f32f6a445`

固定 production/test 候选：
`a75e8fe113497d018cdce5c4da692a544f09667d`

评审角色：修复后顺序独立复核 3/3。Reviewer 未参与本轮设计、测试、实现或前两轮
复核；本报告不修改 production、测试、前端或 Backend，也没有启动其他 subagent。

## 1. 结论

**Blocking 数为 2；固定候选当前不可合并。**

| 评审轴 | Blocking | Non-blocking | 结论 |
|---|---:|---:|---|
| 最终安全 / 回归风险 | 2 | 1 | 不通过 |
| Repository Standards / scope | 0 | 0 | 通过 |

四个旧 finding 的复核结果：

| 旧 finding | 最终 disposition | 说明 |
|---|---|---|
| M-01 `ParsedParameter` 可伪造 | `accepted-fixed` | 普通公共构造路径已关闭 |
| M-02 深 literal 泄漏 `RecursionError` | `accepted-fixed` | 递归异常在 `literal_eval` seam 稳定投影 |
| M-03 enum 判重为 O(n²) | `reopened-partial` | 连续整数已线性，但可预测整数哈希碰撞仍恢复 O(n²) |
| S-01 构造器缺少 `-> None` | `accepted-fixed` | 两处返回标注均已补齐 |

新增的第二个 blocking 是 deterministic render closure：约 4 KiB 的合法十六进制
integer `Literal` 可以被解析并进入 canonical contract，但规范化 AST 无法被
`ast.unparse()` 输出，泄漏裸 `ValueError`。

旧 NB-01 仍准确为 non-blocking：当前没有 Registry/Compiler 生产 caller；未来接线
必须提供来自真实模块 AST、只含模块作用域且能识别名称遮蔽的 import map。

## 2. Blocking findings

### B-01：可预测整数哈希碰撞使两层 enum 判重恢复 O(n²)

**Disposition：`blocking-open`**

候选在以下两处使用 Python `set` 做唯一性判断：

- `unilabos/registry/annotation_schema.py:161-167`；
- `unilabos/workflow/schema.py:255-268`。

进入 set 前的严格类型和有限数值校验是正确的，但 Python integer hash 可预测。
令：

```python
modulus = sys.hash_info.modulus
values = [index * modulus for index in range(n)]
```

这些值是 D-082/D-083/D-091 允许的唯一 integer，彼此不相等、声明顺序明确，但
hash 全部碰撞。固定解释器上的独立复现为：

| 成员数 | canonical JSON 字节数 | Annotation parse | `parse_input_contract` |
|---:|---:|---:|---:|
| 4,000 | 95,607 | 0.292253 s | 0.156384 s |
| 16,000 | 395,270 | 4.220857 s | 2.450444 s |

成员扩大 4 倍，Annotation 路径增长约 14.4 倍，直接 Workflow Schema 路径增长约
15.7 倍，接近二次复杂度。395,270 字节远低于 D-101 的 8 MiB 公共请求体预算；
该输入足以阻塞单 OS event loop 数秒，并且增大到约 0.8 MiB 后风险继续平方增长。

现有安全测试
`tests/registry/test_annotation_schema_safety_regressions.py:205-219`
只使用 hash 各异的连续整数 `range(...)`。它能守护普通路径，但会让上述碰撞输入
全绿，因此没有关闭旧 M-03 的对抗性 CPU 风险。

修复不能通过未经决策的新 enum 小上限规避 D-091。两层 canonical Authority 都必须
采用不依赖可预测 numeric hash 均匀性的判重策略，同时保留：

- boolean 与 integer 严格分离；
- integer/number 的既有数值等价；
- `1`/`1.0`、`-0.0`/`0` 的重复语义；
- 声明顺序和确定性输出；
- 直接 `WorkflowInputContract` caller 与 Annotation caller 的一致行为。

独立回归测试必须包含碰撞 integer，而不能只重复连续整数墙钟测试。

### B-02：合法大十六进制 Literal 破坏 render/unparse closure

**Disposition：`blocking-open`**

设计
`02b1-parameter-annotation-design.md:186-199`
要求从 canonical descriptor 生成 enum `Literal[...]`，并明确要求
`ast.unparse(render(...))` 可以重新解析为同一 canonical descriptor。

当前 parser 在
`unilabos/registry/annotation_schema.py:139-168`
接受合法 integer Literal；renderer 在
`unilabos/registry/annotation_schema.py:532-554,624-652`
把 canonical integer 放入 `ast.Constant`。CPython unparser 会把 integer 转成十进制，
并受进程默认 4,300 位十进制转换上限约束。

以下输入完全来自正常 `ast.parse`，不是伪造 AST：

```python
source = "Literal[0x" + "f" * 4000 + "]"
annotation = ast.parse(source, mode="eval").body
parameter = parse_parameter_annotation(
    "value",
    annotation,
    default=NO_DEFAULT,
    imports=MappingProxyType({"Literal": "typing:Literal"}),
)
ast.unparse(render_parameter_annotation(parameter))
```

独立复现：

```text
source_bytes 4011
accepted_kind integer
ValueError: Exceeds the limit (4300 digits) for integer string conversion
```

这既违反确定性 render closure，也让正常作者输入在调用方泄漏非
`AnnotationSchemaError` 异常。现有 accepted-type/render 测试只覆盖小整数，没有
覆盖 Python 可以正常解析、但规范化十进制输出超过转换预算的十六进制整数。

D-101 明确不能通过修改 `sys.set_int_max_str_digits` 来改变进程全局语义。关闭本项
需要在不改变该全局设置的前提下，明确并实现 Authoring integer token 预算或修改
冻结 renderer 合同；若选择拒绝，必须是经过决策的 transport/source 预算并稳定
返回 `AnnotationSchemaError`，不能把它伪装成新的 Workflow integer 类型上限。

## 3. 已关闭风险

### 3.1 parser-only 对象 closure

`annotation_schema.py:49-76` 使用 `init=False`、拒绝普通 `__new__` 和模块 token。
独立测试
`test_annotation_schema_safety_regressions.py:56-99`
覆盖空 contract、多参数 contract、非 slot metadata、错误 symbol 和看似合法状态；
普通 `ParsedParameter(contract, symbols)` 均在进入 `to_dict()`/render 前抛
`TypeError`。合法 parser 结果仍可比较、哈希、独立 dump 和确定性 render。

显式访问模块私有 token，或使用 `object.__new__`/`object.__setattr__` 绕过 Python
对象模型，不属于普通公共 Interface。M-01 保持关闭。

### 3.2 literal 异常隔离、import/eval/exec 与 runtime helper

`annotation_schema.py:123-127` 只在 `ast.literal_eval` 局部捕获
`RecursionError`、`TypeError` 和 `ValueError`。独立 monkeypatch 复现：

```text
RecursionError => invalid_annotation /default 中文稳定错误
RuntimeError("sentinel") => 原样越过，不被宽泛吞掉
```

这关闭了旧 M-02，且没有用 `except Exception` 掩盖实现错误。新增 production
没有 `importlib`、`eval`、`exec`、`__import__`、文件、网络或进程调用。

`unilabos/registry/annotations.py:1-19` 只导入标准库 `dataclasses`/`typing`，
提供 typing alias 与冻结 metadata carrier；实测导入不会加载 Pydantic、Registry
scanner、驱动或作者模块。`AllowedResourceTemplates` runtime carrier 可以持有普通
对象，静态合法性仍由 AST parser 失败关闭，不构成第二个 Catalog Authority。

### 3.3 严格数值、default/nullable/Field/ResourceTemplate

除 B-01 的复杂度外，set 替换没有改变合法域语义：

- Annotation 在入 set 前使用 `type(value) is ...` 区分 bool/int/float；
- Workflow Schema 在入 set 前完成严格 scalar normalization；
- `Literal[True, 1]` 仍因混族失败；
- `Literal[1, 1.0]` 与 `Literal[-0.0, 0]` 仍按 number 数值等价失败；
- enum 输出仍来自 list，顺序没有依赖 set iteration。

default 只通过 `ast.literal_eval` 后交给唯一 `WorkflowInputContract` Authority；
required、optional non-null、nullable `= None`、ResourceSlot 无非空默认及
`list[ResourceSlot]` 仅 `[]` 的边界未改变。

Field 仍是闭合 keyword 集，数值/长度/presentation 按类型验证；
`AllowedResourceTemplates` 仍只接受 slot shape 上唯一 imported Name，不执行 symbol、
不接 Catalog、不伪造 UUID。未发现新的 nullable、Field 或 ResourceTemplate 安全
blocking。

### 3.4 性能测试本身

现有普通宽 enum 测试用 1,000 与 4,000 个逆序连续整数、两次取最小值，并允许
`8x + 0.02s`。本机连续运行 10 轮，增长比稳定在 `3.86x～4.04x`，均通过；
正常 CI 门槛有足够余量，不把它单独列为 flaky-test finding。

问题不是该门槛在正常 CI 不稳定，而是其数据分布漏掉 B-01 的确定性 hash collision。

## 4. Non-blocking finding

### NB-01：未来 caller 的 AST/import map 作用域与名称遮蔽

**Disposition：`non-blocking-follow-up`**

`annotation_schema.py:97-110` 将 `imports` 当作已经由 caller 证明的 identity map；
本轮 production 中除定义外没有 `parse_parameter_annotation` caller。当前候选也
没有接旧 Registry scanner、Compiler 或 HTTP，因此错误作用域信息尚不可达。

未来接线 round 必须：

1. 只接受由真实 Python module parse 得到的 AST，不把任意伪造 `ast.Name.id`
   当作可渲染名称；
2. import map 只收集合法模块作用域 import；
3. 识别 `Assign`、`AnnAssign`、函数/class 等对 builtin/helper/import 的名称遮蔽；
4. 加入嵌套 import 不证明 identity、遮蔽后失败关闭及 render closure 集成测试。

因此旧 NB-01 仍准确为当前 02B1 的 non-blocking，但在生产 caller 接线前必须关闭；
当前纯 parser 测试不能被表述为完整模块静态名称解析已经交付。

## 5. Scope 与文档状态

候选 diff 只涉及：

- Registry Annotation 深模块与 runtime annotation helper；
- 为 enum 判重修改的 canonical Workflow Schema；
- 独立 Registry 测试；
- 本轮设计、趋势和前序评审文档。

没有修改 Backend、前端、HTTP route、Catalog、SQLite、旧 Registry scanner 或完整
Workflow compiler。D-100 的 Action result record、D-117 单编辑权及后续
FE-OS 联调均未被提前实现，scope 正确。

`02b1-parameter-annotation-trend.md:9-11,33-39,71-102` 仍登记旧候选
`64f3fc3`、旧文件/行数、旧测试数量和“无 blocking”趋势。它是评审尚未结束时的
阶段报告，不增加 production/test finding；但在修复 B-01/B-02、重新完成顺序复核
后，必须更新为最终候选、四个已关闭旧 finding与本次风险的真实趋势，才能宣称
Round 02B1 完成。

## 6. 实际门禁

全部使用：

```text
/home/changjunhan/.micromamba/envs/unilab/bin/python
```

结果：

```text
Parameter Annotation 目标：
  139 passed in 0.91s

02A Schema/route 累计：
  212 passed, 2 warnings in 2.34s

Registry：
  165 passed in 2.61s

Workflow：
  644 passed, 3 warnings in 27.79s

正式 python -m pytest tests -q：
  1195 passed, 3 skipped, 19 warnings in 68.01s

Ruff E/F/I：
  All checks passed

Ruff format --check：
  5 files already formatted

git diff --check ca6083b...a75e8fe：
  passed

git diff --check 097d0df...a75e8fe：
  passed
```

warnings 均为既有 FastAPI/TestClient、ROS test class、SOCKS 可选依赖与 lifespan
deprecated 提示；没有本轮新增 warning。所有 pytest 进程均正常退出，无挂起。

自动门禁全绿不能关闭 B-01/B-02，因为现有测试没有覆盖碰撞 integer 与大十六进制
Literal 的规范化输出。

## 7. 合并门禁

固定 production/test 候选
`a75e8fe113497d018cdce5c4da692a544f09667d`
当前为 **2 blocking、1 non-blocking**，不允许合并到
`integration/workflow-task-runtime`。

关闭条件：

1. 独立测试作者先补可预测 integer hash collision 的两层回归；
2. 独立测试作者补合法大十六进制 Literal 的 render/unparse closure 回归；
3. 明确 B-02 的 source budget 或 renderer 合同，不修改全局
   `sys.set_int_max_str_digits`；
4. production 同时关闭两项后固定新 production/test SHA；
5. 在新 SHA 重跑 139+、02A、Registry、Workflow、正式全量、Ruff、format 和 diff；
6. 三名独立 reviewer 对新 SHA 依次确认，任何 production/test 变化使旧确认失效；
7. 更新中文趋势报告中的候选、增量、问题趋势和策略。

以上修复不要求接 Registry/Compiler caller，不要求修改前端或 Backend，也不应扩大
到 Action result record、Catalog 或 HTTP Interface。
