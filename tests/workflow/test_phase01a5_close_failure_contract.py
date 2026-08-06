"""Phase 01A5 Service 关闭失败与 Authority 租约公共合同。"""

from __future__ import annotations

import multiprocessing
from pathlib import Path
from typing import Any

import pytest

from unilabos.app.scheduler.inventory import InventoryService
from unilabos.workflow.composition import (
    compose_workflow_runtime,
    get_workflow_service,
    reset_workflow_service_for_test,
)
from unilabos.workflow.store import WorkflowStore

CLOSE_FAILURE = "Phase 01A5 注入的 Service.close 失败"
STARTUP_FAILURE = "M1R 注入的 InventoryService.open 启动失败"
STORE_CLOSE_FAILURE = "M1R 注入的 pre-Service Store.close 失败"
LEASE_REJECTION = "当前工作区已由另一个 OS Workflow Authority 占用"


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


def test_service关闭失败保留原service和租约直到重试成功(
    tmp_path: Path,
) -> None:
    working_dir = tmp_path / "unilabos_data"
    close_calls = 0
    real_close_succeeded = False
    first_error: tuple[str, str] | None = None
    retained_service = False
    second_process: tuple[str, ...] = ("not_started",)
    reopened_after_retry = False

    reset_workflow_service_for_test()
    service = compose_workflow_runtime(working_dir)
    real_close = service.close

    def fail_once_close() -> None:
        nonlocal close_calls, real_close_succeeded
        close_calls += 1
        if close_calls == 1:
            raise RuntimeError(CLOSE_FAILURE)
        real_close()
        real_close_succeeded = True

    service.close = fail_once_close
    try:
        try:
            reset_workflow_service_for_test()
        except RuntimeError as error:
            first_error = (type(error).__name__, str(error))

        retained_service = get_workflow_service() is service
        second_process = _second_process_result(working_dir)

        reset_workflow_service_for_test()
        replacement = compose_workflow_runtime(working_dir)
        reopened_after_retry = (
            replacement is get_workflow_service() and replacement is not service
        )
    finally:
        try:
            reset_workflow_service_for_test()
        finally:
            service.close = real_close
            if not real_close_succeeded:
                real_close()

    assert (
        first_error,
        retained_service,
        second_process,
        close_calls,
        reopened_after_retry,
    ) == (
        ("RuntimeError", CLOSE_FAILURE),
        True,
        ("rejected", LEASE_REJECTION),
        2,
        True,
    )


def test_service构造前store关闭失败保留租约直到reset重试(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    working_dir = tmp_path / "unilabos_data"
    opened_stores: list[WorkflowStore] = []
    real_store_close = WorkflowStore.close
    first_error: tuple[str, str, str, str] | None = None
    second_process: tuple[str, ...] = ("not_started",)
    reopened_after_retry = False

    def fail_inventory_service_startup(
        cls: type[InventoryService],
        **kwargs: Any,
    ) -> None:
        del cls, kwargs
        raise RuntimeError(STARTUP_FAILURE)

    def fail_store_close(store: WorkflowStore) -> None:
        opened_stores.append(store)
        raise RuntimeError(STORE_CLOSE_FAILURE)

    reset_workflow_service_for_test()
    try:
        with monkeypatch.context() as startup_faults:
            startup_faults.setattr(
                InventoryService,
                "open",
                classmethod(fail_inventory_service_startup),
            )
            startup_faults.setattr(WorkflowStore, "close", fail_store_close)

            try:
                compose_workflow_runtime(working_dir)
            except RuntimeError as error:
                cause = error.__cause__
                first_error = (
                    type(error).__name__,
                    str(error),
                    type(cause).__name__ if cause is not None else "",
                    str(cause) if cause is not None else "",
                )

            assert get_workflow_service() is None
            second_process = _second_process_result(working_dir)

        reset_workflow_service_for_test()
        replacement = compose_workflow_runtime(working_dir)
        reopened_after_retry = replacement is get_workflow_service()
    finally:
        reset_workflow_service_for_test()
        for store in opened_stores:
            real_store_close(store)

    assert second_process == ("rejected", LEASE_REJECTION)
    assert first_error == (
        "RuntimeError",
        STARTUP_FAILURE,
        "RuntimeError",
        STORE_CLOSE_FAILURE,
    )
    assert reopened_after_retry
