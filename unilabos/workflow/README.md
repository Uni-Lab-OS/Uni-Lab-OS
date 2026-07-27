# Workflow authoring and compilation

本目录负责把 JSON/Python 编写内容收敛为同一个 Canonical `WorkflowRevision` v2，
并将通过验证的 revision 编译为调度器使用的完整 `TaskDag`。它不执行设备动作。

## 核心路径

```text
Canonical v2 ── to_python_script ──► Python + source map
     ▲                                      │
     └──── from_python_script (AST only) ◄──┘
                       │
                       ▼
          canonical_ir candidate validation
                       │
                       ▼
             dag_compile / TaskDag
```

- `canonical.py`：Canonical revision、invocation、edge、binding、source map 模型。
- `from_python_script.py`：只读 AST 的 Python → Canonical 编译器。
- `source_library.py`：显式配置的跨文件工作流函数索引；只读源码，不执行 import。
- `to_python_script.py`：Canonical → 可读 Python 投影，并反向编译自校验。
- `canonical_ir.py`：authoring API 候选、诊断、action catalog 验证。
- `dag_compile.py`：Canonical revision → 完整执行 DAG。
- `bindings.py`、`expression.py`：参数、节点输出和表达式绑定。
- `contracts.py`：工作流契约辅助。
- `schemas/runtime/v1/`：公开 schema。

## Authoring API 的三阶段

1. `generate-python`：输入 `base_revision_id`、`canonical_ir`、`source_uri`，返回候选
   `canonical_ir`、`python_source`、`source_map` 和 diagnostics。
2. `compile`：输入 `base_revision_id`、`python_source`、`source_uri`，调用
   `compile_python_script` 生成候选。
3. `validate`：输入 `base_revision_id` 和完整 candidate，确认 revision、schema、
   action catalog 与 source map 后才允许应用/保存/运行。

失败必须返回结构化 diagnostics 或 fail closed；不能偷偷回退为原图并宣称成功。

## Python 安全边界

工作流 Python 是“编写语言”，不是运行脚本：

- 只允许由 AST 编译器支持的声明、控制块和设备调用。
- 复合工作流可用 `from <configured-library> import <workflow>` 和普通函数调用；
  OS 从 `--workflow-library python.module=/source/root` 指定的根目录静态解析被调
  `.py`，递归编译为带 `os_control.group` 边界的完整 DAG。
- 不执行 Python import、`eval`、`exec` 或启动子进程执行用户源代码。
- `authoring.py` 中的对象仅服务类型提示和编辑器体验；其运行时调用主动报错。
- 静态循环和节点总数有上限，避免编译期无限/爆炸展开。
- 不支持的 AST、未知 action、错误参数和不安全表达式必须拒绝。

## Node id 与 source map

- 每个设备节点和控制节点都有稳定 `node_id`。
- Python source map 必须覆盖所有节点；隐式 join 也要有稳定代码位置或生成注释。
- diagnostics、代码选中、起始点、断点和 DAG 节点均用同一 `node_id` 对齐。
- 重新编译产生的新 revision 必须返回新的完整 source map，不能沿用陈旧行号。

## 绝对不能做

- 不能从前端画布的有损 nodes/edges 重建执行 revision。
- 不能执行用户 Python 来“获得”工作流。
- 不能忽略控制节点、分支标签、bindings 或 source map。
- 不能把校验失败的 candidate 写入 store 或交给 runtime。
- 不能为了兼容旧格式改变 v2 schema 的既有字段语义。

## 验证

```bash
UNILAB_PY=/home/changjunhan/.micromamba/envs/unilab/bin/python
"$UNILAB_PY" -m pytest tests/workflow
```

修改转换器时至少覆盖 JSON → Python → Canonical 往返、控制节点、source map、
不支持 AST、未知 action、绑定校验和编译上限。
