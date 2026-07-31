# Phase 02A3：Workflow JSON 资源预算与完整值深度趋势报告

日期：2026-07-31

基线：`3e63ecf`

实现候选：`d306004`

## 1. 本轮结论

本轮以 D-101 和独立 RED 关闭第二轮安全评审的两个 blocking：

- OS 公共 Workflow JSON ingress 现在限制 8 MiB body、4096 位 integer token 和
  完整 JSON document 10000 层；
- Schema 的深度预算改为完整 normalized value 或完整 Contract canonical payload，
  array 与 Contract wrapper 都参与计数。

可信内部 decoder 缺省仍支持任意 Python `int`，没有修改
`sys.set_int_max_str_digits`。HTTP 超限统一返回既有 `400 invalid_input`，declared
oversize 零读取，streamed oversize 在第一个超限 chunk 后立即停止，service
调用和副作用均为零。

本轮修改的是 OS HTTP adapter 与纯模块，没有修改 Backend 或前端，也没有覆盖
FE-OS 联调。

## 2. 测试与门禁

独立测试作者新增 16 个 case。修复前 `10 failed, 6 passed`：

- 4 个 external integer budget case 缺 decoder 参数；
- 1 个 HTTP 4097 位 integer 穿透到 service；
- 1 个 oversized `Content-Length` 仍读取并落到 service；
- 2 个 missing/chunked stream 继续读取超限后的 sentinel；
- 1 个 `list[object]` 漏计 list root；
- 1 个 Input default 漏计 Contract wrapper。

修复前已经通过的 6 个临界 case继续保持：可信 5001 位整数、精确 8 MiB、
standalone 10000/10001、list item 9999 和 Input default 9997。

修复后：

| 门禁 | 结果 |
|---|---:|
| 02A3 + 全部 Schema/codec case | 189 passed |
| Workflow 累积测试 | 621 passed |
| 正式完整测试集 `pytest tests -q` | 1033 passed, 3 skipped |
| Ruff `E/F/I` | 通过 |
| Ruff format（本轮文件） | 通过 |
| `git diff --check` | 通过 |

## 3. 实现规模

以下数据相对基线统计，暂不计本趋势报告自身：

| 类型 | 文件数 | 新增行 | 删除行 | 净增 |
|---|---:|---:|---:|---:|
| Production | 3 | 77 | 12 | 65 |
| Test | 1 | 422 | 0 | 422 |
| 决策、审计与修复设计 | 3 | 126 | 5 | 121 |
| 合计 | 7 | 625 | 17 | 608 |

测试与 production 新增行比约为 `5.48:1`。主要原因是测试用真实 ASGI
scope/receive spy 证明“拒绝时没有继续读取或调用 service”，并以迭代 helper 覆盖
接近 10000 层的四个上下文，而 production 只增加一个流式 body seam、一个 decoder
预算参数和一个完整值 validator 末端。

## 4. 问题趋势

| 阶段 | 已知 blocking | 新增产品状态/Authority | 正式通过测试数 |
|---|---:|---:|---:|
| 第二轮安全评审后 | 2 | 0 | 1017 |
| 02A3 独立测试 RED | 2 | 0 | 原 Schema 173 |
| 02A3 实现候选 | 0（待复审确认） | 0 | 1033 |

问题继续收敛。B-05 被收口为一个不可信 transport budget，B-06 被收口为一个统一的
complete-value invariant；都没有增加 Workflow 业务状态、token、Authority 或
持久模型。

## 5. 策略调整

本轮通过领域边界把“数学值能力”和“外部计算预算”分离：canonical/store 是可信
内部语义，HTTP ingress 是不可信适配器；两者复用同一 codec，但由 adapter 显式传入
预算。深度同样只保留“完整值”一个定义，后续 compiler、Task preflight 和输出校验
不再自行计算 wrapper 余量。

下一步由第二名模块/安全 reviewer 复审 B-05/B-06。清零后，再按迁移门禁完成额外
独立风险评审和 integration 合并验证。前端仍在生产 Authoring compiler 与纯转换
Interface 就绪后单开分支启动；继续不修改 Backend。
