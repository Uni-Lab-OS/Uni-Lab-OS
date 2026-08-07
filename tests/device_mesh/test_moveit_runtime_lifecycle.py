from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from unilabos.device_mesh.moveit_runtime import MoveItRuntime


class _FakeMoveItRuntime(MoveItRuntime):
    def _materialize_launch_bundle(self, runtime_root: Path) -> None:
        (runtime_root / "launch_bundle.yaml").write_text(
            "enable_rviz: false\n",
            encoding="utf-8",
        )


def test_immediate_launch_service_failure_is_reported_before_start_returns(
    monkeypatch,
    tmp_path,
) -> None:
    created: list[SimpleNamespace] = []

    class _Context:
        def Process(self, *args, **kwargs):
            runtime_root = Path(kwargs["args"][0])
            proc = SimpleNamespace(exitcode=None, _alive=False)

            def start() -> None:
                (runtime_root / ".launch_error").write_text(
                    "launch failed immediately",
                    encoding="utf-8",
                )
                proc.exitcode = 1
                proc._alive = False

            def is_alive() -> bool:
                return bool(proc._alive)

            def join(timeout: float | None = None) -> None:
                del timeout

            def terminate() -> None:
                proc._alive = False

            def kill() -> None:
                proc._alive = False

            proc.start = start
            proc.is_alive = is_alive
            proc.join = join
            proc.terminate = terminate
            proc.kill = kill
            created.append(proc)
            return proc

    monkeypatch.setattr(
        "multiprocessing.get_context",
        lambda *_a, **_k: _Context(),
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
    assert created
