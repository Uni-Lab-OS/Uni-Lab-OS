"""Phase 01A4 Draft 监视器停机与 Authority 租约公共合同。"""

from __future__ import annotations

import multiprocessing
import threading
from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow.composition import (
    compose_workflow_runtime,
    get_workflow_service,
    reset_workflow_service_for_test,
)
from unilabos.workflow.service import WorkflowError

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
CATALOG_FINGERPRINT = f"sha256:{'c' * 64}"


class BlockingCompiler:
    """在真实 monitor 编译边界阻塞，释放后以普通编译错误结束。"""

    compiler_version = "phase-01a4-monitor-shutdown-v1"
    template_catalog_fingerprint = CATALOG_FINGERPRINT

    def __init__(self) -> None:
        self.compile_entered = threading.Event()
        self.release_compile = threading.Event()
        self.compile_released = threading.Event()

    def compile(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        python_source: str,
        source_uri: str,
        applied_graph: dict[str, Any],
    ) -> None:
        del (
            workflow_uuid,
            workflow_revision,
            python_source,
            source_uri,
            applied_graph,
        )
        self.compile_entered.set()
        try:
            if not self.release_compile.wait(timeout=15):
                raise TimeoutError("测试未释放 Draft 编译器")
        finally:
            self.compile_released.set()
        raise WorkflowError("invalid_input")


def _try_compose_in_second_process(
    working_dir: str,
    outcome: Any,
) -> None:
    """在全新解释器中通过公开组合根尝试取得同一工作区。"""

    try:
        compose_workflow_runtime(working_dir)
    except RuntimeError as error:
        outcome.put(("rejected", str(error)))
    except Exception as error:  # noqa: BLE001 - 必须把子进程异常回传父进程
        outcome.put(("unexpected_error", type(error).__name__, str(error)))
    else:
        outcome.put(("opened", ""))
    finally:
        reset_workflow_service_for_test()


def _second_process_result(working_dir: Path) -> tuple[str, ...]:
    context = multiprocessing.get_context("spawn")
    outcome = context.Queue()
    process = context.Process(
        target=_try_compose_in_second_process,
        args=(str(working_dir), outcome),
    )
    process.start()
    process.join(timeout=8)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        pytest.fail("第二个 OS Workflow Authority 进程未在限定时间内结束")
    result = outcome.get(timeout=2)
    outcome.close()
    outcome.join_thread()
    return result


def _release_compiler_and_reset(compiler: BlockingCompiler) -> None:
    """失败断言也必须释放 compiler，并通过公开 reset 完成清理。"""

    compiler.release_compile.set()
    if compiler.compile_entered.is_set():
        compiler.compile_released.wait(timeout=2)
    reset_workflow_service_for_test()


def test_monitor_停止超时保留原_service_和工作区租约直到可重试(
    tmp_path: Path,
) -> None:
    working_dir = tmp_path / "unilabos_data"
    package_root = tmp_path / "package"
    draft_path = package_root / "workflows" / "demo.py"
    compiler = BlockingCompiler()
    retained_service = False
    reopened_after_retry = False
    second_process: tuple[str, ...] = ("not_started",)

    reset_workflow_service_for_test()
    service = compose_workflow_runtime(working_dir, compiler=compiler)
    try:
        service.create_workflow(
            name="Phase 01A4 monitor 停机合同",
            tags=[],
            description=None,
            meta_data={},
            workflow_uuid=WORKFLOW_UUID,
        )
        package_root.mkdir()
        service.register_editable_source(
            workflow_uuid=WORKFLOW_UUID,
            package_id="phase_01a4_monitor_contract",
            package_root=package_root,
            relative_path="workflows/demo.py",
        )
        draft_path.parent.mkdir()
        draft_path.write_text(
            "result = externally_edited()\n",
            encoding="utf-8",
        )
        assert compiler.compile_entered.wait(timeout=3), (
            "真实 source monitor 未进入外部 Draft 重编译"
        )

        with pytest.raises(RuntimeError, match="Workflow Draft 监视器未能停止"):
            reset_workflow_service_for_test()

        retained_service = get_workflow_service() is service
        second_process = _second_process_result(working_dir)

        compiler.release_compile.set()
        assert compiler.compile_released.wait(timeout=2)
        reset_workflow_service_for_test()
        replacement = compose_workflow_runtime(working_dir, compiler=compiler)
        reopened_after_retry = (
            replacement is get_workflow_service() and replacement is not service
        )
    finally:
        compiler.release_compile.set()
        if compiler.compile_entered.is_set():
            compiler.compile_released.wait(timeout=2)
        try:
            _release_compiler_and_reset(compiler)
        except RuntimeError:
            _release_compiler_and_reset(compiler)

    assert (retained_service, second_process) == (
        True,
        ("rejected", "当前工作区已由另一个 OS Workflow Authority 占用"),
    )
    assert reopened_after_retry
