# Round 02G1：local Authority Candidate round-trip 联调修正

## 1. 来源与定位

本 Round 不是既定 `02H — Task input preflight` 的改名或扩张，而是 Round FE-D117 使用
真实 production OS、SQLite、`TemplateCatalog`、`WorkflowAuthoringEngine` 和 SSE 联调时
发现的 02G 集成阻塞修正。

可重复证据是：local `CatalogAuthority` 按合同为 Catalog import 分配 server-owned
Template/Handle UUID 后，persistent Authoring GET 返回的自身 Candidate graph 原样提交给
`POST /api/v1/authoring/generate-python`，接口返回
`round_trip_mismatch: Candidate graph 不能证明为等价的 Python`。固定 Backend-owned UUID
的 02D 单元 fixture 没有覆盖这条 production local Authority 路径。

该缺陷阻塞 D-117 画布模式，因为前端必须先让 OS 从完整 Candidate graph 生成确定性
Python，不能在前端绕过 round-trip proof、猜测 UUID 或生成源码。

## 2. 目标

对同一个 production engine/catalog snapshot，证明以下闭环成立：

```text
package Python
  -> persistent compile/Candidate
  -> Authoring aggregate
  -> pure generate-python
  -> 等价完整 graph + deterministic normalized Python
```

生成结果必须继续通过 closed Candidate validation、Catalog projection、Workflow identity、
完整语义 graph 比较和 normalized-source proof。修复不得把严格比较改成“忽略所有
Template/Handle UUID”，也不得允许 local Catalog import 注入客户端 UUID。

## 3. 边界与停止线

本轮只修改 Uni-Lab-OS Authoring engine/Catalog 集成及其测试和报告：

- 不修改 Frontend；FE-D117 独立分支保持等待，修复合入 OS integration 后继续；
- 不修改 Backend；
- 不改变 Apply 单 `candidate_hash`、Draft PUT 双 CAS 或 SSE schema；
- 不实现 02H Task input preflight、P0-4、P0-5、Debugger、Conditional Join；
- 不引入客户端 Candidate identity、route mock 或前端 Python 生成器。

## 4. 测试优先验收

唯一独立 test-author 先提交 RED tests，至少覆盖：

1. local Authority 拒绝 caller UUID，并由 Catalog 分配真实 Node/Handle Template UUID；
2. 使用上述 Catalog 编译有效 Python 后，生成自身 Candidate graph 必须成功；
3. persistent production composition 的 aggregate Candidate 原样进入 pure
   `generate-python` 后成功，并保留完整 graph；
4. 非默认 scalar 修改后 compile -> generate 仍保持输入 contract/default；
5. output bindings、Node input bindings、Edge Handle identity 和 Catalog projection 不漂移；
6. 真正不等价或不属于当前 Catalog 的 graph 继续稳定失败；
7. 02D engine、02E pure API、02G persistent production 和完整 OS suite 无回归。

## 5. 合并门

本轮从 `integration/workflow-task-runtime@0138044` 新建
`migration/02g1-local-authority-roundtrip`。一个 test subagent 完成 RED 合同后才实现；一个
review subagent 对 Standards/Spec 两轴审查。focused、Phase 02 累积、完整测试、Ruff、
format 和 `git diff --check` 全绿且 review blocking 为 0 后，非 squash 本地合入
`integration/workflow-task-runtime`，不 push。Round 结束写中文趋势/策略报告；随后立即
回到 FE-D117 真实联调，不等待人工确认。
