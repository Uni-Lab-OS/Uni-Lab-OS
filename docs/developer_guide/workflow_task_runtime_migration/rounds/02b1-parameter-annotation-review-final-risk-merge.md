# Round 02B1 Parameter Annotation 最终合并风险复审

## 1. 固定输入与复审边界

本次为 Round 02B1 合并前第 3/3 轮独立复审，固定输入如下：

- production/test 快照：`4469953f1c5d47405b0e46adf7af07f4c971f1f6`
- migration 候选 HEAD：`163b0443473e91ea3cb7f46e7a1a1e6da62ec9c2`
- 对比基线：`ca6083badf9ac7db299b30c4f2999f1f32f6a445`
- 独立分支：`review/02b1-final-risk-merge`
- 独立 worktree：
  `/home/gaojing/.worktrees/uni-lab-os-02b1-final-risk-merge`

本轮只复核已经冻结的最终风险面：

1. `ast.literal_eval()` 的输入级异常是否全部稳定收敛；
2. 四个 literal 入口的正、负 complex overflow 是否冻结稳定
   `code/path/message`；
3. Authoring 4096 位整数预算是否存在非十进制或嵌套容器绕过；
4. B01 collision 修复是否保持严格 number 语义和声明顺序；
5. parser-only、安全边界、可信 runtime 整数和 `sys` 全局设置是否不变；
6. NB01 是否仍被正确留在未来 caller 接线前关闭。

停止线没有扩展到未来 caller、FE、Backend、HTTP、Catalog、SQLite 或 compiler。
本轮没有修改 production、test、FE 或 Backend。

## 2. 结论

**Blocking：0。**

**Non-blocking：1，沿用 NB01。**

`4469953` 已以最小且正确的方式关闭此前发现的
`ast.literal_eval()` `OverflowError` 泄漏；本轮没有发现同一 seam 上新的输入级
异常泄漏或整数预算绕过。

**最终意见：允许合并 Round 02B1。**

NB01 不阻塞本轮合并，但必须在未来首次接入 production caller 前关闭，不能把
parser-only 结果直接当作已经完成 Registry symbol 解析的运行时合同。

## 3. OverflowError 修复复核

### 3.1 修复形状

`unilabos/registry/annotation_schema.py::_literal_value()` 将既有窄捕获：

```python
(RecursionError, TypeError, ValueError)
```

扩为：

```python
(OverflowError, RecursionError, TypeError, ValueError)
```

这是对 `ast.literal_eval()` 可由不可信 AST literal 输入触发的精确异常补齐。
异常继续统一投影为：

- `code == "invalid_annotation"`
- 对应入口的稳定 JSON Pointer path
- 非空简体中文 message
- `str(error) == error.message`

修复没有增加宽泛 `Exception` 捕获，也没有吞掉 `MemoryError`、`BaseException`、
`SystemExit` 或 `KeyboardInterrupt`。本轮不要求、也不建议扩大该异常边界。

### 3.2 独立 RED 的充分性

独立 RED 提交 `ce4e27d2fefb746543197a77c728804b19b211d1` 共冻结 8 个案例：

| literal 入口 | 运算 | 稳定 path |
|---|---:|---|
| `Literal[...]` member | `+1j` / `-1j` | `/annotation` |
| parameter default | `+1j` / `-1j` | `/default` |
| `Field(ge=...)` bound | `+1j` / `-1j` | `/annotation/metadata/0/ge` |
| nested JSON default | `+1j` / `-1j` | `/default` |

每个案例都使用 1024 位十进制整数，并显式证明：

```text
1024 < 4096
```

因此该问题不是 Authoring 整数预算拒绝，而是
`ast.literal_eval()` 在预算检查前抛出的真实 `OverflowError`。RED 还先直接证明
CPython seam 会抛出 `OverflowError`，再通过公共 parser 连续调用两次冻结完全
相同的 `code/path/message`。

当前候选上的四组目标测试共 167 项全部通过，说明一行 production 修复与这 8
个回归案例形成了有效的 RED/GREEN 闭环。

### 3.3 普通 complex 语义未被放宽

普通小 complex 仍会在 `literal_eval()` 成功后进入既有类型白名单，并因不属于
`str/bool/int/finite float` 而稳定拒绝。加入 `OverflowError` 只改变异常归一化，
没有把 complex 扩入 Parameter Annotation v1。

## 4. 同一 literal seam 的剩余风险

四类入口最终都汇入 `_literal_value()`；当前窄捕获已覆盖可由合法 literal AST
输入直接触发并需要归一化的：

- `OverflowError`
- `RecursionError`
- `TypeError`
- `ValueError`

随后使用显式栈遍历 literal 值，避免在嵌套容器预算检查中重新引入递归深度泄漏。
没有执行 import、`eval()`、`exec()`、函数调用、属性访问或任意用户代码。

在本轮固定风险面内未发现新的输入级异常类型需要加入捕获集合。资源耗尽类异常和
`BaseException` 不属于应被 Annotation Schema 错误吞掉的输入合同。

## 5. 4096 位 Authoring 整数预算

最终目标回归确认：

- 4096 位十进制整数可被 Authoring parser 接受；
- 可完成 parse、canonical descriptor、render、`ast.unparse()` 与再次 parse；
- 4097 位正、负整数均稳定拒绝；
- hex、octal、binary 不依赖源码字符数，而按解析后的整数绝对值执行同一预算，
  因而不能绕过；
- list、tuple、set、dict key/value 等嵌套 literal 容器都进入同一迭代预算检查；
- 1024 位整数与 `±1j` 的 overflow 不会被错误归类为 4096 位预算超限。

预算只存在于不可信 Authoring AST seam。可信的
`WorkflowInputContract` canonical/runtime 值仍允许超过 4096 位的任意 Python
整数；实现没有调用或修改 `sys.set_int_max_str_digits()`，进程级 `sys` 设置保持
不变。

因此本轮未发现十进制边界、非十进制写法或嵌套容器形成的预算绕过。

## 6. B01 collision 与严格 number 语义

B01 修复继续使用：

1. 先按精确类型白名单验证 Literal/enum 成员；
2. 对已验证值排序；
3. 只比较相邻值判断重复；
4. 返回时保留原始声明顺序。

该路径从可预测整数 hash collision 下的 set 去重，改为确定性的
`O(n log n)` 时间和 `O(n)` 临时空间，不再承受构造性 `O(n²)` hash collision。
它没有新增宽度上限，也没有改变外部 enum 顺序。

严格语义保持不变：

- `bool` 不被当作 `int`；
- string、boolean、integer 家族继续严格区分；
- integer 与有限 float 仍属于 number 兼容族；
- `1` 与 `1.0` 仍视为重复；
- `-0.0` 与 `0`/`0.0` 仍视为重复；
- `NaN`、正负无穷和普通 complex 仍拒绝；
- duplicate 的稳定 path 和消息不变。

本轮未发现 B01 collision 回归、严格 number 语义漂移或新的预算绕过。

## 7. Parser-only、安全边界与 NB01

`parse_parameter_annotation()` 仍只接收静态 AST 和静态 import identity：

- 不 import 用户模块；
- 不执行注解或 default；
- 不执行 action、Workflow 或设备代码；
- 不解析真实 Registry template UUID；
- `ParsedParameter` 仍只能由模块内 parser token 构造。

当前 production 中尚无新增 caller。由此，NB01 的处理仍然正确：

> 当前 parser 保留 `ResourceTemplateSymbol`，但真实 Registry symbol 解析与模板
> UUID 绑定尚未接入 production caller。

这不影响 02B1 深模块本身合并；它是未来 caller 接线的进入门禁，而不是要求本轮
提前扩大到 Registry/HTTP/compiler 的理由。

## 8. Standards 与范围复核

production 修复只有一个精确异常类型，仍位于已有深模块边界内。没有新增公共
接口、重复分支或 speculative abstraction；caller 不需要了解 CPython
`literal_eval()` 的具体失败集合。

注释、docstring 与错误消息继续使用简体中文，类型标注与 canonical schema
形状未改变。没有发现仓库标准违反，也没有发现需要升级为 finding 的
Duplicated Code、Shotgun Surgery、Speculative Generality 或宽泛异常捕获。

候选没有触碰 FE、Backend、HTTP route、Catalog、SQLite、compiler 或执行路径，
符合本轮停止线。

## 9. 实际门禁

全部实际执行使用固定解释器：

```text
/home/changjunhan/.micromamba/envs/unilab/bin/python
```

本独立 worktree 的结果：

```text
Parameter Annotation 四组目标：
  167 passed in 1.40s

02A Schema/route 累计：
  212 passed, 1 warning in 2.01s

Registry：
  193 passed in 3.15s

Workflow：
  644 passed, 4 warnings in 28.28s

Ruff E/F/I：
  All checks passed

Ruff format --check：
  7 files already formatted

git diff --check ca6083b...4469953：
  passed

git diff --check 4469953...163b044：
  passed
```

Workflow warning 是既有 `httpx/starlette` deprecated、无效转义和 FastAPI
`on_event` deprecated 提示，不是本轮新增失败。

正式全量测试没有在本独立 worktree 重复执行；引用主执行方在相同 production/test
SHA `4469953` 上的正式记录：

```text
1223 passed, 3 skipped
```

## 10. 最终合并意见

| 类别 | 数量 | 结论 |
|---|---:|---|
| Blocking | 0 | 无 |
| Non-blocking | 1 | NB01，未来 caller 接线前关闭 |

最终判断：

1. `OverflowError` 泄漏已由精确一行修复关闭；
2. 四入口、正负号、1024 小于 4096 和稳定错误合同已冻结；
3. 普通 complex、4096/4097、非十进制、嵌套容器、B01 collision 与严格 number
   语义均无回归；
4. parser-only、可信任意整数和 `sys` 全局状态边界保持不变；
5. 没有要求吞掉 `MemoryError` 或 `BaseException`；
6. 没有发现新的 blocking。

**允许合并 Round 02B1。**
