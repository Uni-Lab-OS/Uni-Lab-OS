# Phase 02A：Workflow v1 Schema 趋势报告

日期：2026-07-31

基线：`e85a60c`

实现候选：`44f4a26`

## 1. 本轮结论

本轮已经实现纯内存深模块 `unilabos.workflow.schema`，冻结并验证：

- v1 scalar、opaque object、ResourceSlot、array 和 nullable schema；
- 严格值规范化、enum、边界和 JSON Pointer 错误定位；
- Input Contract 的三种合法 required/default 组合；
- Output Contract 的闭合结构和显式输出规则；
- ResourceSlot UUID 与 allowlist 的结构验证。

本轮没有访问数据库、Catalog、Material Authority 或设备，没有新增 HTTP 路由，
也没有修改 Backend 或前端；因此尚未覆盖 FE-OS 联调。

## 2. 测试与门禁

独立测试作者先提交了 148 个合同用例。初始 RED 只有一个明确原因：
`unilabos.workflow.schema` 尚不存在。实现后结果如下：

| 门禁 | 结果 |
|---|---:|
| v1 Schema 独立合同测试 | 148 passed |
| Workflow 累积测试 | 580 passed |
| 正式完整测试集 `pytest tests -q` | 992 passed, 3 skipped |
| Ruff `E/F/I` | 通过 |
| Ruff format（本轮文件） | 通过 |
| `git diff --check` | 通过 |

## 3. 实现规模

以下数据相对基线统计，暂不计本趋势报告自身：

| 类型 | 文件数 | 新增行 | 删除行 | 净增 |
|---|---:|---:|---:|---:|
| Production | 1 | 815 | 0 | 815 |
| Test | 1 | 1219 | 0 | 1219 |
| 设计与执行计划 | 2 | 152 | 19 | 133 |
| 合计 | 4 | 2186 | 19 | 2167 |

测试代码与实现代码比约为 `1.50:1`。较高比例主要来自 table-driven 的合法形状、
非法路径、默认值和不可变性矩阵，不代表新增运行时状态。

## 4. 问题趋势

| 阶段 | 已知 blocking | 新增产品状态/Authority | 测试通过数 |
|---|---:|---:|---:|
| 设计冻结 | 1（模块尚未实现） | 0 | 既有 844 |
| 独立测试 RED | 1（缺少目标模块） | 0 | 既有 smoke 12 |
| 实现候选 | 0 | 0 | 正式 992 |

问题在收敛，没有继续扩散。148 个新增用例把 P0-2 已确认的有限合同展开成确定边界，
但没有发现需要新 token、新持久模型、新 Authority 或新 HTTP 字段的问题。

## 5. 策略调整

本轮维持“先纯合同、后边界适配”的深模块策略。下一步先由两名 reviewer 串行检查：

1. 合同与冻结决策是否一致；
2. 模块边界、错误稳定性和长期维护风险。

只有两轮评审和复验都通过才合并 integration。之后从新的 integration 基线开启
annotation parser/Catalog 投影轮；生产 Authoring compiler 和纯
`generate-python` Interface 就绪后，立即在独立前端分支实现代码/画布单编辑权并做
FE-OS 联调。整个过程继续不修改 Backend。
