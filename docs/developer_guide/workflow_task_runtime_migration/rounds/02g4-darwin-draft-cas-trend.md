# Round 02G4：Darwin Draft CAS 能力边界趋势记录

状态：**实现候选；精确测试 SHA、完整门禁和独立 reviewer 最终 disposition
记录在候选提交的 Git note。**

基线：`e1279926`（`integration/workflow-task-runtime`）

分支：`migration/02g4-darwin-draft-cas`

## 1. 问题与边界

Darwin 的 Python 运行时可以提供目录 FD 路径和 `fcntl` 模块，却不提供 Linux
专属的 `F_SETLEASE`、`F_SETSIG` 和 `signal.sigtimedwait`。原平台分派只检查前两项，
因此 macOS Draft PUT 会进入 Linux file lease 实现；初始化失败后的 cleanup 又调用
不存在的 `sigtimedwait`，第二个 `AttributeError` 覆盖受控冲突并越过错误 envelope，
最终表现为没有响应体和 CORS header 的 HTTP 500。

本轮只修复工作流创作草稿（Authoring Draft）的跨平台能力选择和相同字节保存，
不改变 Draft PUT wire model、候选工作流（Candidate Workflow）、已应用工作流图
（Applied Workflow Graph）、工作流修订（Workflow Revision）或工作流任务
（WorkflowTask）语义。冻结不变量如下：

1. Draft PUT 仍同时校验调用方观察到的 Draft hash 与 Workflow revision；
2. 外部 coding-agent、Git 或编辑器的字节不得被静默覆盖；
3. registered package 文件仍是唯一 Draft 权威；
4. Apply 仍只提交已物化 Candidate，不在 SQLite 事务中写回 Draft；
5. 不具备强 CAS 原语的平台必须在任何 Linux lease/signal 副作用前失败关闭。

## 2. 独立 RED 测试

唯一 test-author：`/root/test_author_02g4`（Euclid）。

独立测试分支：`test/02g4-darwin-draft-cas`。

原始测试提交：`78ea5ba1`，以非 squash 提交 `7b00272` 带入本分支；同一测试作者
修正 credentialed CORS 断言并补 missing Draft 基线控制的提交为 `42f89397`，以
非 squash 提交 `595a304` 带入本分支。

RED 命令：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q \
  tests/workflow/test_02g4_darwin_draft_cas_contract.py
```

初始结果为 `2 failed`；补齐基线控制后为 `1 passed, 2 failed`。失败通过真实
FastAPI `PUT /api/v1/workflows/{uuid}/authoring/draft` 证明：

- 相同源码本可直接确认并编译，却因 Linux cleanup 异常返回空 500；
- 不同源码在缺少强 CAS 时本应返回受控 409，却同样返回空 500；
- 两条失败都错误调用了 Linux lease/signal 接缝；
- 缺失 Draft 的 `expected_draft_hash=null` 仍可用 exclusive link 安全创建。

## 3. 实现结论

平台选择现在检查完整 Linux 强 CAS 能力：Linux 平台、目录 FD、`fcntl` 的 lease
常量，以及同步 signal mask/drain 原语必须同时存在。Darwin 不再进入部分初始化的
Linux 实现。

`WorkflowService.save_draft()` 在请求字节已经等于当前 Draft hash 时不替换权威
文件，但会再次读取、校验并编译，因而“接受并保存规范化源码”可以安全完成并返回
自洽 Candidate。若读取窗口内发生外部变化，最终 hash 复核仍返回冲突。

缺失 Draft 继续使用同目录 exclusive link 作为可证明的原子创建；已有 Draft 需要
替换而平台没有 Linux/Windows 强 CAS Adapter 时，返回带生产 CORS 和统一错误
envelope 的 `draft_hash_conflict`，并保留原始权威字节。该行为不提供不受保护的
覆盖或 force-save 接口。

## 4. 验证趋势

| 门禁 | 结果 |
| --- | --- |
| Darwin、Windows 与 Linux lease/Authoring 风险合同 | `119 passed, 3 skipped` |
| 变更 Python 文件 Ruff `E,F,I` | passed |
| 变更 Python 文件 `ruff format --check` | passed |
| `compileall` 与 `git diff --check` | passed |

完整 `tests/workflow`、正式 `tests/`、精确候选 SHA 和 reviewer disposition 在本轮
完成后追加到 Git note，避免提交内文件伪造自己的未来 SHA。

## 5. 文件规模与模块边界

- `service.py` 是既有 3188 行工作流应用服务。本轮只在现有平台分派 seam 增加
  精确能力探测，并在现有保存流程增加同字节 no-op；没有把新的文件 CAS 算法继续
  内联到超大 Service。
- 独立 Darwin HTTP 合同测试为 366 行，保持在 500 行预算内。
- 若后续实现 Darwin 原生强 CAS，应把原子交换、冲突恢复和 artifact 生命周期放入
  独立 `darwin_draft_cas.py` 深模块，`service.py` 只保留 Adapter 选择与领域错误映射；
  本轮不把事故修复与新的原生文件算法混为一次变更。
