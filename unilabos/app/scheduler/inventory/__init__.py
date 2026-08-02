"""Edge 本地仓储/物料库（唯一事实源）.

分层：
- domain    —— 状态机、不变量、领域错误（零外部依赖）
- store     —— SQLite WAL 持久化 + 事务 API
- service   —— 业务写操作（业务行 + ledger + outbox 同事务提交）
- sync      —— outbox worker（批量上报云端、ACK cursor、snapshot）
- commands  —— 云端 command-to-edge 幂等执行入口
- api       —— 本地 FastAPI 路由（薄层）
"""

from unilabos.app.scheduler.inventory.commands import execute_command
from unilabos.app.scheduler.inventory.domain import (
    InvariantViolation,
    InventoryError,
    InventoryEvent,
    MaterialAuthorityUnavailable,
    MaterialConflict,
    MaterialError,
    MaterialInvalidInput,
    MaterialNotFound,
    MaterialRecord,
    ResourceSlotResolution,
    ResourceTemplateIdentity,
    SiteRecord,
    TaskMaterialAdmissionCommand,
    TaskMaterialAdmissionResult,
    TaskMaterialAdmissionSource,
    TaskMaterialBinding,
    TaskMaterialReleaseCommand,
    TaskMaterialReleaseResult,
)
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.sync import OutboxWorker, build_snapshot

__all__ = [
    "InvariantViolation",
    "InventoryError",
    "InventoryEvent",
    "InventoryService",
    "MaterialAuthorityUnavailable",
    "MaterialConflict",
    "MaterialError",
    "MaterialInvalidInput",
    "MaterialNotFound",
    "MaterialRecord",
    "OutboxWorker",
    "ResourceSlotResolution",
    "ResourceTemplateIdentity",
    "SiteRecord",
    "TaskMaterialAdmissionCommand",
    "TaskMaterialAdmissionResult",
    "TaskMaterialAdmissionSource",
    "TaskMaterialBinding",
    "TaskMaterialReleaseCommand",
    "TaskMaterialReleaseResult",
    "build_snapshot",
    "execute_command",
]
