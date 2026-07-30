"""Process composition for the workspace-local Workflow authority."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from unilabos.workflow.service import AuthoringCompiler, WorkflowService
from unilabos.workflow.store import WorkflowStore

_lock = threading.Lock()
_service: Optional[WorkflowService] = None
_database_path: Optional[Path] = None


def setup_workflow_service(
    working_dir: str | Path,
    *,
    compiler: Optional[AuthoringCompiler] = None,
) -> WorkflowService:
    """Create the one process-wide authority rooted at ``working_dir``."""

    global _database_path, _service
    database_path = Path(working_dir).resolve() / "workflow.db"
    with _lock:
        if _service is not None:
            if database_path != _database_path:
                raise RuntimeError(
                    "Workflow authority cannot switch working_dir at runtime"
                )
            return _service
        _service = WorkflowService(
            WorkflowStore(database_path),
            compiler=compiler,
        )
        _database_path = database_path
        return _service


def get_workflow_service() -> Optional[WorkflowService]:
    return _service


def reset_workflow_service_for_test() -> None:
    """Close and forget the singleton; intended only for isolated tests."""

    global _database_path, _service
    with _lock:
        if _service is not None:
            _service.store.close()
        _service = None
        _database_path = None


__all__ = [
    "get_workflow_service",
    "reset_workflow_service_for_test",
    "setup_workflow_service",
]
