# Round 02G3：Windows CRLF Draft 读取与 CAS 一致性趋势记录

状态：**实现候选；精确测试 SHA、完整门禁和独立 reviewer 最终 disposition
记录在候选提交的 Git note。**

基线：`33e79963`（`integration/workflow-task-runtime`）

分支：`migration/02g3-windows-crlf-draft-read`

## 1. 问题与边界

02G2 已实现 Windows Draft CAS 写入，但无相对目录 FD 平台的
`WorkflowService._read_source_by_path()` 没有显式请求 `os.O_BINARY`。Windows CRT
文本模式会把磁盘 CRLF 字节转换为 LF，导致 Authoring GET 返回的 `draft_hash` 与
Windows Draft PUT CAS 对原始字节计算的 hash 不同；保存规范化工作流源码
（Workflow Source）时因此稳定返回 `draft_hash_conflict`，后续 Apply 不会发生。

本轮只统一 Windows Draft 读取与 CAS 的字节观察，不改变候选工作流
（Candidate Workflow）、已应用工作流图（Applied Workflow Graph）、工作流修订
（Workflow Revision）或工作流任务（WorkflowTask）语义。POSIX 上
`getattr(os, "O_BINARY", 0)` 为零，既有 `dir_fd` 与文件租约路径保持不变。

## 2. 独立 RED 测试

唯一 test-author：`/root/windows_crlf_test_author`。

独立测试分支：`test/02g3-windows-crlf-draft-read-red`。

原始测试提交：`0af98261`；以非 squash 提交 `463d901c` 带入本分支。

RED 命令：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest \
  tests/workflow/test_02g2_windows_draft_cas_contract.py::test_windows_crlf_draft_read_uses_binary_mode_and_hash_saves \
  -q
```

结果连续两次为 `1 failed`，失败点证明 `_read_source_by_path()` 的 `os.open`
flags 未包含合成 `O_BINARY`。测试通过真实 `WorkflowService.get_authoring()` 与
`save_draft()` 链路验证：

1. Authoring GET 保留磁盘原始 `b"seed()\r\n"`；
2. `draft_hash` 是原始 CRLF 字节的 SHA-256；
3. GET 返回的 hash 可直接作为 Draft PUT CAS 令牌；
4. 保存后 Draft 字节和候选 hash 保持一致。

Linux fixture 注入一个合成二进制 flag，记录生产调用 flags，并在调用真实
`os.open` 前剥离该 flag，因此回归合同不依赖测试机原生提供 `O_BINARY`。

## 3. 实现结论

无相对目录 FD 的 Draft 读取 flags 增加：

```python
getattr(os, "O_BINARY", 0)
```

Authoring GET、Draft PUT CAS 与 Apply 现在都基于同一份原始 UTF-8 字节。
回归用例转为 `1 passed`；Windows Draft CAS 与持久 Authoring 合同合计
`16 passed`，Windows 文件边界相关集合为 `69 passed, 3 skipped`，其中三项仅在
原生 Windows 执行。

## 4. 文件规模与模块边界

- `service.py` 是既有超大工作流应用服务。本轮只在现有跨平台文件读取 seam 增加
  一个 flag，并补全该函数的中文责任、参数、返回值、异常与字节一致性说明；将
  单行平台 flag 抽成新模块会扩大接口而不增加安全性。
- `test_02g2_windows_draft_cas_contract.py` 为 463 行，继续复用同一 Windows CAS
  harness，未超过 500 行预算。
- 本趋势记录不包含运行时数据库、日志、缓存或实验室工作流源码。
