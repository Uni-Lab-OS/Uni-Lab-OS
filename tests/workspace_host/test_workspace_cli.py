"""The public `unilab workspace` command bypasses product runtime composition."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from unilabos.app.main import main


def test_workspace_status_returns_stable_offline_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "unilab",
            "workspace",
            "status",
            "--workspace",
            str(workspace),
            "--json",
        ],
    )
    main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["schemaVersion"] == "unilab-workspace-host/v1"
    assert payload["workspacePath"] == str(workspace)
    assert payload["host"]["phase"] == "offline"
    assert set(payload["components"]) == {"backend", "edge", "plc", "renderer"}
