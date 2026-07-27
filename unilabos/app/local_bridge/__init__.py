"""本地工作流桥（local_bridge）— OS 执行权威与统一前端之间的薄传输层。

桥仅保留两面：
- schedule_ws.py：OS 面 WS（/api/v1/ws/schedule），OS 主动连入；
- local_api.py：统一前端 HTTP/WS v1（:8014）。

旧 Cloud panel ``/ws/workflow/{uuid}`` 已删除。桥不复制执行或物料权威：
完整 TaskDag 交 OS 执行，Material API 只投影 OS 当前内存 ResourceTreeSet 快照。

契约见 docs/features/F003-local-workflow-bridge/interface-design.md。
"""

from __future__ import annotations
