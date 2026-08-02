# Phase 02A4：Workflow route body budget 趋势报告

日期：2026-07-31

分支：`migration/02a4-route-body-budget`

基线：`1111380acd9e57be1f56223d9aa74a7ff5cb1304`

最终已测 production 候选：`c0d5874`

上游交付跟踪：

- Core 决策：[D-117](https://github.com/Uni-Lab-OS/Uni-Lab-Core/issues/139)；
- OS owning issue：[deepmodeling/Uni-Lab-OS#300](https://github.com/deepmodeling/Uni-Lab-OS/issues/300)。

## 1. 本轮结论

最终风险评审的 B-07 已在实现候选上关闭：

- FastAPI 已解析出 `body_field` 的 Workflow route，不再允许调用方通过伪造或省略
  `Content-Type` 关闭 8 MiB 请求体预算；
- 声明超限在零次 `receive` 时失败；
- 未声明长度或 chunked body 在第一个超限 chunk 后停止；
- 非 JSON body 只缓存受限 bytes，仍交给 FastAPI 执行原内容类型校验；
- JSON 继续使用唯一的整数/深度 decoder；
- GET、SSE 和其他无 body field 的 route 不再因为 JSON MIME 主动读取 body。

本轮没有新增业务状态、HTTP DTO、Backend 或前端实现。

## 2. 独立 RED 与实现

独立测试作者从设计提交 `66a5475` 开始，只新增公共 ASGI 接缝测试：

- 原始测试提交：`677371c8447d79de5c8759de76c4fe7c2efc404d`；
- 纳入实现分支的测试提交：`9d29454`；
- 新增 1 个测试文件、277 行、23 cases；
- RED：`20 failed, 3 passed`；
- 20 个失败分别证明任意非 JSON MIME 可绕过 declared/actual body budget，以及
  bodyless GET/SSE 被旧 JSON preload 抢先处理；
- 3 个 exact-limit case 证明既有 FastAPI validation envelope 和零业务副作用
  没有被测试误判为缺口；
- 原 D-101 16 cases 和全部 189 个 Schema/资源 cases 在测试分支保持绿色。

实现提交 `a54c402` 只修改 `_BackendJSONRoute` 和预算命名：

- production：1 个文件，新增 16 行、删除 14 行；
- 没有修改测试断言；
- route 在 handler 构造时冻结 `body_field` 判断，不增加全局 middleware 或第二套
  parser。

## 3. 规模

相对本轮基线 `1111380`：

| 类型 | 文件数 | 新增行 | 删除行 | 净增 |
|---|---:|---:|---:|---:|
| Production | 1 | 16 | 14 | 2 |
| Test | 1 | 277 | 0 | 277 |
| 决策与设计 | 2 | 64 | 1 | 63 |
| 合计（报告加入前） | 4 | 357 | 15 | 342 |

实现代码保持很小；本轮行数主要来自独立参数化 ASGI 合同矩阵，而不是新的生产复杂度。

## 4. 测试门

候选 `a54c402` 已通过：

```text
新增 route body + 原 D-101：
39 passed

Workflow 累积：
644 passed

正式完整 tests：
1056 passed, 3 skipped

Ruff E/F/I：
通过

Ruff format --check：
通过

git diff --check：
通过
```

完整测试的 19 个 warning 均为既有依赖弃用、测试类收集和可选 SOCKS 依赖提示，
没有本轮新增失败。

## 5. 问题趋势与策略调整

| 指标 | 本轮前 | 实现候选 |
|---|---:|---:|
| 02A 已知 blocking | 1 | 0（三名独立 reviewer 已确认） |
| 新增产品状态/DTO | 0 | 0 |
| 新增 non-blocking | 0 | 0 |
| 保留 non-blocking | NB-01 | NB-01 |

问题继续变少。风险已经从 Schema 语义、不可变性、递归和大整数边界收敛到一个
route-local 的 MIME 绕过；本轮没有发现新的架构分支。

策略调整：

1. 后续公共资源预算继续绑定“route 是否声明 body”，不绑定调用方可伪造的 MIME；
2. JSON 计算预算仍只属于 JSON decoder，不把 HTTP framing parser 复制进业务层；
3. NB-01 的宽松 `Content-Length` 词法保持 non-blocking，不在本轮扩大修改面；
4. 先由最终风险 reviewer 复审 B-07，再由合同与模块 reviewer 对固定候选分别确认；
5. 三名 reviewer 全部通过后，才合并完整 02A lineage，并从 integration 新开下一个
   OS Authoring Interface round。

## 6. 前端与合并状态

- 本轮未覆盖前端，也未进行 FE-OS 联调；
- 未修改 Backend；
- 候选已经通过三名顺序独立 reviewer，可以合并完整 02A lineage；
- 完整 02A 合并后，继续完成生产 Annotation、Catalog、compiler、transform 和
  `generate-python`；这些 OS Interface 可合并后，才触发独立 FE 分支。

## 7. 最终评审闭环

第一次三名顺序评审中：

1. 风险 reviewer 确认 B-07 为 `accepted-fixed`；
2. 合同 reviewer 确认 D-101、D-117、route shape、错误信封和 Apply 单 token
   均一致；
3. 模块 reviewer 确认 B-01～B-07 未重开，但发现本轮改写的 class docstring
   仍是英文，记为 Standards blocking S-03。

主执行者把该 docstring 改为准确的简体中文，形成新 production SHA `c0d5874`，
并重新运行：

```text
Schema/route 目标：212 passed
Workflow 累积：644 passed
正式完整 tests：1056 passed, 3 skipped
Ruff、format、diff-check：通过
```

production SHA 改变后没有沿用旧评审，而是重新顺序执行三名 reviewer：

| 顺序 | 评审 | 结果 |
|---:|---|---|
| 1/3 | 最终风险确认 | B-07、S-03 `accepted-fixed`，0 blocking |
| 2/3 | 合同确认 | D-101/D-117/错误信封/Apply 无变化，0 blocking |
| 3/3 | 模块安全确认 | B-01～B-07 均关闭，S-03 已修复，0 blocking |

最终保留三个不阻塞合并的 follow-up：

- NB-01：raw ASGI `Content-Length` 词法偏宽；
- NB-M01：FastAPI/Starlette 依赖升级时必须运行 body/cache 兼容门；
- NB-M02：D-117 新顶层 Authoring router 必须复用同一 route class，并增加
  OpenAPI `requestBody` 动态盘点。

问题趋势仍是收敛：本轮新增的唯一 Standards finding 已在同轮关闭，没有引入新的
产品语义、并发模型或持久状态。
