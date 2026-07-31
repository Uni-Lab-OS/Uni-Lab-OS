# Round 02B1：Parameter Annotation 趋势与策略报告

日期：2026-07-31

分支：`migration/02b-annotation-schema`

基线：`ca6083b`

候选：`64f3fc3`

状态：**实现与正式测试完成，等待三名独立 reviewer 依次关闭门禁。**

## 1. 本轮交付

本轮新增纯 AST 的共享 Parameter Annotation 深模块：

- `unilabos/registry/annotation_schema.py`
  - 将有限 Python 参数注解解析为一个 canonical
    `WorkflowInputContract`；
  - 支持 D-082～D-091 冻结的 scalar、list、nullable、`Literal`、
    `Field` 与 `ResourceSlot` 语法；
  - 只保存 `AllowedResourceTemplates` 的静态 import identity，不提前伪造
    Catalog UUID；
  - 从 canonical descriptor 确定性渲染 annotation AST；
  - parser 不 import、eval、exec 或执行作者表达式。
- `unilabos/registry/annotations.py`
  - 提供源码可 import 的 `JSONValue`；
  - 提供冻结的 `AllowedResourceTemplates` metadata。

本轮没有接入旧 Registry scanner，没有修改 HTTP、Catalog、SQLite、前端或
Backend。Action result record 继续留给 02B2。

## 2. 代码与测试增量

| 类别 | 文件数 | 新增行 | 删除行 |
|---|---:|---:|---:|
| 生产代码 | 2 | 649 | 0 |
| 独立合同测试 | 1 | 1126 | 0 |
| 实现前设计 | 1 | 243 | 0 |

测试代码明显多于生产代码，是因为有限语法的价值主要来自闭合拒绝矩阵：测试不仅
证明接受什么，也证明遇到动态表达式、非冻结 import identity、任意 Union、
可执行 default 或非 v1 metadata 时会失败关闭。

## 3. RED → GREEN 证据

独立测试作者在没有生产模块的分支上冻结 127 个用例：

```text
127 failed
统一首因：ModuleNotFoundError: unilabos.registry.annotation_schema
```

首次实现后：

```text
126 passed
1 个测试夹具在调用 parser 前发生 INTERNALERROR
```

该 INTERNALERROR 的根因是夹具先替换 `builtins.__import__`，随后 pytest
`monkeypatch.setattr` 自身才惰性 import `inspect`。同一名独立测试作者只移动
一行 guard 安装顺序；其测试分支仍保持可计数 RED，合入实现候选后：

```text
127 passed
```

这不是产品并发或注解合同缺陷，也没有通过放宽断言解决。

## 4. 门禁结果

```text
Parameter Annotation 目标：127 passed
02A Schema/route 累计：212 passed
Registry：153 passed
Workflow：644 passed
正式 tests：1183 passed, 3 skipped, 19 warnings
Ruff E/F/I：passed
Ruff format --check：passed
git diff --check：passed
```

warnings 均来自既有 FastAPI、ROS test class、SOCKS 可选依赖与 lifespan
deprecated 提示；没有本轮新增 warning。

## 5. 问题趋势

测试用例数不能当作问题数。127 个初始失败是 TDD 对“计划新增模块不存在”的同一
预期 RED，不表示发现了 127 个设计问题。

本轮实际问题变化如下：

| 阶段 | 新发现的独立问题 | 已关闭 | 尚未关闭 |
|---|---:|---:|---:|
| 独立测试冻结 | 0 个产品问题 | 0 | 0 |
| 首次实现验证 | 1 个测试基础设施问题 | 1 | 0 |
| 回归与正式门禁 | 0 个产品回归 | 0 | 0 |

相对 02A4 曾由 reviewer 新发现 1 个 blocking Standards 问题，本轮在正式测试
候选阶段尚无 blocking 产品问题。当前证据支持“问题面继续收敛”，而不是迁移在
不断制造新的交互状态或 Authority。

## 6. 策略调整

1. 继续保持 Parameter Annotation 是深模块：后续 compiler 与 Registry
   复用它，不各自实现类型猜测。
2. 02B1 合并前依次做合同、模块安全、最终风险三轮独立评审；同时只运行一名
   subagent。
3. reviewer 若发现 blocking，修复后必须在新 production SHA 上重新跑正式门禁
   并逐个复核，不能沿用旧评审结论。
4. 02B2 只增加 Action result record，不趁机接旧 Registry 或 HTTP，继续缩小
   每轮变更半径。
5. Catalog identity resolution、完整 compiler/transform/generate-python 各自
   后移到独立 round；这些生产接口可合并后才触发前端单编辑权实现与 FE-OS
   联调。

## 7. 前端与 Backend 覆盖

- 前端：**未覆盖、未修改**；
- Backend：**未覆盖、未修改**；
- 本轮仅完善 OS 内部共享 Authoring/Registry annotation interface。

前端启动条件仍是 OS 生产
`compile/transform/generate-python` 路径通过合并门禁，而不是仅有 schema 或
设计文档。
