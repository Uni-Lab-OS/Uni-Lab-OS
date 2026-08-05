from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from unilabos.device_mesh.moveit_runtime import MoveItRuntime


class _FakeMoveItRuntime(MoveItRuntime):
    def _create_launch_description(self, runtime_root):
        del runtime_root
        return object()


def test_immediate_launch_service_failure_is_reported_before_start_returns(
    monkeypatch,
    tmp_path,
) -> None:
    class FailingLaunchService:
        def __init__(self, *, noninteractive: bool) -> None:
            assert noninteractive is True

        def include_launch_description(self, description) -> None:
            del description

        def run(self):
            raise RuntimeError("launch failed immediately")

        def shutdown(self) -> None:
            pass

    monkeypatch.setitem(
        sys.modules,
        "launch",
        SimpleNamespace(LaunchService=FailingLaunchService),
    )
    monkeypatch.setenv("AMENT_PREFIX_PATH", "/test/ros")
    runtime = _FakeMoveItRuntime(
        {},
        moveit_device_ids=("robot",),
        package_sources=(),
        package_catalogs=(),
        runtime_parent=tmp_path,
    )

    with pytest.raises(RuntimeError, match="启动失败"):
        runtime.start(timeout=1.0)
    assert runtime.healthy is False
    assert list(tmp_path.iterdir()) == []
