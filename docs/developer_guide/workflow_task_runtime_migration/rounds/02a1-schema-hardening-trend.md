# Phase 02A1：Schema canonical value hardening 趋势报告

日期：2026-07-31

基线：`6cb6e27`

修复候选：`24d1c28`

## 1. 本轮结论

本轮以独立 RED 回归关闭第一轮合同评审发现的三个实现缺口：

- value object 不再持有或暴露可变 canonical 容器，公开构造不能绕过 parser；
- nullable 的 non-null member 不能再次是 nullable；
- 深层合法 opaque JSON default 与 `to_dict()` 不再经过递归 `deepcopy`。

实现使用现有非递归 JSON codec 持有不可变 bytes payload，每次 dump 解码为独立
JSON 容器。没有增加 schema 类型、合同字段、持久状态、HTTP 路由或 Authority。
本轮没有修改 Backend 或前端，也尚未覆盖 FE-OS 联调。

## 2. 测试与门禁

独立测试作者新增 14 个 case。修复前是 `10 failed, 4 passed`：

- 3 个非法直接构造失败；
- 3 个可变 `_data` 暴露失败；
- 3 个双层 nullable 未拒绝；
- 1 个深度 1200 default 泄漏 `RecursionError`；
- 4 个既有 dump 独立性和超深稳定错误 case 保持通过。

修复后：

| 门禁 | 结果 |
|---|---:|
| 02A1 加固 + 原 v1 Schema 合同 | 162 passed |
| Workflow 累积测试 | 594 passed |
| 正式完整测试集 `pytest tests -q` | 1006 passed, 3 skipped |
| Ruff `E/F/I` | 通过 |
| Ruff format（本轮文件） | 通过 |
| `git diff --check` | 通过 |

## 3. 实现规模

以下数据相对基线统计，暂不计本趋势报告自身：

| 类型 | 文件数 | 新增行 | 删除行 | 净增 |
|---|---:|---:|---:|---:|
| Production | 1 | 81 | 33 | 48 |
| Test | 1 | 336 | 0 | 336 |
| 修复设计 | 1 | 80 | 0 | 80 |
| 合计 | 3 | 497 | 33 | 464 |

本轮测试与 production 新增行比约为 `4.15:1`。原因是深层对象使用迭代式测试
helper，并分别覆盖三个 value object 和三种错误路径；production 实际只收紧一个
canonical ownership seam 和一个 grammar flag。

## 4. 问题趋势

| 阶段 | 已知 blocking | 新增产品状态/Authority | 正式通过测试数 |
|---|---:|---:|---:|
| 02A 首轮评审后 | 3 | 0 | 992 |
| 02A1 独立测试 RED | 3 | 0 | 原合同 148 |
| 02A1 修复候选 | 0（待复审确认） | 0 | 1006 |

问题再次收敛。评审新增的三个问题都已转成明确回归，没有导出新的产品决策或跨模块
依赖；当前风险从“字典是否真的不可变”变成了一个可单独评审的 bytes ownership
实现。

## 5. 策略调整

本轮不再用 frozen dataclass 包装可变容器，而是把“validated canonical value”做成
真正的深模块边界：parser 独占创建，调用方只能取得独立 dump。后续 annotation
parser、compiler 和 Task preflight 都依赖此 Interface，不接触内部 payload。

下一步先由原第一名 reviewer 逐项复审 B-01～B-03；清零后，再由第二名 reviewer
检查模块边界、资源上限和异常稳定性。两轮都通过才合并 integration。前端仍等最小
生产 Authoring compiler 与纯转换 Interface 就绪后，从独立前端分支启动；继续不
修改 Backend。
