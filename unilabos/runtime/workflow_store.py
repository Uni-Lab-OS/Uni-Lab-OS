"""Atomic local persistence for editable Canonical workflow revisions."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from unilabos.workflow.canonical import WorkflowRevision, revalidate_workflow_revision


WORKFLOW_DIR_ENV = "UNILABOS_WORKFLOW_DIR"


def default_workflow_dir() -> Path:
    configured = os.environ.get(WORKFLOW_DIR_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home() / ".unilabos" / "workflows"


class WorkflowRevisionConflict(RuntimeError):
    """Optimistic revision token does not match the stored document."""


class WorkflowDocumentStore:
    """Store one lossless Canonical revision per workflow id."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_workflow_dir()
        self.root.mkdir(parents=True, exist_ok=True)

    def load(self, workflow_id: str) -> WorkflowRevision | None:
        path = self._path(workflow_id)
        if not path.exists():
            return None
        return WorkflowRevision.model_validate(json.loads(path.read_text("utf-8")))

    def save(
        self,
        revision: WorkflowRevision,
        *,
        expected_revision_id: str | None = None,
    ) -> WorkflowRevision:
        revision = revalidate_workflow_revision(revision)
        current = self.load(revision.workflow_id)
        if (
            expected_revision_id is not None
            and (current is None or current.revision_id != expected_revision_id)
        ):
            actual = current.revision_id if current is not None else "-"
            raise WorkflowRevisionConflict(
                "WORKFLOW_REVISION_CONFLICT: "
                f"expected {expected_revision_id}, actual {actual}"
            )
        path = self._path(revision.workflow_id)
        payload: dict[str, Any] = revision.model_dump(mode="json")
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=self.root,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return revision

    def _path(self, workflow_id: str) -> Path:
        if not workflow_id or any(
            token in workflow_id for token in ("/", "\\", "\x00", "..")
        ):
            raise ValueError("INVALID_WORKFLOW_ID")
        return self.root / f"{workflow_id}.json"
