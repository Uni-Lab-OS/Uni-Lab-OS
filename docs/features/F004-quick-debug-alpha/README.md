# F004 — Quick Debug Alpha

本目录记录本地文件优先的工作流编写、统一 Runtime、DAG 调试与 pTLC Alpha 验收。
当前 `progress.md` 记录为 **5/6，最终门禁中**；AC-8 文件原子保存/索引收口等未完成项
不得被描述为已交付。

## 阅读顺序

1. [`requirement.md`](requirement.md)：W1–W2 Alpha 需求与验收标准。
2. [`interface-design.md`](interface-design.md)：Canonical、Runtime 和 bridge 契约。
3. [`feature-list.json`](feature-list.json)：任务与状态。
4. [`checklist.md`](checklist.md)：验收清单。
5. [`progress.md`](progress.md)：实现证据、限制和下一步。
6. [`w1-w2-software-gate-report.md`](w1-w2-software-gate-report.md)：软件门禁报告。

## 核心边界

- 复用现有 `TaskDag`、`DagExecutor`、resource lock、journal 和 debugger，不建前端专用
  scheduler。
- 工作流源以规范化本地文件/Git 为权威；SQLite 是索引、draft 和 runtime journal。
- start node、breakpoint 和 step 不裁剪 Canonical/DAG。
- HTTP/WS ack 不是设备终态；恢复不得重放 unknown physical effect。
- pTLC 只通过通用 Action/Profile/Workflow 接入，OS core 不增加 pTLC 专用 API。
- offline 只替代传输/进程边界，不降低真实模式的状态与资源规则。

相关代码导航分别见 `unilabos/workflow/README.md`、`unilabos/runtime/README.md`、
`unilabos/scheduler/README.md` 和 `unilabos/app/local_bridge/README.md`。
