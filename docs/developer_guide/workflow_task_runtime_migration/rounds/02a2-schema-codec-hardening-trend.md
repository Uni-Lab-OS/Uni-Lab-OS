# Phase 02A2：canonical deletion 与 JSON 大整数趋势报告

日期：2026-07-31

基线：`d340b19`

修复候选：`4c845c9`

## 1. 本轮结论

本轮以独立 RED 回归关闭 02A1 复审剩余的两个 blocking：

- `_CanonicalValue` 现在同时封锁普通属性赋值和删除；
- 公共 `json_codec` 以固定小块十进制迭代算法编码、解码任意位数的 Python
  `int`。

实现没有调用或修改 `sys.set_int_max_str_digits`，没有增加新的产品整数上限，也
没有把大整数包装成 JSON 字符串。Schema constraint、Input default、独立 value
和持久 canonical payload 继续共享同一标准 JSON number 语义。

本轮没有修改 Backend 或前端，也没有覆盖 FE-OS 联调。

## 2. 测试与门禁

独立测试作者新增 11 个 case。修复前是 `9 failed, 2 passed`：

- 3 个 typed value object 删除 payload 未被阻止；
- 正、负 5001 位整数的 2 个 encode 和 2 个 decode 泄漏 `ValueError`；
- Schema bound 与 Input default 各 1 个 canonical encode 泄漏 `ValueError`；
- integer/number 的 2 个独立 normalize case 已经通过。

修复后：

| 门禁 | 结果 |
|---|---:|
| 02A2 + 全部 Schema 合同/加固 | 173 passed |
| Workflow 累积测试 | 605 passed |
| 正式完整测试集 `pytest tests -q` | 1017 passed, 3 skipped |
| Ruff `E/F/I` | 通过 |
| Ruff format（本轮文件） | 通过 |
| `git diff --check` | 通过 |

## 3. 实现规模

以下数据相对基线统计，暂不计本趋势报告自身：

| 类型 | 文件数 | 新增行 | 删除行 | 净增 |
|---|---:|---:|---:|---:|
| Production | 2 | 41 | 2 | 39 |
| Test | 1 | 167 | 0 | 167 |
| 修复设计 | 1 | 62 | 0 | 62 |
| 合计 | 4 | 270 | 2 | 268 |

测试与 production 新增行比约为 `4.07:1`。production 的实际改变集中在两个很小
的 seam：3 行 value object 删除防护，以及 38/-2 行公共整数 codec；测试分别验证
两个符号、两个方向和三个 Schema 消费位置。

## 4. 问题趋势

| 阶段 | 已知 blocking | 新增产品状态/Authority | 正式通过测试数 |
|---|---:|---:|---:|
| 02A1 复审后 | 2 | 0 | 1006 |
| 02A2 独立测试 RED | 2 | 0 | 原 Schema 162 |
| 02A2 修复候选 | 0（待复审确认） | 0 | 1017 |

问题继续收敛。B-01 从“可变 dict、可绕过构造、可删除 payload”缩小为完整封锁普通
属性操作；B-04 被定位为既有公共 codec 的解释器边界，而不是新的 Workflow 产品
状态。修复没有新增 token、Authority、持久模型或 HTTP 字段。

## 5. 策略调整

公共 JSON 语义只保留一套 codec：Schema 不另建大整数序列化，也不通过修改全局
解释器配置制造并发风险。后续模块直接依赖 `json_codec` 的标准 JSON round-trip。

下一步由原合同 reviewer 复审 B-01、B-04。若清零，再启动第二名独立 reviewer
检查整个 02A/02A1/02A2 候选的模块边界、资源上限和异常稳定性。两轮通过后才合并
integration。前端仍在生产 Authoring compiler 与纯转换 Interface 就绪后，从独立
前端分支启动；继续不修改 Backend。
