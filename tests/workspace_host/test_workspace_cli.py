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


def test_workspace_restart_uses_the_shared_client_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls: list[tuple[str, object]] = []

    class Client:
        def execute(self, command: str, **options: object) -> dict[str, object]:
            calls.append((command, options))
            return {"operationId": "cli-restart", "phase": "succeeded"}

    monkeypatch.setattr(
        "unilabos.workspace_host.cli.ensure_workspace_host",
        lambda _workspace: Client(),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "unilab",
            "workspace",
            "restart",
            "--workspace",
            str(workspace),
            "--component",
            "os",
            "--runtime-mode",
            "normal",
            "--operation-id",
            "cli-restart",
            "--wait",
            "9",
            "--json",
        ],
    )

    main()

    assert json.loads(capsys.readouterr().out)["phase"] == "succeeded"
    assert calls == [
        (
            "os.restart",
            {
                "parameters": {"runtimeMode": "normal"},
                "operation_id": "cli-restart",
                "timeout": 9.0,
            },
        )
    ]
