# Phase 01A：模块设计与 Standards 评审趋势报告

状态：**发现 1 个 blocking、2 个 non-blocking，禁止合并。**

评审分支：`review/01a-module-design`

固定比较点：`2a394737ec7a36f8710e0af27472953451c308bc`

被审精确候选：`0b36da2b3f098032f8c78576bd8e5818c199a675`

唯一 reviewer：`/root/01a_module_design_reviewer`

本轮没有修改 Production、Tests、Backend 或前端。

## 1. Standards 结论

### Blocking：monitor 未退出仍释放 Authority 租约

位置：

- `unilabos/workflow/composition.py:141-158`；
- `unilabos/workflow/source_monitor.py:44-50`。

`WorkflowSourceMonitor.stop()` 设置 stop event 后只执行有超时的 `join`，没有报告线程
是否真正退出。`reset_workflow_service_for_test()` 的 `finally` 随后仍关闭 Store、
清空 runtime 并释放工作区租约。

如果 monitor 在超时后仍存活：

- 旧线程可能继续使用已关闭 Store；
- 工作区租约已经释放；
- 第二 Authority 可以打开同一工作区；
- 进程内同时出现旧 monitor 与新 Authority。

这违反 AGENTS 的“一个 working_dir 只有一个 OS Workflow Authority”以及 reset 必须
先停止 monitor、再关闭 Service、最后释放租约的顺序。

最小建议：stop 未确认线程退出时明确失败并保留 runtime/Service/租约；只有确认退出
后才关闭 Store 和释放租约，并增加失败路径公共测试。

### Non-blocking：事务内 callback 与工作量过宽

位置：

- `unilabos/workflow/store.py:1171-1212`；
- `unilabos/workflow/service.py:1052-1074,1178-1221`。

当前 Store 接收通用 `Callable`，并在 SQLite writer transaction 内调用。实现只有
一个 adapter，且 callback 会完整读取没有显式大小上限的 Draft，并读取 compiler
Catalog property。判断项为 possible Speculative Generality 与锁内不受控工作。

后续应评估：

- 将参数命名收窄为 Apply 线性化语义；
- 明确 callback 禁止 Store 重入；
- 最终 Draft 校验是否可以按 Candidate 已知字节长度做有界读取；
- 避免把新的通用 port 暴露为公共 Interface。

### Non-blocking：系统竞态测试绑定 `/proc/wchan`

位置：

- `tests/workflow/test_phase01a2_draft_linearization_contract.py:233-252`。

测试通过 `/proc/self/task/<tid>/wchan == hrtimer_nanosleep` 判断 SQLite busy wait，
绑定 Linux 内核与当前 SQLite busy handler 实现。产品本身当前依赖 Linux `fcntl`，
因此不是立即 blocker，但测试可维护性需要回归/安全 reviewer 再判断。

## 2. 通过项

reviewer 确认：

- 单 token strict HTTP interface 小而深；
- composition 的租约、fork 与大部分异常清理集中在组合根；
- source monitor 已无废止 writeback 支路；
- 中文注释/日志、类型和命名符合仓库规则；
- 未发现公共 DTO scope creep。

## 3. 趋势

| 阶段 | Blocking | Non-blocking |
|---|---:|---:|
| Phase 01A3 完整门禁 | 0 | 0 |
| 模块设计独立评审 | 1 | 2 |

问题数暂时回升，但新 blocking 位于一个具体的异常停机路径，不是 Apply、Candidate、
writeback 或多 Authority Store 初始化重新扩张。两个 non-blocking 均是内部 seam
和测试可维护性判断项。

本轮为只读评审：

| 范围 | 变动文件 | 新增行 | 删除行 |
|---|---:|---:|---:|
| Production | 0 | 0 | 0 |
| Tests | 0 | 0 | 0 |

被审候选已通过 `838 passed, 3 skipped`，但测试通过不能覆盖未被制造的 monitor
停止超时路径，因此不构成合并证据。

## 4. 下一轮策略

1. 新开 monitor 停机修正分支；
2. 只启用一个独立 test-author subagent，通过公开 composition/reset seam 制造 monitor
   未退出；
3. 合同必须证明 stop 失败时 Service 和租约仍保留，第二 Authority 仍被拒绝；
4. 实现只在确认 monitor 退出后 close/release，失败路径允许重试；
5. 目标与完整门禁全绿后，由本 reviewer 在后续独立 round 确认 blocking；
6. 再以单独 round 处理或书面 disposition 两个 non-blocking；
7. 最后执行回归/事务/安全评审。

## 5. 前端覆盖

本轮没有前端变更或 FE–OS 联调。Backend 未修改。
