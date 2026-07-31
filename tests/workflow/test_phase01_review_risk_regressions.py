"""Phase 01 风险评审发现的 Authoring 恢复与文件安全回归测试。"""

from __future__ import annotations

import importlib
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from unilabos.config.config import BasicConfig
from unilabos.workflow import composition
from unilabos.workflow.models import CandidateCompilation
from unilabos.workflow.service import (
    WorkflowConflict,
    WorkflowError,
    WorkflowService,
)
from unilabos.workflow.store import WorkflowStore

WORKFLOW_A_UUID = "11111111-1111-4111-8111-111111111111"
WORKFLOW_B_UUID = "22222222-2222-4222-8222-222222222222"
CATALOG_FINGERPRINT = "sha256:" + ("c" * 64)


class DeterministicCompiler:
    compiler_version = "phase-01-risk-review-v1"
    template_catalog_fingerprint = CATALOG_FINGERPRINT

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
        normalized = (
            python_source if python_source.endswith("\n") else python_source + "\n"
        )
        return CandidateCompilation(
            diagnostics=[],
            graph=applied_graph,
            normalized_python_source=normalized,
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
def clean_workflow_composition():
    composition.reset_workflow_service_for_test()
    try:
        yield
    finally:
        composition.reset_workflow_service_for_test()


@pytest.fixture()
def service(tmp_path: Path):
    opened = WorkflowStore(tmp_path / "workflow.db")
    workflow_service = WorkflowService(
        opened,
        compiler=DeterministicCompiler(),
    )
    try:
        yield workflow_service
    finally:
        opened.close()


def _create_workflow(service: WorkflowService, workflow_uuid: str) -> None:
    service.create_workflow(
        name=f"workflow-{workflow_uuid[:8]}",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=workflow_uuid,
    )


def _register(
    service: WorkflowService,
    *,
    workflow_uuid: str,
    package_root: Path,
    relative_path: str,
    package_id: str = "risk_review_package",
) -> dict[str, Any]:
    return service.register_editable_source(
        workflow_uuid=workflow_uuid,
        package_id=package_id,
        package_root=package_root,
        relative_path=relative_path,
    )


def _wait_for(
    observation: Callable[[], Any],
    predicate: Callable[[Any], bool],
    *,
    timeout: float = 3.0,
) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = observation()
        if predicate(value):
            return value
        threading.Event().wait(0.02)
    value = observation()
    assert predicate(value), f"bounded wait timed out; last value: {value!r}"
    return value


def test_composed_runtime_reconciles_all_sources_and_watches_external_drafts(
    tmp_path: Path,
) -> None:
    working_dir = tmp_path / "unilabos_data"
    package_root = tmp_path / "package"
    package_root.mkdir()
    sources = {
        WORKFLOW_A_UUID: package_root / "workflows" / "a.py",
        WORKFLOW_B_UUID: package_root / "workflows" / "b.py",
    }

    seed_store = WorkflowStore(working_dir / "workflow.db")
    seed = WorkflowService(seed_store, compiler=DeterministicCompiler())
    for workflow_uuid, source_path in sources.items():
        _create_workflow(seed, workflow_uuid)
        _register(
            seed,
            workflow_uuid=workflow_uuid,
            package_root=package_root,
            relative_path=f"workflows/{source_path.name}",
        )
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(
            f"value = {workflow_uuid!r}\n",
            encoding="utf-8",
        )
    seed_store.close()

    compose = getattr(composition, "compose_workflow_runtime", None)
    assert callable(compose), "process composition must own reconciliation/watching"
    runtime_service = compose(
        working_dir,
        compiler=DeterministicCompiler(),
    )
    assert isinstance(runtime_service, WorkflowService)

    startup = _wait_for(
        lambda: {
            workflow_uuid: runtime_service.get_authoring(workflow_uuid)
            for workflow_uuid in sources
        },
        lambda aggregates: all(
            aggregate["candidate"] is not None for aggregate in aggregates.values()
        ),
    )
    assert set(startup) == set(sources)
    startup_events = runtime_service.list_events(after_id=0)["items"]
    assert {
        (event["data"]["workflow_uuid"], event["data"]["cause"])
        for event in startup_events
    } == {
        (WORKFLOW_A_UUID, "recovered"),
        (WORKFLOW_B_UUID, "recovered"),
    }

    cursor = startup_events[-1]["id"]
    changed_source = "value = 'changed outside OS'\n"
    sources[WORKFLOW_A_UUID].write_text(changed_source, encoding="utf-8")
    changed = _wait_for(
        lambda: runtime_service.get_authoring(WORKFLOW_A_UUID),
        lambda aggregate: (
            aggregate["draft"]["python_source"] == changed_source
            and aggregate["candidate"] is not None
            and aggregate["candidate"]["draft_hash"] == aggregate["draft"]["draft_hash"]
        ),
    )
    events = runtime_service.list_events(after_id=cursor)["items"]
    assert len(events) == 1
    assert events[0]["event"] == "workflow.authoring.changed"
    assert events[0]["data"] == {
        "workflow_uuid": WORKFLOW_A_UUID,
        "cause": "external_draft_changed",
        "workflow_revision": 1,
        "draft_hash": changed["draft"]["draft_hash"],
        "candidate_hash": changed["candidate"]["candidate_hash"],
    }


def test_server_startup_uses_composed_workflow_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    working_dir = tmp_path / "unilabos_data"
    service = WorkflowService(
        WorkflowStore(tmp_path / "server-workflow.db"),
        compiler=DeterministicCompiler(),
    )
    calls: list[Path] = []

    def fake_compose(configured_working_dir: str | Path):
        calls.append(Path(configured_working_dir))
        return service

    monkeypatch.setattr(
        composition,
        "compose_workflow_runtime",
        fake_compose,
        raising=False,
    )
    monkeypatch.setattr(BasicConfig, "working_dir", str(working_dir))
    server = importlib.reload(importlib.import_module("unilabos.app.web.server"))
    try:
        server.setup_server()
        assert calls == [working_dir]
    finally:
        composition.reset_workflow_service_for_test()
        service._store.close()


@pytest.mark.parametrize("collision", ["physical_path", "source_uri"])
def test_one_source_identity_can_belong_to_only_one_workflow(
    service: WorkflowService,
    tmp_path: Path,
    collision: str,
) -> None:
    first_root = tmp_path / "package-a"
    first_root.mkdir()
    second_root = first_root
    second_package_id = "different_package"
    if collision == "source_uri":
        second_root = tmp_path / "package-b"
        second_root.mkdir()
        second_package_id = "risk_review_package"
    _create_workflow(service, WORKFLOW_A_UUID)
    _create_workflow(service, WORKFLOW_B_UUID)
    first = _register(
        service,
        workflow_uuid=WORKFLOW_A_UUID,
        package_root=first_root,
        relative_path="workflows/shared.py",
    )

    with pytest.raises(WorkflowConflict) as conflict:
        _register(
            service,
            workflow_uuid=WORKFLOW_B_UUID,
            package_root=second_root,
            relative_path="workflows/shared.py",
            package_id=second_package_id,
        )
    assert conflict.value.code == "invalid_input"
    assert service.get_authoring(WORKFLOW_A_UUID)["draft"] is None
    assert first["source_uri"] == ("package://risk_review_package/workflows/shared.py")
    with pytest.raises(WorkflowError) as unregistered:
        service.get_authoring(WORKFLOW_B_UUID)
    assert unregistered.value.code == "workflow_not_found"


@pytest.mark.parametrize("symlink_location", ["package_root", "relative_parent"])
def test_source_registration_rejects_every_symlinked_parent(
    service: WorkflowService,
    tmp_path: Path,
    symlink_location: str,
) -> None:
    _create_workflow(service, WORKFLOW_A_UUID)
    if symlink_location == "package_root":
        real_root = tmp_path / "real-package"
        real_root.mkdir()
        package_root = tmp_path / "package-link"
        package_root.symlink_to(real_root, target_is_directory=True)
        relative_path = "workflows/demo.py"
    else:
        package_root = tmp_path / "package"
        package_root.mkdir()
        real_parent = package_root / "real-workflows"
        real_parent.mkdir()
        (package_root / "workflows").symlink_to(
            real_parent,
            target_is_directory=True,
        )
        relative_path = "workflows/demo.py"

    with pytest.raises(WorkflowError) as rejected:
        _register(
            service,
            workflow_uuid=WORKFLOW_A_UUID,
            package_root=package_root,
            relative_path=relative_path,
        )
    assert rejected.value.code == "invalid_input"


def test_registered_source_resolution_rejects_new_parent_symlink(
    service: WorkflowService,
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "package"
    source_parent = package_root / "workflows"
    source_parent.mkdir(parents=True)
    source_path = source_parent / "demo.py"
    source_path.write_text("value = 'original'\n", encoding="utf-8")
    _create_workflow(service, WORKFLOW_A_UUID)
    _register(
        service,
        workflow_uuid=WORKFLOW_A_UUID,
        package_root=package_root,
        relative_path="workflows/demo.py",
    )

    real_parent = package_root / "real-workflows"
    source_parent.rename(real_parent)
    source_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(WorkflowError) as rejected:
        service.get_authoring(WORKFLOW_A_UUID)
    assert rejected.value.code == "invalid_input"
