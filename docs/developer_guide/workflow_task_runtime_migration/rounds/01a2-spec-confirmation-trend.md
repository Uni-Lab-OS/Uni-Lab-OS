# Phase 01A2：Spec 修复确认趋势与策略报告

状态：**原 blocking 已关闭；新增 1 个非阻塞测试覆盖缺口，候选暂不合并。**

评审分支：`review/01a2-spec-confirmation`

固定比较点：`d20a929511d52da5153193b65417e5965b2a3e25`

被审实现 SHA：`de5df0461b14f590cd923bbfaeaa62f4aa72ab15`

唯一 reviewer：`/root/01a_contract_reviewer`

Backend 保持只读。本轮没有修改 Production、Tests、Backend 或前端，也没有开展
FE–OS 联调。

## 1. Spec 结论

### 原 blocking：已关闭

- `WorkflowStore.transaction` 先成功取得 `BEGIN IMMEDIATE`；
- Store 在事务内复核持久 Candidate 与 Workflow revision；
- 随后调用 Service 的最终 Draft/Catalog 校验；
- graph/source/event mutation 均发生在该校验之后；
- 校验异常由事务上下文回滚；
- 独立合同证明竞争期外部替换返回 409、数据库状态不变且外部文件未被覆盖。

对应位置：

- `unilabos/workflow/store.py:306-317,1179-1214`；
- `unilabos/workflow/service.py:1052-1075`；
- `tests/workflow/test_phase01a2_draft_linearization_contract.py:222-272`。

结论符合主规格 §4 的事务内线性化顺序。

### 原 non-blocking：已关闭

`tests/workflow/test_phase01a2_draft_linearization_contract.py:275-311` 已分别冻结：

- 同进程同目录复用同一 Authority Service；
- 运行中的 Authority 拒绝切换工作区；
- reset 释放租约后可以重新装配。

### 新 non-blocking：线性化后编辑缺少公共合同

当前尚无一个公共测试把以下完整行为固定在同一场景：

1. Candidate Apply 成功；
2. 线性化点之后出现新的外部 Draft 编辑；
3. OS 不覆盖该文件；
4. Authoring aggregate 保留 Applied Source，同时把它投影为 stale。

reviewer 检查 `unilabos/workflow/service.py:1107-1129,2471-2478` 并用临时探针确认
当前实现行为正确。因此这是覆盖缺口，不是已证实的 Production 错误。

reviewer 未发现 scope creep。

## 2. 本轮趋势

| 阶段 | Blocking | Non-blocking |
|---|---:|---:|
| 第一轮 Spec 评审 | 1 | 1 |
| Phase 01A2 实现门禁 | 0 | 0 |
| Spec 修复确认 | 0 | 1 |

Blocking 已从 1 降到 0。新增问题没有扩大 Production 行为面，只是 reviewer 对
新线性化语义提出了一个公共回归合同缺口。问题趋势仍在收敛，但测试证据还没有完全
封闭。

本轮是只读评审轮：

| 范围 | 变动文件 | 新增行 | 删除行 |
|---|---:|---:|---:|
| Production | 0 | 0 | 0 |
| Tests | 0 | 0 | 0 |

被审精确 SHA 已通过 `837 passed, 3 skipped`；目标合同为 `4 passed`。

## 3. 下一轮策略

1. 新开测试补强分支，只启用一个独立 test-author subagent；
2. 通过 HTTP Apply、实际 Draft 文件和 Authoring GET/SSE 公共 seam，冻结
   “Apply 成功后的新外部编辑被保留并投影 stale”；
3. 该行为当前预计为 PASS，因此这是评审覆盖补强，不伪造 RED，也不修改
   Production；
4. 运行完整测试和静态门禁后生成新精确候选；
5. 之后再顺序执行模块设计、回归/安全 reviewer，每轮一个 subagent；
6. 所有评审与门禁结束前不合并、不 push。

## 4. 前端覆盖

本轮没有覆盖前端。前端独立分支与 FE–OS 联调继续等待 OS 候选完成顺序评审。
Backend 不得修改。
