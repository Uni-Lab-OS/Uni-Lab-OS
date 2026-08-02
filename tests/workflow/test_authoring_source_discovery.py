"""Round 02F editable package source declaration 与组合生命周期合同。"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from unilabos.workflow import composition
from unilabos.workflow.models import CandidateCompilation
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_A_UUID = "11111111-1111-4111-8111-111111111111"
WORKFLOW_B_UUID = "22222222-2222-4222-8222-222222222222"
PACKAGE_A = "alpha_lab"
PACKAGE_B = "beta_lab"
CATALOG_FINGERPRINT = f"sha256:{'f' * 64}"


def _discovery() -> ModuleType:
    """让未实现的 02F module 成为逐项行为 RED，而不是 collection error。"""

    try:
        return importlib.import_module("unilabos.workflow.source_discovery")
    except ModuleNotFoundError:
        pytest.fail(
            "Round 02F 缺少 unilabos.workflow.source_discovery 公共适配层",
            pytrace=False,
        )


class RecordingService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def register_editable_source(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"registered": kwargs["workflow_uuid"]}


class SourceOnlyCompiler:
    compiler_version = "round-02f-test-v1"
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
    package: str = PACKAGE_A,
    entries: tuple[tuple[str, str], ...] = (
        (WORKFLOW_A_UUID, f"{PACKAGE_A}/workflows/demo.py"),
    ),
) -> str:
    lines = ["package:", f"  name: {package}", "", "workflows:"]
    for workflow_uuid, source in entries:
        lines.extend(
            [
                f"  - workflow_uuid: {workflow_uuid}",
                f"    source: {source}",
            ]
        )
    return "\n".join(lines) + "\n"


def _write_package(
    root: Path,
    *,
    package: str = PACKAGE_A,
    entries: tuple[tuple[str, str], ...] = (
        (WORKFLOW_A_UUID, f"{PACKAGE_A}/workflows/demo.py"),
    ),
    create_sources: bool = True,
) -> None:
    root.mkdir(parents=True)
    (root / package).mkdir()
    (root / "package.yaml").write_text(
        _manifest_text(package=package, entries=entries),
        encoding="utf-8",
    )
    if create_sources:
        for _workflow_uuid, source in entries:
            target = root / source
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("result = build()\n", encoding="utf-8")


def _seed_workflows(working_dir: Path, *workflow_uuids: str) -> None:
    store = WorkflowStore(working_dir / "workflow.db")
    service = WorkflowService(store)
    try:
        for workflow_uuid in workflow_uuids:
            service.create_workflow(
                name=f"workflow-{workflow_uuid[:8]}",
                tags=[],
                description=None,
                meta_data={},
                workflow_uuid=workflow_uuid,
            )
    finally:
        store.close()


def _assert_declaration_error(root: Path) -> None:
    api = _discovery()
    error_type = api.SourceDeclarationError
    with pytest.raises(error_type) as captured:
        api.load_editable_package_manifest(root)
    assert isinstance(captured.value.code, str)
    assert captured.value.code
    assert str(captured.value)


def test_closed_manifest_registers_exact_public_service_arguments(
    tmp_path: Path,
) -> None:
    root = tmp_path / "editable"
    entries = (
        (WORKFLOW_A_UUID, f"{PACKAGE_A}/workflows/first.py"),
        (WORKFLOW_B_UUID, f"{PACKAGE_A}/workflows/second.py"),
    )
    _write_package(root, entries=entries)
    service = RecordingService()
    api = _discovery()

    manifest = api.load_editable_package_manifest(root)
    registrations = api.register_editable_package_sources(service, root)

    assert manifest is not None
    assert registrations == (
        {"registered": WORKFLOW_A_UUID},
        {"registered": WORKFLOW_B_UUID},
    )
    assert service.calls == [
        {
            "workflow_uuid": WORKFLOW_A_UUID,
            "package_id": PACKAGE_A,
            "package_root": root / PACKAGE_A,
            "relative_path": "workflows/first.py",
        },
        {
            "workflow_uuid": WORKFLOW_B_UUID,
            "package_id": PACKAGE_A,
            "package_root": root / PACKAGE_A,
            "relative_path": "workflows/second.py",
        },
    ]


@pytest.mark.parametrize(
    "manifest",
    [
        "package:\n  name: alpha_lab\nworkflows: []\nextra: true\n",
        (
            "workflows:\n"
            "  - workflow_uuid: 11111111-1111-4111-8111-111111111111\n"
            "    source: alpha_lab/workflows/demo.py\n"
        ),
        "package:\n  name: alpha_lab\n",
        "package: alpha_lab\nworkflows: []\n",
        "package:\n  name: alpha_lab\n  version: 1\nworkflows: []\n",
        "package:\n  name: ''\nworkflows: []\n",
        "package:\n  name: alpha-lab\nworkflows: []\n",
        "package:\n  name: alpha_lab\nworkflows: {}\n",
        "package:\n  name: alpha_lab\nworkflows: []\n",
        "package:\n  name: alpha_lab\nworkflows:\n  - demo.py\n",
        (
            "package:\n  name: alpha_lab\nworkflows:\n"
            "  - workflow_uuid: 11111111-1111-4111-8111-111111111111\n"
            "    source: alpha_lab/workflows/demo.py\n    extra: true\n"
        ),
    ],
    ids=[
        "unknown-top-level",
        "missing-package",
        "missing-workflows",
        "package-wrong-type",
        "unknown-package-field",
        "empty-name",
        "invalid-package-identifier",
        "workflows-wrong-type",
        "empty-workflows",
        "entry-wrong-type",
        "unknown-entry-field",
    ],
)
def test_manifest_shape_is_closed(tmp_path: Path, manifest: str) -> None:
    root = tmp_path / "editable"
    root.mkdir()
    (root / "alpha_lab").mkdir()
    (root / "package.yaml").write_text(manifest, encoding="utf-8")
    _assert_declaration_error(root)


@pytest.mark.parametrize(
    "manifest_bytes",
    [
        b"package:\n  name: alpha_lab\n  name: beta_lab\nworkflows: []\n",
        b"package: &pkg\n  name: alpha_lab\nworkflows:\n  - *pkg\n",
        b"package: !unsafe\n  name: alpha_lab\nworkflows: []\n",
        b"package:\n  name: alpha_lab\nworkflows: []\n---\n{}\n",
        b"\xff\xfe\x00",
    ],
    ids=["duplicate-key", "alias", "tag", "multi-document", "invalid-utf8"],
)
def test_yaml_is_safe_single_document_without_graph_features(
    tmp_path: Path,
    manifest_bytes: bytes,
) -> None:
    root = tmp_path / "editable"
    root.mkdir()
    (root / "alpha_lab").mkdir()
    (root / "package.yaml").write_bytes(manifest_bytes)
    _assert_declaration_error(root)


@pytest.mark.parametrize(
    "entries",
    [
        (
            (WORKFLOW_A_UUID, f"{PACKAGE_A}/workflows/a.py"),
            (WORKFLOW_A_UUID, f"{PACKAGE_A}/workflows/b.py"),
        ),
        (
            (WORKFLOW_A_UUID, f"{PACKAGE_A}/workflows/a.py"),
            (WORKFLOW_B_UUID, f"{PACKAGE_A}/workflows/a.py"),
        ),
        (("not-a-uuid", f"{PACKAGE_A}/workflows/a.py"),),
        (("00000000-0000-0000-0000-000000000000", f"{PACKAGE_A}/workflows/a.py"),),
        (("AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA", f"{PACKAGE_A}/workflows/a.py"),),
    ],
    ids=[
        "duplicate-uuid",
        "duplicate-path",
        "invalid-uuid",
        "nil-uuid",
        "noncanonical-uuid",
    ],
)
def test_workflow_identity_and_path_are_unique_and_canonical(
    tmp_path: Path,
    entries: tuple[tuple[str, str], ...],
) -> None:
    root = tmp_path / "editable"
    _write_package(root, entries=entries, create_sources=False)
    _assert_declaration_error(root)


@pytest.mark.parametrize(
    "source",
    [
        "/tmp/demo.py",
        "../alpha_lab/workflows/demo.py",
        "alpha_lab/../workflows/demo.py",
        r"alpha_lab\workflows\demo.py",
        "beta_lab/workflows/demo.py",
        "alpha_lab/workflows/nested/demo.py",
        "alpha_lab/demo.py",
        "alpha_lab/workflows/demo.txt",
        "alpha_lab/workflows/.py",
    ],
    ids=[
        "absolute",
        "leading-traversal",
        "inner-traversal",
        "backslash",
        "wrong-package",
        "nested-directory",
        "outside-workflows",
        "non-python",
        "empty-filename",
    ],
)
def test_source_path_is_one_closed_package_relative_shape(
    tmp_path: Path,
    source: str,
) -> None:
    root = tmp_path / "editable"
    _write_package(
        root,
        entries=((WORKFLOW_A_UUID, source),),
        create_sources=False,
    )
    _assert_declaration_error(root)


@pytest.mark.parametrize("unsafe_kind", ["symlink", "directory", "fifo"])
def test_existing_source_must_be_regular_utf8_file(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    root = tmp_path / "editable"
    _write_package(root, create_sources=False)
    source = root / PACKAGE_A / "workflows" / "demo.py"
    source.parent.mkdir()
    if unsafe_kind == "symlink":
        outside = tmp_path / "outside.py"
        outside.write_text("secret = True\n", encoding="utf-8")
        source.symlink_to(outside)
    elif unsafe_kind == "directory":
        source.mkdir()
    else:
        os.mkfifo(source)

    _assert_declaration_error(root)


def test_existing_source_must_decode_as_utf8(tmp_path: Path) -> None:
    root = tmp_path / "editable"
    _write_package(root)
    (root / PACKAGE_A / "workflows" / "demo.py").write_bytes(b"\xff\xfe")
    _assert_declaration_error(root)


def test_declaration_error_does_not_leak_untrusted_manifest_content(
    tmp_path: Path,
) -> None:
    root = tmp_path / "editable"
    root.mkdir()
    (root / PACKAGE_A).mkdir()
    secret = "credential-should-not-appear-in-error"
    (root / "package.yaml").write_text(
        _manifest_text() + f"{secret}: true\n",
        encoding="utf-8",
    )
    api = _discovery()

    with pytest.raises(api.SourceDeclarationError) as captured:
        api.load_editable_package_manifest(root)

    assert secret not in str(captured.value)


@pytest.mark.parametrize("symlink_level", ["manifest-root", "package-source-root"])
def test_manifest_and_package_source_roots_must_not_be_symlink_directories(
    tmp_path: Path,
    symlink_level: str,
) -> None:
    real_root = tmp_path / "real"
    _write_package(real_root)
    if symlink_level == "manifest-root":
        supplied = tmp_path / "linked"
        supplied.symlink_to(real_root, target_is_directory=True)
    else:
        supplied = real_root
        source_root = real_root / PACKAGE_A
        moved = real_root / "actual-source"
        source_root.rename(moved)
        source_root.symlink_to(moved, target_is_directory=True)

    _assert_declaration_error(supplied)


def test_missing_declared_source_is_valid_and_never_created_by_discovery(
    tmp_path: Path,
) -> None:
    root = tmp_path / "editable"
    _write_package(root, create_sources=False)
    source = root / PACKAGE_A / "workflows" / "demo.py"
    service = RecordingService()

    registrations = _discovery().register_editable_package_sources(service, root)

    assert len(registrations) == 1
    assert not source.exists()


def test_all_manifest_validation_precedes_first_registration(tmp_path: Path) -> None:
    root = tmp_path / "editable"
    entries = (
        (WORKFLOW_A_UUID, f"{PACKAGE_A}/workflows/valid.py"),
        (WORKFLOW_B_UUID, f"{PACKAGE_A}/workflows/nested/invalid.py"),
    )
    _write_package(root, entries=entries, create_sources=False)
    service = RecordingService()
    api = _discovery()

    with pytest.raises(api.SourceDeclarationError):
        api.register_editable_package_sources(service, root)

    assert service.calls == []


def test_registration_rechecks_containment_if_parent_is_swapped_after_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "editable"
    _write_package(root)
    source_root = root / PACKAGE_A
    outside = tmp_path / "outside"
    outside.mkdir()
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store)
    service.create_workflow(
        name="containment race",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_A_UUID,
    )
    original = service.register_editable_source

    def swap_then_register(**kwargs: Any) -> dict[str, Any]:
        moved = root / "moved-source"
        source_root.rename(moved)
        source_root.symlink_to(outside, target_is_directory=True)
        return original(**kwargs)

    monkeypatch.setattr(service, "register_editable_source", swap_then_register)
    try:
        with pytest.raises(WorkflowError) as captured:
            _discovery().register_editable_package_sources(service, root)
        assert captured.value.code == "invalid_input"
        assert service.list_registered_sources() == []
    finally:
        store.close()


def test_composition_registers_reconciles_then_starts_monitor_with_complete_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    working_dir = tmp_path / "unilabos_data"
    package_root = tmp_path / "editable"
    entries = (
        (WORKFLOW_A_UUID, f"{PACKAGE_A}/workflows/a.py"),
        (WORKFLOW_B_UUID, f"{PACKAGE_A}/workflows/b.py"),
    )
    _write_package(package_root, entries=entries)
    _seed_workflows(working_dir, WORKFLOW_A_UUID, WORKFLOW_B_UUID)
    trace: list[tuple[str, Any]] = []
    original_register = WorkflowService.register_editable_source
    original_reconcile = WorkflowService.reconcile_registered_source

    def record_register(self: WorkflowService, **kwargs: Any) -> dict[str, Any]:
        trace.append(("register", kwargs["workflow_uuid"]))
        return original_register(self, **kwargs)

    def record_reconcile(self: WorkflowService, workflow_uuid: str) -> dict[str, Any]:
        trace.append(("reconcile", workflow_uuid))
        return original_reconcile(self, workflow_uuid)

    def record_start(monitor: Any) -> None:
        registrations = monitor._service.list_registered_sources()
        trace.append(
            ("monitor", tuple(item["workflow_uuid"] for item in registrations))
        )

    monkeypatch.setattr(WorkflowService, "register_editable_source", record_register)
    monkeypatch.setattr(
        WorkflowService, "reconcile_registered_source", record_reconcile
    )
    monkeypatch.setattr(composition.WorkflowSourceMonitor, "start", record_start)

    service = composition.compose_workflow_runtime(
        working_dir,
        compiler=SourceOnlyCompiler(),
        editable_package_roots=(package_root,),
    )

    assert service is composition.get_workflow_service()
    assert trace == [
        ("register", WORKFLOW_A_UUID),
        ("register", WORKFLOW_B_UUID),
        ("reconcile", WORKFLOW_A_UUID),
        ("reconcile", WORKFLOW_B_UUID),
        ("monitor", (WORKFLOW_A_UUID, WORKFLOW_B_UUID)),
    ]


def test_two_explicit_packages_register_deterministically_without_scanning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    working_dir = tmp_path / "unilabos_data"
    root_a = tmp_path / "package-a"
    root_b = tmp_path / "package-b"
    _write_package(root_a)
    _write_package(
        root_b,
        package=PACKAGE_B,
        entries=((WORKFLOW_B_UUID, f"{PACKAGE_B}/workflows/b.py"),),
    )
    undeclared = root_a / PACKAGE_A / "workflows" / "not_declared.py"
    undeclared.write_text("must_not_be_scanned = True\n", encoding="utf-8")
    _seed_workflows(working_dir, WORKFLOW_A_UUID, WORKFLOW_B_UUID)
    registration_order: list[str] = []
    original_register = WorkflowService.register_editable_source

    def record_register(self: WorkflowService, **kwargs: Any) -> dict[str, Any]:
        registration_order.append(kwargs["workflow_uuid"])
        return original_register(self, **kwargs)

    monkeypatch.setattr(WorkflowService, "register_editable_source", record_register)

    service = composition.compose_workflow_runtime(
        working_dir,
        compiler=SourceOnlyCompiler(),
        editable_package_roots=(root_b, root_a),
    )

    registrations = service.list_registered_sources()
    assert registration_order == [WORKFLOW_B_UUID, WORKFLOW_A_UUID]
    assert [item["workflow_uuid"] for item in registrations] == [
        WORKFLOW_A_UUID,
        WORKFLOW_B_UUID,
    ]
    assert {item["relative_path"] for item in registrations} == {
        "workflows/b.py",
        "workflows/demo.py",
    }


def test_duplicate_workflow_identity_across_packages_fails_closed(
    tmp_path: Path,
) -> None:
    working_dir = tmp_path / "unilabos_data"
    root_a = tmp_path / "package-a"
    root_b = tmp_path / "package-b"
    _write_package(root_a)
    _write_package(
        root_b,
        package=PACKAGE_B,
        entries=((WORKFLOW_A_UUID, f"{PACKAGE_B}/workflows/b.py"),),
    )
    _seed_workflows(working_dir, WORKFLOW_A_UUID)

    with pytest.raises(WorkflowError):
        composition.compose_workflow_runtime(
            working_dir,
            compiler=SourceOnlyCompiler(),
            editable_package_roots=(root_a, root_b),
        )

    assert composition.get_workflow_service() is None
    store = WorkflowStore(working_dir / "workflow.db")
    try:
        assert WorkflowService(store).list_registered_sources() == []
    finally:
        store.close()


def test_manifest_failure_cleans_composition_and_releases_workspace_lease(
    tmp_path: Path,
) -> None:
    working_dir = tmp_path / "unilabos_data"
    package_root = tmp_path / "editable"
    _write_package(package_root)
    _seed_workflows(working_dir, WORKFLOW_A_UUID)
    manifest = package_root / "package.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + "unknown: true\n",
        encoding="utf-8",
    )
    api = _discovery()

    with pytest.raises(api.SourceDeclarationError):
        composition.compose_workflow_runtime(
            working_dir,
            compiler=SourceOnlyCompiler(),
            editable_package_roots=(package_root,),
        )
    assert composition.get_workflow_service() is None

    manifest.write_text(_manifest_text(), encoding="utf-8")
    replacement = composition.compose_workflow_runtime(
        working_dir,
        compiler=SourceOnlyCompiler(),
        editable_package_roots=(package_root,),
    )
    assert replacement is composition.get_workflow_service()


def test_missing_workflow_is_not_created_to_satisfy_declaration(tmp_path: Path) -> None:
    working_dir = tmp_path / "unilabos_data"
    package_root = tmp_path / "editable"
    _write_package(package_root)

    with pytest.raises(WorkflowError) as captured:
        composition.compose_workflow_runtime(
            working_dir,
            compiler=SourceOnlyCompiler(),
            editable_package_roots=(package_root,),
        )

    assert captured.value.code == "workflow_not_found"
    assert composition.get_workflow_service() is None
    store = WorkflowStore(working_dir / "workflow.db")
    try:
        assert WorkflowService(store).list_workflows()["items"] == []
    finally:
        store.close()


def test_composition_identity_includes_compiler_and_package_root_set(
    tmp_path: Path,
) -> None:
    working_dir = tmp_path / "unilabos_data"
    root_a = tmp_path / "package-a"
    root_b = tmp_path / "package-b"
    _write_package(root_a)
    _write_package(
        root_b,
        package=PACKAGE_B,
        entries=((WORKFLOW_B_UUID, f"{PACKAGE_B}/workflows/b.py"),),
    )
    _seed_workflows(working_dir, WORKFLOW_A_UUID, WORKFLOW_B_UUID)
    compiler = SourceOnlyCompiler()
    service = composition.compose_workflow_runtime(
        working_dir,
        compiler=compiler,
        editable_package_roots=(root_a,),
    )
    assert (
        composition.compose_workflow_runtime(
            working_dir,
            compiler=compiler,
            editable_package_roots=(root_a,),
        )
        is service
    )

    with pytest.raises(RuntimeError):
        composition.compose_workflow_runtime(
            working_dir,
            compiler=SourceOnlyCompiler(),
            editable_package_roots=(root_a,),
        )
    with pytest.raises(RuntimeError):
        composition.compose_workflow_runtime(
            working_dir,
            compiler=compiler,
            editable_package_roots=(root_b,),
        )


def test_missing_source_reconciles_to_missing_then_recovers_at_canonical_path(
    tmp_path: Path,
) -> None:
    working_dir = tmp_path / "unilabos_data"
    package_root = tmp_path / "editable"
    _write_package(package_root, create_sources=False)
    _seed_workflows(working_dir, WORKFLOW_A_UUID)

    service = composition.compose_workflow_runtime(
        working_dir,
        compiler=SourceOnlyCompiler(),
        editable_package_roots=(package_root,),
    )
    assert service.get_authoring(WORKFLOW_A_UUID)["state"] == "draft_missing"
    source = package_root / PACKAGE_A / "workflows" / "demo.py"
    source.parent.mkdir()
    source.write_text("recovered = True\n", encoding="utf-8")

    recovered = service.reconcile_registered_source(WORKFLOW_A_UUID)
    events = service.list_events(after_id=0)["items"]

    assert recovered["draft"]["python_source"] == "recovered = True\n"
    assert events[-1]["data"]["cause"] == "recovered"
