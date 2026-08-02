# Phase 02A：Workflow v1 Schema 第二轮模块与安全评审

日期：2026-07-31

评审分支：`review/02a-module-safety`

固定原始集成基线：`e85a60c1acec53cf8d6e2643e40a7ba0c12cd36f`

固定候选：`fbf8bb20244ba50ee05051f006a766711bf0a93c`

评审范围：独立检查完整 02A、02A1、02A2 的模块深度、canonical value
不变量、异常稳定性、JSON depth/cycle/shared reference、大整数 codec
资源与并发安全、严格数值类型和范围停止线。本报告只修改评审文档，不修改
production、测试、Backend 或前端。

## 1. 结论

**本候选暂不可合并到 `integration/workflow-task-runtime`。**

| 评审轴 | Blocking | Non-blocking | 结论 |
|---|---:|---:|---|
| 模块与 Standards | 1 | 0 | 不通过 |
| 安全与稳健性 | 1 | 0 | 不通过 |

第一名合同 reviewer 已关闭 B-01～B-04；本轮不依赖其结论，重新检查后确认：

- 四个主操作形成较小而有杠杆的 Interface，finite grammar、strict
  normalization 和错误定位集中在一个纯内存深模块；
- 三种 canonical value object 在设计声明的普通 Python 操作范围内始终不可修改，
  公开构造不能绕过 parser，dump 不共享容器；
- cycle 被拒绝，非循环 shared reference 被复制为独立 JSON 容器；
- bool/int/float 分支保持严格，整数 chunk codec 不修改解释器全局状态；
- 没有引入 DB、HTTP 路由、Catalog、Material Authority、Backend 或前端实现。

但是，独立对抗检查发现两个现有测试没有覆盖的新 blocking：

1. 大整数 codec 在无任何不可信输入资源上限的公共 HTTP seam 绕过 Python
   防拒绝服务保护，且转换复杂度明显超线性；
2. opaque object 的深度预算按子树校验，却按带 Contract/array 包装的完整值解码，
   使 parser 能产生自己的 `to_dict()` 或公共 codec 无法消费的值。

自动测试全绿不能关闭这两个确定性反例。

## 2. 模块与 Standards 轴

### B-06：深度预算跨 Schema、Contract 与 codec seam 不一致

**级别：blocking-new**

02A1 设计要求：在 `MAX_BACKEND_JSON_DEPTH` 范围内的合法 opaque object
可作为独立值和 Input default，并可重复 `to_dict()`；超限失败必须是稳定的
`WorkflowSchemaError`
（`02a1-schema-hardening-design.md:53-64`）。

实现只在 `_validate_json_value` 中按 opaque object 子树计深
（`unilabos/workflow/schema.py:471-505`）。Input parser 随后把该 default
包进 parameter list、descriptor 和 Contract envelope，再由 `_from_canonical`
编码为 payload（`:53-74`、`:677-783`）。`to_dict()` 却以同一个
`MAX_BACKEND_JSON_DEPTH=10000` 解码完整 payload，因此 wrapper 层没有纳入
第一次校验的预算。

独立只读复现结果：

```text
opaque default depth 9996: parse=ok, to_dict=ok
opaque default depth 9997: parse=ok, to_dict=ok
opaque default depth 9998: parse=ok, to_dict=ValueError
opaque default depth 10000: parse=ok, to_dict=ValueError
```

裸异常均为：

```text
ValueError: JSON nesting exceeds the Backend limit
```

同一问题也出现在 `list[dict[str, JSONValue]]`：item object 深度 10000
可被 `normalize_value` 接受，但返回值多一层 array；对该返回值执行公共
`encode_json`/`decode_json_bytes` round-trip 时，decode 立即以同一
`ValueError` 拒绝。

这破坏了深模块最重要的不变量：通过 Interface 创建的 canonical value 应始终有效，
且调用方不应理解内部 wrapper 深度才能安全调用 `to_dict()`。它也会使后续 compiler、
Store 和 HTTP Adapter 对同一个已规范化值给出不同结论。

修复前必须先明确 `MAX_BACKEND_JSON_DEPTH` 的计量对象：

- 若它约束完整持久/传输 JSON，则 parser 必须把 Contract/array wrapper 纳入预算，
  并在创建 value object 前以稳定、带完整 pointer 的 Schema error 拒绝；
- 若它只约束 opaque subtree，则 canonical dump 与后续持久 codec 必须显式支持
  合法 wrapper 开销，且不能放宽不可信 HTTP 总深度。

需要独立 RED 覆盖 object depth 临界值、Input default parse/dump、`list[object]`
normalize/codec round-trip，以及超限时稳定 code/path。不能只把测试深度从 1200
提高而忽略 wrapper。

### 模块正向证据

- 公共 Interface 仍只有
  `parse_value_schema`、`parse_input_contract`、`parse_output_contract` 和
  `normalize_value`，复杂 grammar、默认值和错误路径没有散布给 caller；
- deletion test 成立：删除 `unilabos.workflow.schema` 后，严格类型、闭合对象、
  nullable/default 和 canonical ownership 复杂性会重新散布到 compiler、
  Task preflight 与前端，而不是消失；因此它是深模块，不是 Middle Man；
- typed value object 以 immutable bytes 持有 canonical payload，普通
  set/del、直接公开构造和 dump mutation 均不能破坏对象；未发现新的
  Mysterious Name、Feature Envy、重复 Authority 或 speculative Adapter；
- Schema 只复用公共 `json_codec` 和唯一 `validate_uuid`，没有复制 Material
  lookup、Catalog 或 runtime Authority；ResourceSlot allowlist 仍只做结构校验；
- 新增注释、docstring 和异常消息为简体中文，Python 3.11 类型标注完整。

## 3. 安全与稳健性轴

### B-05：公共 HTTP codec 绕过整数防护但没有替代资源上限

**级别：blocking-new**

02A2 为满足“任意 Python `int`”合同，使用固定 9 位 chunk 规避
`sys.get_int_max_str_digits()`，并明确不修改进程全局状态
（`02a2-schema-codec-hardening-design.md:27-43`）。不修改全局状态是正确的；
但候选同时让公共 HTTP decoder 接受任意位数 token，却没有新增请求体大小、
整数位数、CPU 或内存预算。

具体 seam：

- `_BackendJSONRoute` 先 `await request.body()` 读取完整请求体，再在 event loop
  内同步调用 `decode_json_bytes`
  （`unilabos/app/workflow_api.py:47-68`）；
- `_decode_json_integer` 对不断增长的 bigint 重复执行乘法，`_encode_json_integer`
  对不断缩小的 bigint 重复执行 `divmod`
  （`unilabos/workflow/json_codec.py:20-51`）；
- public route 没有 body 或 integer token 上限，codec 本身也没有对应参数；
- Python 原有的 4300 位保护在修改前会快速拒绝这种输入，候选现在主动绕过它。

同一环境的只读计时（标准十进制 `1` 后补零）：

| 位数 | decode | encode |
|---:|---:|---:|
| 5,000 | 0.000336 s | 0.000784 s |
| 50,000 | 0.018985 s | 0.060494 s |
| 500,000 | 1.771306 s | 5.868624 s |

另一次 500,000 位 round-trip 为 7.67 秒、峰值 RSS 17,588 KiB。500 KB 是普通
HTTP 基础设施常会接收的量级；单个最终会被业务模型拒绝的 numeric token 已能同步
占用 event loop 秒级时间。增长明显超线性，因而不能用“请求体本身只有 N 字节”
替代 CPU 边界。

现有 02A2 测试只证明 5001 位正确 round-trip 和全局设置不变，没有证明不可信 seam
有资源限制。该问题不是要求恢复 `sys.set_int_max_str_digits`；正确停止线是先冻结
一个产品可解释的外部资源合同，例如：

- HTTP 总 body 上限和/或单个 integer token 位数上限；
- 在任何 bigint 构造前以线性扫描检查并快速失败；
- 通过冻结 JSON error envelope 返回 `invalid_input`，不泄漏裸异常；
- 若内部 canonical encode 仍需支持任意 Python `int`，应把“内部可信值能力”和
  “外部不可信 decode 预算”分开，而不是复制第二套 codec。

该上限会改变 02A2“外部 codec 接受任意位数”的设计文字，因此必须先完成明确决策，
再由独立测试作者提供 RED 边界/性能守护。没有该决策和门禁，本候选不应接入生产
Authoring 或 FE-OS 联调。

### 安全正向证据

- `sys.set_int_max_str_digits` 未出现在 production；测试前后
  `sys.get_int_max_str_digits()` 保持不变，没有进程级并发副作用；
- bool 分支先于 int，strict scalar validator 使用精确 `type`，NaN/Infinity
  继续拒绝；small int、负数、零和标准 JSON number token 没有回归；
- `_validate_json_value` 的 active-path 集合能拒绝 cycle；同一容器被不同 sibling
  引用时不会误判为 cycle，最终 encode/decode 会消除共享引用；
- canonical payload 不保留 caller 的 dict/list 引用，`to_dict()` 每次返回独立
  容器；普通属性赋值和删除后对象仍完整；
- 没有发现 DB/HTTP/Material/Backend/FE 状态写入，也没有第二个 UUID 或 Material
  Authority。

## 4. 测试与门禁证据

本 reviewer 在固定候选上实际运行：

```text
/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q \
  tests/workflow/test_schema_codec_hardening.py \
  tests/workflow/test_value_schema_hardening.py \
  tests/workflow/test_value_schema_v1.py
=> 173 passed, 2 warnings

/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q tests/workflow
=> 605 passed, 3 warnings

/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -qq tests
=> exit 0；与固定候选登记的 1017 passed, 3 skipped 一致

/home/changjunhan/.micromamba/envs/unilab/bin/python -m ruff check \
  --select E,F,I --ignore E501 \
  unilabos/workflow/json_codec.py \
  unilabos/workflow/schema.py \
  tests/workflow/test_schema_codec_hardening.py \
  tests/workflow/test_value_schema_hardening.py \
  tests/workflow/test_value_schema_v1.py
=> All checks passed

/home/changjunhan/.micromamba/envs/unilab/bin/python -m ruff format --check \
  unilabos/workflow/json_codec.py \
  unilabos/workflow/schema.py \
  tests/workflow/test_schema_codec_hardening.py \
  tests/workflow/test_value_schema_hardening.py \
  tests/workflow/test_value_schema_v1.py
=> 5 files already formatted

git diff --check e85a60c...fbf8bb2
=> passed
```

另运行三个只读对抗 snippet，分别覆盖：

1. 深度 9996/9997/9998/10000 的 Input default parse/dump；
2. 深度 10000 的 `list[object]` normalize/codec round-trip；
3. 5000/50000/500000 位整数的 encode/decode 正确性与耗时。

这些 snippet 没有修改 production 或测试。

## 5. Finding disposition 与合并条件

| Finding | 当前 disposition | 合并前置条件 |
|---|---|---|
| B-01 canonical value 可破坏 | `accepted-fixed` | 保持既有回归 |
| B-02 嵌套 nullable | `accepted-fixed` | 保持既有回归 |
| B-03 深层 JSON 递归异常 | `accepted-fixed` | B-06 修复不得重新引入递归实现 |
| B-04 合法大整数裸异常 | `accepted-fixed` | B-05 修复不得恢复裸解释器异常或全局状态修改 |
| B-05 不可信大整数无资源边界 | `blocking-new` | 冻结外部资源合同；快速拒绝；独立 RED/GREEN |
| B-06 wrapper 深度预算不一致 | `blocking-new` | 统一深度计量；稳定错误；独立临界回归 |

B-05、B-06 全部修复后，必须在新的固定 SHA 重跑目标、Workflow、正式 `tests/`、
Ruff、format 与 diff gate，并由本模块/安全 reviewer 复审。任何 production
变更都会使当前评审 SHA 失效。在此之前，02A 不可合并 integration，也不应作为
生产 Authoring compiler 或前端联调的可信 Schema seam。
