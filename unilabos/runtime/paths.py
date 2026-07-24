"""Shared local-runtime filesystem locations.

The bridge may project the OS journal, but it must never invent a second
default database path.  An explicit environment override keeps desktop and
developer harnesses deterministic.
"""

from __future__ import annotations

import os
from pathlib import Path


RUNTIME_DB_ENV = "UNILABOS_RUNTIME_DB"


def default_runtime_db_path() -> Path:
    configured = os.environ.get(RUNTIME_DB_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home() / ".unilabos" / "runtime.sqlite"
