"""Round 02F 独立评审 S/P-B01~B03 的安全与 lifecycle 回归合同。"""

from __future__ import annotations

import ctypes
import hashlib
import multiprocessing
import os
import threading
from pathlib import Path
from queue import Empty
from typing import Any, ClassVar
from uuid import UUID

import pytest

from unilabos.workflow import composition, source_discovery
from unilabos.workflow.models import CandidateCompilation
from unilabos.workflow.service import (
    WorkflowConflict,
    WorkflowError,
    WorkflowService,
)
from unilabos.workflow.source_discovery import (
    SourceDeclarationError,
    load_editable_package_manifest,
)
from unilabos.workflow.store import WorkflowStore

WORKFLOW_A_UUID = "11111111-1111-4111-8111-111111111111"
WORKFLOW_B_UUID = "22222222-2222-4222-8222-222222222222"
WORKFLOW_C_UUID = "33333333-3333-4333-8333-333333333333"
PACKAGE_A = "alpha_lab"
PACKAGE_B = "beta_lab"
MANIFEST_BYTE_LIMIT = 1 * 1024 * 1024
SOURCE_BYTE_LIMIT = 8 * 1024 * 1024
YAML_DEPTH_LIMIT = 32
WORKFLOW_ENTRY_LIMIT = 1024
YAML_SCALAR_BYTE_LIMIT = 1 * 1024 * 1024
LEASE_REJECTION = "当前工作区已由另一个 OS Workflow Authority 占用"
START_FAILURE = "Round 02F 注入的 monitor start 主失败"
STOP_FAILURE = "Round 02F 注入的 monitor stop 清理失败"
RECOVERY_FAILURE = "Round 02F 注入的 startup recovery 主失败"
CLOSE_FAILURE = "Round 02F 注入的 Service.close 清理失败"


class SourceOnlyCompiler:
    compiler_version = "round-02f-cas-read-budget-v1"
    template_catalog_fingerprint = f"sha256:{'b' * 64}"

    def compile(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        python_source: str,
        source_uri: str,
        applied_graph: dict[str, Any],
    ) -> CandidateCompilation:
        del workflow_uuid, workflow_revision, source_uri
        return CandidateCompilation(
            diagnostics=[],
            graph=applied_graph,
            normalized_python_source=python_source,
            source_map=[],
            changeset={
                "kind": "source_only",
                "created_node_uuids": [],
                "updated_node_uuids": [],
                "deleted_node_uuids": [],
                "created_edge_uuids": [],
                "updated_edge_uuids": [],
                "deleted_edge_uuids": [],
                "reserved_metadata_changed": False,
            },
            compiler_version=self.compiler_version,
            template_catalog_fingerprint=self.template_catalog_fingerprint,
        )


@pytest.fixture(autouse=True)
def clean_composition() -> Any:
    composition.reset_workflow_service_for_test()
    try:
        yield
    finally:
        composition.reset_workflow_service_for_test()


def _manifest_text(
    *,
    package_id: str = PACKAGE_A,
    entries: tuple[tuple[str, str], ...] = (
        (WORKFLOW_A_UUID, f"{PACKAGE_A}/workflows/demo.py"),
    ),
) -> str:
    lines = ["package:", f"  name: {package_id}", "", "workflows:"]
    for workflow_uuid, source in entries:
        lines.extend(
            [
                f"  - workflow_uuid: {workflow_uuid}",
                f"    source: {source}",
            ]
        )
    return "\n".join(lines) + "\n"


def _write_manifest_package(
    selected_root: Path,
    *,
    package_id: str = PACKAGE_A,
    entries: tuple[tuple[str, str], ...] = (
        (WORKFLOW_A_UUID, f"{PACKAGE_A}/workflows/demo.py"),
    ),
    create_sources: bool = True,
) -> Path:
    selected_root.mkdir(parents=True)
    source_root = selected_root / package_id
    source_root.mkdir()
    (selected_root / "package.yaml").write_text(
        _manifest_text(package_id=package_id, entries=entries),
        encoding="utf-8",
    )
    if create_sources:
        for _workflow_uuid, source in entries:
            target = selected_root / source
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("result = build()\n", encoding="utf-8")
    return source_root


def _assert_declaration_error(
    root: Path,
    *,
    code: str,
    forbidden_content: str | None = None,
) -> SourceDeclarationError:
    with pytest.raises(SourceDeclarationError) as captured:
        load_editable_package_manifest(root)
    assert captured.value.code == code
    if forbidden_content is not None:
        assert forbidden_content not in str(captured.value)
    return captured.value


@pytest.mark.parametrize("replacement", ["symlink", "rename"])
def test_selected_root_identity_cannot_change_between_check_and_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    selected = tmp_path / "selected"
    outside = tmp_path / "outside"
    _write_manifest_package(selected)
    _write_manifest_package(
        outside,
        entries=((WORKFLOW_B_UUID, f"{PACKAGE_A}/workflows/outside.py"),),
    )
    saved_selected = tmp_path / "saved-selected"
    original_check = source_discovery._contains_symlink
    replaced = False

    def check_then_replace(path: Path) -> bool:
        nonlocal replaced
        result = original_check(path)
        if path == selected and not replaced:
            selected.rename(saved_selected)
            if replacement == "symlink":
                selected.symlink_to(outside, target_is_directory=True)
            else:
                outside.rename(selected)
            replaced = True
        return result

    monkeypatch.setattr(source_discovery, "_contains_symlink", check_then_replace)

    _assert_declaration_error(selected, code="invalid_package_root")


def _fifo_race_probe(selected_root: str, source_path: str, outcome: Any) -> None:
    """在 open 前把已通过检查的 regular source 换成 FIFO。"""

    target = Path(source_path)
    real_open = source_discovery.os.open
    swapped = False

    def swap_then_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal swapped
        if Path(path).name == target.name and not swapped:
            target.unlink()
            os.mkfifo(target)
            swapped = True
        return real_open(path, flags, *args, **kwargs)

    source_discovery.os.open = swap_then_open
    try:
        load_editable_package_manifest(selected_root)
    except SourceDeclarationError as error:
        outcome.put(("declaration_error", error.code, str(error)))
    except BaseException as error:  # noqa: BLE001 - 子进程必须回传异常类型
        outcome.put(("unexpected_error", type(error).__name__, str(error)))
    else:
        outcome.put(("returned",))


def test_regular_to_fifo_race_fails_closed_without_blocking(tmp_path: Path) -> None:
    selected = tmp_path / "selected"
    _write_manifest_package(selected)
    source = selected / PACKAGE_A / "workflows" / "demo.py"
    context = multiprocessing.get_context("spawn")
    outcome = context.Queue()
    process = context.Process(
        target=_fifo_race_probe,
        args=(str(selected), str(source), outcome),
    )
    process.start()
    process.join(timeout=2)
    completed = not process.is_alive()
    if not completed:
        process.terminate()
        process.join(timeout=3)
    try:
        result = outcome.get(timeout=1) if completed else ("blocked",)
    except Empty:
        result = ("no_result", process.exitcode)
    finally:
        outcome.close()
        outcome.join_thread()

    assert completed, "regular→FIFO 竞态使 manifest loader 超过两秒仍未返回"
    assert result[:2] == ("declaration_error", "invalid_workflow_source")


def test_nul_source_path_is_stable_nonleaking_declaration_error(
    tmp_path: Path,
) -> None:
    selected = tmp_path / "selected"
    selected.mkdir()
    (selected / PACKAGE_A).mkdir()
    secret = "outside-secret-after-nul"
    manifest = (
        "package:\n"
        f"  name: {PACKAGE_A}\n"
        "workflows:\n"
        f"  - workflow_uuid: {WORKFLOW_A_UUID}\n"
        f'    source: "{PACKAGE_A}/workflows/demo\\0{secret}.py"\n'
    )
    (selected / "package.yaml").write_text(manifest, encoding="utf-8")

    _assert_declaration_error(
        selected,
        code="invalid_workflow_source",
        forbidden_content=secret,
    )


def test_manifest_byte_budget_is_one_mib_and_checked_before_yaml_parse(
    tmp_path: Path,
) -> None:
    selected = tmp_path / "selected"
    _write_manifest_package(selected)
    marker = "oversized-manifest-secret"
    manifest = selected / "package.yaml"
    valid_prefix = manifest.read_bytes()
    padding = (
        b"#"
        + marker.encode()
        + b"x" * (MANIFEST_BYTE_LIMIT - len(valid_prefix) - len(marker) + 1)
    )
    manifest.write_bytes(valid_prefix + padding)
    assert manifest.stat().st_size > MANIFEST_BYTE_LIMIT

    _assert_declaration_error(
        selected,
        code="invalid_manifest",
        forbidden_content=marker,
    )


def test_source_byte_budget_is_eight_mib(tmp_path: Path) -> None:
    selected = tmp_path / "selected"
    _write_manifest_package(selected)
    marker = "oversized-source-secret"
    source = selected / PACKAGE_A / "workflows" / "demo.py"
    source.write_bytes(marker.encode() + b"x" * (SOURCE_BYTE_LIMIT - len(marker) + 1))
    assert source.stat().st_size > SOURCE_BYTE_LIMIT

    _assert_declaration_error(
        selected,
        code="invalid_workflow_source",
        forbidden_content=marker,
    )


def test_yaml_depth_budget_is_32(tmp_path: Path) -> None:
    selected = tmp_path / "selected"
    selected.mkdir()
    (selected / PACKAGE_A).mkdir()
    marker = "over-depth-secret"
    nested = marker
    for _ in range(YAML_DEPTH_LIMIT + 1):
        nested = f"[{nested}]"
    (selected / "package.yaml").write_text(
        "package:\n"
        f"  name: {nested}\n"
        "workflows:\n"
        f"  - workflow_uuid: {WORKFLOW_A_UUID}\n"
        f"    source: {PACKAGE_A}/workflows/demo.py\n",
        encoding="utf-8",
    )

    _assert_declaration_error(
        selected,
        code="invalid_manifest",
        forbidden_content=marker,
    )


def test_workflow_entry_budget_is_1024(tmp_path: Path) -> None:
    selected = tmp_path / "selected"
    marker = "over-entry-secret"
    entries = [
        (
            str(UUID(int=index + 1)),
            f"{PACKAGE_A}/workflows/workflow_{index}.py",
        )
        for index in range(WORKFLOW_ENTRY_LIMIT + 1)
    ]
    entries[-1] = (
        entries[-1][0],
        f"{PACKAGE_A}/workflows/{marker}.py",
    )
    _write_manifest_package(
        selected,
        entries=tuple(entries),
        create_sources=False,
    )

    _assert_declaration_error(
        selected,
        code="invalid_manifest",
        forbidden_content=marker,
    )


def test_yaml_scalar_budget_is_one_mib(tmp_path: Path) -> None:
    selected = tmp_path / "selected"
    selected.mkdir()
    (selected / PACKAGE_A).mkdir()
    marker = "oversized-scalar-secret"
    scalar = marker + "x" * (YAML_SCALAR_BYTE_LIMIT - len(marker) + 1)
    (selected / "package.yaml").write_text(
        "package:\n"
        f"  name: {scalar}\n"
        "workflows:\n"
        f"  - workflow_uuid: {WORKFLOW_A_UUID}\n"
        f"    source: {PACKAGE_A}/workflows/demo.py\n",
        encoding="utf-8",
    )

    _assert_declaration_error(
        selected,
        code="invalid_manifest",
        forbidden_content=marker,
    )


def _create_workflows(service: WorkflowService) -> None:
    for workflow_uuid in (WORKFLOW_A_UUID, WORKFLOW_B_UUID, WORKFLOW_C_UUID):
        service.create_workflow(
            name=f"workflow-{workflow_uuid[:8]}",
            tags=[],
            description=None,
            meta_data={},
            workflow_uuid=workflow_uuid,
        )


def _registration_snapshot(working_dir: Path) -> list[dict[str, Any]]:
    store = WorkflowStore(working_dir / "workflow.db")
    try:
        return WorkflowService(store).list_registered_sources()
    finally:
        store.close()


@pytest.mark.parametrize(
    "collision",
    ["physical_path", "source_uri", "package_identity"],
)
def test_invalid_multi_package_batch_preserves_existing_registration_exactly(
    tmp_path: Path,
    collision: str,
) -> None:
    working_dir = tmp_path / "unilabos_data"
    seed_store = WorkflowStore(working_dir / "workflow.db")
    seed = WorkflowService(seed_store)
    _create_workflows(seed)
    roots: tuple[Path, ...]

    if collision == "physical_path":
        root = tmp_path / "editable"
        source_root = _write_manifest_package(
            root,
            entries=(
                (WORKFLOW_B_UUID, f"{PACKAGE_A}/workflows/b.py"),
                (WORKFLOW_C_UUID, f"{PACKAGE_A}/workflows/shared.py"),
            ),
        )
        seed.register_editable_source(
            workflow_uuid=WORKFLOW_A_UUID,
            package_id="legacy_alpha",
            package_root=source_root,
            relative_path="workflows/shared.py",
        )
        roots = (root,)
    elif collision == "source_uri":
        existing_root = tmp_path / "existing-physical"
        existing_root.mkdir()
        seed.register_editable_source(
            workflow_uuid=WORKFLOW_A_UUID,
            package_id=PACKAGE_A,
            package_root=existing_root,
            relative_path="workflows/shared.py",
        )
        root = tmp_path / "editable"
        _write_manifest_package(
            root,
            entries=(
                (WORKFLOW_B_UUID, f"{PACKAGE_A}/workflows/b.py"),
                (WORKFLOW_C_UUID, f"{PACKAGE_A}/workflows/shared.py"),
            ),
        )
        roots = (root,)
    else:
        existing_root = tmp_path / "existing-alpha"
        existing_root.mkdir()
        seed.register_editable_source(
            workflow_uuid=WORKFLOW_A_UUID,
            package_id=PACKAGE_A,
            package_root=existing_root,
            relative_path="workflows/existing.py",
        )
        root_b = tmp_path / "editable-beta"
        root_c = tmp_path / "editable-alpha"
        _write_manifest_package(
            root_b,
            package_id=PACKAGE_B,
            entries=((WORKFLOW_B_UUID, f"{PACKAGE_B}/workflows/b.py"),),
        )
        _write_manifest_package(
            root_c,
            entries=((WORKFLOW_C_UUID, f"{PACKAGE_A}/workflows/c.py"),),
        )
        roots = (root_b, root_c)

    before = seed.list_registered_sources()
    seed_store.close()
    caught: WorkflowConflict | None = None
    try:
        composition.compose_workflow_runtime(
            working_dir,
            editable_package_roots=roots,
        )
    except WorkflowConflict as error:
        caught = error
    finally:
        composition.reset_workflow_service_for_test()
    after = _registration_snapshot(working_dir)

    assert caught is not None
    assert caught.code == "invalid_input"
    assert after == before


def test_service_is_not_published_until_startup_recovery_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    working_dir = tmp_path / "unilabos_data"
    recovery_entered = threading.Event()
    allow_recovery = threading.Event()
    compose_finished = threading.Event()
    outcome: dict[str, Any] = {}
    original_recover = WorkflowService.recover_registered_sources

    def blocked_recover(service: WorkflowService) -> None:
        recovery_entered.set()
        if not allow_recovery.wait(timeout=3):
            raise TimeoutError("测试未释放 startup recovery")
        original_recover(service)

    monkeypatch.setattr(
        WorkflowService,
        "recover_registered_sources",
        blocked_recover,
    )

    def compose() -> None:
        try:
            outcome["service"] = composition.compose_workflow_runtime(working_dir)
        except BaseException as error:  # noqa: BLE001 - 后台异常回传主线程
            outcome["error"] = error
        finally:
            compose_finished.set()

    thread = threading.Thread(target=compose, name="round02f-recovery-gate")
    thread.start()
    try:
        assert recovery_entered.wait(timeout=1)
        visible_during_recovery = composition.get_workflow_service()
        allow_recovery.set()
        assert compose_finished.wait(timeout=3)
        thread.join(timeout=1)
    finally:
        allow_recovery.set()
        thread.join(timeout=3)

    assert not thread.is_alive()
    assert "error" not in outcome
    assert visible_during_recovery is None
    assert composition.get_workflow_service() is outcome["service"]


def _try_compose_in_second_process(working_dir: str, outcome: Any) -> None:
    try:
        composition.compose_workflow_runtime(working_dir)
    except RuntimeError as error:
        outcome.put(("rejected", str(error)))
    except BaseException as error:  # noqa: BLE001 - 子进程必须回传异常
        outcome.put(("unexpected_error", type(error).__name__, str(error)))
    else:
        outcome.put(("opened", ""))
    finally:
        composition.reset_workflow_service_for_test()


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
        process.join(timeout=3)
        pytest.fail("第二个 OS Workflow Authority 未在限定时间内结束")
    result = outcome.get(timeout=2)
    outcome.close()
    outcome.join_thread()
    return result


def _exception_evidence(error: BaseException | None) -> str:
    if error is None:
        return ""
    seen: set[int] = set()
    pending: list[BaseException] = [error]
    parts: list[str] = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        parts.append(str(current))
        parts.extend(str(note) for note in getattr(current, "__notes__", ()))
        grouped = getattr(current, "exceptions", ())
        pending.extend(item for item in grouped if isinstance(item, BaseException))
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return "\n".join(parts)


class PartialStartMonitor:
    instances: ClassVar[list[PartialStartMonitor]] = []

    def __init__(self, service: WorkflowService) -> None:
        self.service = service
        self.stop_calls = 0
        self.__class__.instances.append(self)

    def start(self) -> None:
        raise RuntimeError(START_FAILURE)

    def stop(self) -> None:
        self.stop_calls += 1
        if self.stop_calls == 1:
            raise RuntimeError(STOP_FAILURE)


def test_partial_monitor_start_and_stop_failure_retain_authority_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    working_dir = tmp_path / "unilabos_data"
    PartialStartMonitor.instances.clear()
    close_calls = 0
    original_close = WorkflowService.close

    def record_close(service: WorkflowService) -> None:
        nonlocal close_calls
        close_calls += 1
        original_close(service)

    monkeypatch.setattr(composition, "WorkflowSourceMonitor", PartialStartMonitor)
    monkeypatch.setattr(WorkflowService, "close", record_close)
    primary_error: BaseException | None = None
    try:
        composition.compose_workflow_runtime(working_dir)
    except BaseException as error:  # noqa: BLE001 - 同时检查主异常与清理证据
        primary_error = error

    close_calls_before_retry = close_calls
    lease_while_failed = _second_process_result(working_dir)
    try:
        composition.reset_workflow_service_for_test()
        lease_after_retry = _second_process_result(working_dir)
    finally:
        try:
            composition.reset_workflow_service_for_test()
        except RuntimeError:
            composition.reset_workflow_service_for_test()

    evidence = _exception_evidence(primary_error)
    assert START_FAILURE in evidence
    assert STOP_FAILURE in evidence
    assert close_calls_before_retry == 0
    assert lease_while_failed == ("rejected", LEASE_REJECTION)
    assert lease_after_retry == ("opened", "")
    assert PartialStartMonitor.instances[0].stop_calls == 2


def test_startup_service_close_failure_retains_primary_error_and_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    working_dir = tmp_path / "unilabos_data"
    original_close = WorkflowService.close
    close_calls = 0

    def fail_recovery(_service: WorkflowService) -> None:
        raise RuntimeError(RECOVERY_FAILURE)

    def fail_once_close(service: WorkflowService) -> None:
        nonlocal close_calls
        close_calls += 1
        if close_calls == 1:
            raise RuntimeError(CLOSE_FAILURE)
        original_close(service)

    monkeypatch.setattr(
        WorkflowService,
        "recover_registered_sources",
        fail_recovery,
    )
    monkeypatch.setattr(WorkflowService, "close", fail_once_close)
    primary_error: BaseException | None = None
    try:
        composition.compose_workflow_runtime(working_dir)
    except BaseException as error:  # noqa: BLE001 - 同时检查主异常与清理证据
        primary_error = error

    lease_while_failed = _second_process_result(working_dir)
    try:
        composition.reset_workflow_service_for_test()
        lease_after_retry = _second_process_result(working_dir)
    finally:
        try:
            composition.reset_workflow_service_for_test()
        except RuntimeError:
            composition.reset_workflow_service_for_test()

    evidence = _exception_evidence(primary_error)
    assert RECOVERY_FAILURE in evidence
    assert CLOSE_FAILURE in evidence
    assert close_calls == 2
    assert lease_while_failed == ("rejected", LEASE_REJECTION)
    assert lease_after_retry == ("opened", "")


def _exchange_directories(first: Path, second: Path) -> None:
    """用 Linux renameat2(RENAME_EXCHANGE) 原子交换两个普通目录。"""

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = libc.renameat2
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    at_fdcwd = -100
    rename_exchange = 2
    result = renameat2(
        at_fdcwd,
        os.fsencode(first),
        at_fdcwd,
        os.fsencode(second),
        rename_exchange,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def test_registration_rejects_regular_root_replaced_after_successful_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    working_dir = tmp_path / "unilabos_data"
    selected = tmp_path / "selected"
    replacement = tmp_path / "replacement"
    _write_manifest_package(selected)
    _write_manifest_package(replacement)
    seed_store = WorkflowStore(working_dir / "workflow.db")
    seed_service = WorkflowService(seed_store)
    seed_service.create_workflow(
        name="root replacement contract",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_A_UUID,
    )
    before = seed_service.list_registered_sources()
    seed_store.close()
    original_load = source_discovery.load_editable_package_manifest
    load_completed = False

    def load_then_replace(package_root: str | Path) -> Any:
        nonlocal load_completed
        manifest = original_load(package_root)
        load_completed = True
        _exchange_directories(selected, replacement)
        assert not selected.is_symlink()
        assert selected.is_dir()
        return manifest

    monkeypatch.setattr(
        source_discovery,
        "load_editable_package_manifest",
        load_then_replace,
    )
    caught: SourceDeclarationError | None = None
    visible_after_attempt: WorkflowService | None = None
    try:
        composition.compose_workflow_runtime(
            working_dir,
            editable_package_roots=(selected,),
        )
    except SourceDeclarationError as error:
        caught = error
    finally:
        visible_after_attempt = composition.get_workflow_service()
        composition.reset_workflow_service_for_test()
    after = _registration_snapshot(working_dir)

    observed_code = caught.code if caught is not None else None
    assert (
        load_completed,
        observed_code,
        visible_after_attempt is None,
        after,
    ) == (True, "invalid_package_root", True, before)
    assert before == []


def test_service_rejects_external_source_larger_than_eight_mib_without_event(
    tmp_path: Path,
) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store)
    package_root = tmp_path / "package"
    package_root.mkdir()
    service.create_workflow(
        name="source read budget contract",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_A_UUID,
    )
    service.register_editable_source(
        workflow_uuid=WORKFLOW_A_UUID,
        package_id="source_budget_contract",
        package_root=package_root,
        relative_path="workflows/demo.py",
    )
    baseline = service.get_authoring(WORKFLOW_A_UUID)
    record_before = store.get_authoring_record(WORKFLOW_A_UUID)
    events_before = service.list_events(after_id=0)["items"]
    source = package_root / "workflows" / "demo.py"
    source.parent.mkdir()
    source.write_bytes(b"x" * (SOURCE_BYTE_LIMIT + 1))
    outcomes: dict[str, tuple[Any, ...]] = {}
    try:
        for name, operation in (
            ("get_authoring", lambda: service.get_authoring(WORKFLOW_A_UUID)),
            (
                "reconcile",
                lambda: service.reconcile_registered_source(WORKFLOW_A_UUID),
            ),
        ):
            try:
                aggregate = operation()
            except WorkflowError as error:
                outcomes[name] = ("error", error.code, str(error))
            else:
                draft = aggregate.get("draft")
                returned_length = (
                    len(draft["python_source"]) if isinstance(draft, dict) else None
                )
                outcomes[name] = ("returned", returned_length)
        record_after = store.get_authoring_record(WORKFLOW_A_UUID)
        events_after = service.list_events(after_id=0)["items"]
    finally:
        store.close()

    expected_outcomes = {
        "get_authoring": ("error", "invalid_input", "提交内容格式不正确"),
        "reconcile": ("error", "invalid_input", "提交内容格式不正确"),
    }
    assert (
        baseline["state"],
        outcomes,
        record_after == record_before,
        events_after,
    ) == ("draft_missing", expected_outcomes, True, events_before)
    assert events_before == []


def test_save_draft_cas_bounds_target_temporary_and_published_source_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store, compiler=SourceOnlyCompiler())
    package_root = tmp_path / "package"
    source = package_root / "workflows" / "demo.py"
    source.parent.mkdir(parents=True)
    source.write_text("value = 'initial'\n", encoding="utf-8")
    service.create_workflow(
        name="CAS source read budget contract",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_A_UUID,
    )
    service.register_editable_source(
        workflow_uuid=WORKFLOW_A_UUID,
        package_id="cas_read_budget_contract",
        package_root=package_root,
        relative_path="workflows/demo.py",
    )
    baseline = service.get_authoring(WORKFLOW_A_UUID)
    read_observations: list[tuple[int, int | None]] = []
    original_read_regular_fd = WorkflowService._read_regular_fd

    def observe_regular_read(
        descriptor: int,
        *,
        byte_limit: int | None = None,
    ) -> bytes:
        read_observations.append((os.fstat(descriptor).st_size, byte_limit))
        return original_read_regular_fd(descriptor, byte_limit=byte_limit)

    monkeypatch.setattr(
        WorkflowService,
        "_read_regular_fd",
        staticmethod(observe_regular_read),
    )
    original_atomic_write = service._atomic_write
    grow_before_cas = False
    oversized_bytes = b"E" * (SOURCE_BYTE_LIMIT + 1)

    def atomic_write_after_optional_growth(*args: Any, **kwargs: Any) -> None:
        if grow_before_cas:
            source.write_bytes(oversized_bytes)
        original_atomic_write(*args, **kwargs)

    monkeypatch.setattr(
        service,
        "_atomic_write",
        atomic_write_after_optional_growth,
    )
    phase_a = service.save_draft(
        WORKFLOW_A_UUID,
        python_source="value = 'bounded successful save'\n",
        expected_draft_hash=baseline["draft"]["draft_hash"],
        expected_workflow_revision=1,
    )
    successful_read_observations = tuple(read_observations)
    read_observations.clear()
    record_before_conflict = store.get_authoring_record(WORKFLOW_A_UUID)
    events_before_conflict = service.list_events(after_id=0)["items"]
    grow_before_cas = True
    conflict: WorkflowConflict | None = None
    try:
        service.save_draft(
            WORKFLOW_A_UUID,
            python_source="value = 'must not replace external growth'\n",
            expected_draft_hash=phase_a["draft"]["draft_hash"],
            expected_workflow_revision=1,
        )
    except WorkflowConflict as error:
        conflict = error
    conflict_read_observations = tuple(read_observations)
    record_after_conflict = store.get_authoring_record(WORKFLOW_A_UUID)
    events_after_conflict = service.list_events(after_id=0)["items"]
    canonical_size = source.stat().st_size
    with source.open("rb") as stream:
        canonical_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    remaining_names = sorted(path.name for path in source.parent.iterdir())
    store.close()

    expected_limit = SOURCE_BYTE_LIMIT
    assert successful_read_observations
    assert conflict_read_observations
    assert (
        conflict.code if conflict is not None else None,
        canonical_size,
        canonical_hash,
        remaining_names,
        record_after_conflict == record_before_conflict,
        events_after_conflict,
    ) == (
        "draft_hash_conflict",
        len(oversized_bytes),
        hashlib.sha256(oversized_bytes).hexdigest(),
        [source.name],
        True,
        events_before_conflict,
    )
    successful_limits = [limit for _size, limit in successful_read_observations]
    conflict_limits = [limit for _size, limit in conflict_read_observations]
    assert successful_limits == [expected_limit] * len(successful_limits) and (
        conflict_limits == [expected_limit] * len(conflict_limits)
    ), (
        "每次 CAS target/temporary/published source read 都必须显式受 8 MiB 限制；"
        f"successful={successful_read_observations!r}, "
        f"target_growth={conflict_read_observations!r}"
    )
