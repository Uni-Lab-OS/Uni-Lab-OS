"""Round 02C authority-scoped Template Catalog public contracts."""

from __future__ import annotations

import re
import socket
import sys
import threading
import urllib.request
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow.store import WorkflowStore

_CATALOG_IMPORT_ERROR: ModuleNotFoundError | None = None
try:
    _catalog_api = import_module("unilabos.workflow.catalog")
except ModuleNotFoundError as error:
    if error.name != "unilabos.workflow.catalog":
        raise
    _CATALOG_IMPORT_ERROR = error
    _catalog_api = None

if _catalog_api is not None:
    CatalogAuthority = _catalog_api.CatalogAuthority
    NodeTemplateImport = _catalog_api.NodeTemplateImport
    TemplateCatalog = _catalog_api.TemplateCatalog
    TemplateCatalogImportError = _catalog_api.TemplateCatalogImportError
    TemplateCatalogMismatch = _catalog_api.TemplateCatalogMismatch
    TemplateCatalogSnapshot = _catalog_api.TemplateCatalogSnapshot
    TemplateCatalogStale = _catalog_api.TemplateCatalogStale
    TemplateCatalogUnavailable = _catalog_api.TemplateCatalogUnavailable
else:

    @dataclass(frozen=True)
    class CatalogAuthority:  # pragma: no cover - RED bootstrap only
        authority_id: str
        kind: str

    @dataclass
    class NodeTemplateImport:  # pragma: no cover - RED bootstrap only
        template: Any
        handles: Any

    class TemplateCatalog:  # pragma: no cover - RED bootstrap only
        def __init__(self, _store: WorkflowStore):
            raise AssertionError(
                "missing frozen production seam: unilabos.workflow.catalog"
            ) from None

    TemplateCatalogSnapshot = Any
    TemplateCatalogImportError = type("TemplateCatalogImportError", (ValueError,), {})
    TemplateCatalogMismatch = type("TemplateCatalogMismatch", (LookupError,), {})
    TemplateCatalogStale = type("TemplateCatalogStale", (RuntimeError,), {})
    TemplateCatalogUnavailable = type(
        "TemplateCatalogUnavailable",
        (RuntimeError,),
        {},
    )


LOCAL_AUTHORITY = CatalogAuthority(authority_id="os-local", kind="local")
SECOND_LOCAL_AUTHORITY = CatalogAuthority(authority_id="os-lab-b", kind="local")
BACKEND_AUTHORITY = CatalogAuthority(authority_id="backend-main", kind="backend")

RESOURCE_TEMPLATE_A_UUID = "10000000-0000-4000-8000-000000000001"
RESOURCE_TEMPLATE_B_UUID = "10000000-0000-4000-8000-000000000002"
NODE_A_UUID = "20000000-0000-4000-8000-000000000001"
NODE_B_UUID = "20000000-0000-4000-8000-000000000002"
NODE_A_REPUBLISHED_UUID = "20000000-0000-4000-8000-000000000003"
HANDLE_A_INPUT_UUID = "30000000-0000-4000-8000-000000000001"
HANDLE_A_OUTPUT_UUID = "30000000-0000-4000-8000-000000000002"
HANDLE_B_INPUT_UUID = "30000000-0000-4000-8000-000000000003"
HANDLE_REPUBLISHED_UUID = "30000000-0000-4000-8000-000000000004"
UNKNOWN_UUID = "40000000-0000-4000-8000-000000000001"

FINGERPRINT_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def _node_template(
    *,
    name: str = "heat",
    resource_template_uuid: str = RESOURCE_TEMPLATE_A_UUID,
    template_uuid: str | None = None,
    display_name: str = "Heat",
) -> dict[str, Any]:
    template: dict[str, Any] = {
        "description": "Heat one material",
        "meta_data": {"family": "thermal", "revision_note": ["stable"]},
        "resource_template_uuid": resource_template_uuid,
        "name": name,
        "display_name": display_name,
        "class": "heater",
        "goal": {"temperature": {"type": "number"}},
        "goal_default": {"temperature": 25.0},
        "feedback": {"temperature": {"type": "number"}},
        "result": {"completed": {"type": "boolean"}},
        "schema": "action-contract-v1",
        "type": "action",
        "icon": "thermometer",
        "header": "Thermal",
        "footer": "v1",
        "node_type": "compute",
    }
    if template_uuid is not None:
        template["uuid"] = template_uuid
    return template


def _handle_template(
    *,
    handle_key: str = "material",
    io_type: str = "target",
    handle_uuid: str | None = None,
    display_name: str = "Material",
    value_type: str = "ResourceSlot",
) -> dict[str, Any]:
    handle: dict[str, Any] = {
        "description": "Complete material root",
        "meta_data": {"source": "declared-contract"},
        "handle_key": handle_key,
        "io_type": io_type,
        "display_name": display_name,
        "type": value_type,
        "required": io_type == "target",
        "data_source": "goal" if io_type == "target" else "result",
        "data_key": handle_key,
    }
    if handle_uuid is not None:
        handle["uuid"] = handle_uuid
    return handle


def _node_import(
    *,
    name: str = "heat",
    resource_template_uuid: str = RESOURCE_TEMPLATE_A_UUID,
    template_uuid: str | None = None,
    display_name: str = "Heat",
    handles: list[dict[str, Any]] | None = None,
) -> NodeTemplateImport:
    return NodeTemplateImport(
        template=_node_template(
            name=name,
            resource_template_uuid=resource_template_uuid,
            template_uuid=template_uuid,
            display_name=display_name,
        ),
        handles=(
            handles
            if handles is not None
            else [
                _handle_template(
                    handle_uuid=(
                        HANDLE_A_INPUT_UUID if template_uuid is not None else None
                    )
                )
            ]
        ),
    )


def _backend_pair() -> list[NodeTemplateImport]:
    return [
        _node_import(
            template_uuid=NODE_A_UUID,
            handles=[
                _handle_template(handle_uuid=HANDLE_A_INPUT_UUID),
                _handle_template(
                    handle_key="material_out",
                    io_type="source",
                    handle_uuid=HANDLE_A_OUTPUT_UUID,
                    display_name="Material output",
                ),
            ],
        ),
        _node_import(
            name="cool",
            resource_template_uuid=RESOURCE_TEMPLATE_B_UUID,
            template_uuid=NODE_B_UUID,
            display_name="Cool",
            handles=[
                _handle_template(
                    handle_key="coolant",
                    handle_uuid=HANDLE_B_INPUT_UUID,
                    display_name="Coolant",
                )
            ],
        ),
    ]


@contextmanager
def _open_catalog(
    database_path: Path,
) -> Iterator[tuple[WorkflowStore, TemplateCatalog]]:
    store = WorkflowStore(database_path)
    try:
        yield store, TemplateCatalog(store)
    finally:
        store.close()


def _active_node(snapshot: TemplateCatalogSnapshot, name: str) -> Mapping[str, Any]:
    return next(node for node in snapshot.node_templates if node["name"] == name)


def _active_handle(
    snapshot: TemplateCatalogSnapshot,
    handle_key: str,
) -> Mapping[str, Any]:
    return next(
        handle
        for handle in snapshot.handle_templates
        if handle["handle_key"] == handle_key
    )


def _snapshot_projection(snapshot: TemplateCatalogSnapshot) -> dict[str, Any]:
    return {
        "authority": snapshot.authority,
        "fingerprint": snapshot.fingerprint,
        "nodes": [dict(node) for node in snapshot.node_templates],
        "handles": [dict(handle) for handle in snapshot.handle_templates],
    }


def _assert_safe_path(error: Any, expected_prefix: str) -> None:
    path = error.path
    assert isinstance(path, str)
    assert path.startswith(expected_prefix)
    assert "\n" not in path
    assert "sqlite" not in path.lower()
    assert "/home/" not in path


def test_unavailable_catalog_is_distinct_from_successfully_imported_empty_catalog(
    tmp_path: Path,
) -> None:
    with _open_catalog(tmp_path / "workflow.db") as (_store, catalog):
        with (
            pytest.raises(TemplateCatalogUnavailable) as caught,
            catalog.snapshot(LOCAL_AUTHORITY),
        ):
            pass

        imported = catalog.replace(LOCAL_AUTHORITY, [])
        with catalog.snapshot(LOCAL_AUTHORITY) as reopened:
            observed = _snapshot_projection(reopened)

    assert caught.value.code == "template_catalog_unavailable"
    _assert_safe_path(caught.value, "/authority")
    assert observed == {
        "authority": LOCAL_AUTHORITY,
        "fingerprint": imported.fingerprint,
        "nodes": [],
        "handles": [],
    }
    assert FINGERPRINT_PATTERN.fullmatch(imported.fingerprint)


def test_authorities_are_strictly_partitioned_without_fallback(tmp_path: Path) -> None:
    with _open_catalog(tmp_path / "workflow.db") as (_store, catalog):
        first = catalog.replace(LOCAL_AUTHORITY, [_node_import()])
        second = catalog.replace(SECOND_LOCAL_AUTHORITY, [_node_import()])

        with (
            pytest.raises(TemplateCatalogUnavailable),
            catalog.snapshot(BACKEND_AUTHORITY),
        ):
            pass

        with (
            catalog.snapshot(SECOND_LOCAL_AUTHORITY) as second_read,
            pytest.raises(TemplateCatalogMismatch) as caught,
        ):
            second_read.require_node(_active_node(first, "heat")["uuid"])

    assert _active_node(first, "heat")["uuid"] != _active_node(second, "heat")["uuid"]
    assert first.fingerprint != second.fingerprint
    assert caught.value.code == "template_catalog_mismatch"
    _assert_safe_path(caught.value, "/node_templates")


def test_local_business_keys_preserve_real_uuid_across_update_case_and_restart(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"
    with _open_catalog(database_path) as (_store, catalog):
        first = catalog.replace(LOCAL_AUTHORITY, [_node_import(name=" Heat ")])
        first_node = _active_node(first, " Heat ")
        first_handle = _active_handle(first, "material")

        updated_import = _node_import(name="hEaT", display_name="Heating")
        updated_import.template["goal"] = {"temperature": {"type": "integer"}}
        second = catalog.replace(LOCAL_AUTHORITY, [updated_import])

        assert _active_node(second, "hEaT")["uuid"] == first_node["uuid"]
        assert _active_handle(second, "material")["uuid"] == first_handle["uuid"]
        assert second.fingerprint != first.fingerprint

    with _open_catalog(database_path) as (_store, restarted):
        third = restarted.replace(
            LOCAL_AUTHORITY,
            [_node_import(name="heat", display_name="Heating after restart")],
        )

    assert _active_node(third, "heat")["uuid"] == first_node["uuid"]
    assert _active_handle(third, "material")["uuid"] == first_handle["uuid"]


def test_local_omission_then_republication_allocates_new_node_and_handle_uuids(
    tmp_path: Path,
) -> None:
    with _open_catalog(tmp_path / "workflow.db") as (_store, catalog):
        first = catalog.replace(LOCAL_AUTHORITY, [_node_import()])
        old_node_uuid = _active_node(first, "heat")["uuid"]
        old_handle_uuid = _active_handle(first, "material")["uuid"]

        empty = catalog.replace(LOCAL_AUTHORITY, [])
        with pytest.raises(TemplateCatalogMismatch):
            empty.require_node(old_node_uuid)
        with pytest.raises(TemplateCatalogMismatch):
            empty.require_handle(old_handle_uuid, node_template_uuid=old_node_uuid)

        rebuilt = catalog.replace(LOCAL_AUTHORITY, [_node_import()])

    assert _active_node(rebuilt, "heat")["uuid"] != old_node_uuid
    assert _active_handle(rebuilt, "material")["uuid"] != old_handle_uuid


def test_local_import_rejects_caller_owned_node_and_handle_uuids(
    tmp_path: Path,
) -> None:
    invalid_node = _node_import(template_uuid=NODE_A_UUID)
    invalid_handle = _node_import(
        handles=[_handle_template(handle_uuid=HANDLE_A_INPUT_UUID)]
    )

    with _open_catalog(tmp_path / "workflow.db") as (_store, catalog):
        catalog.replace(LOCAL_AUTHORITY, [])
        for payload in ([invalid_node], [invalid_handle]):
            with pytest.raises(TemplateCatalogImportError) as caught:
                catalog.replace(LOCAL_AUTHORITY, payload)
            assert caught.value.code == "template_catalog_mismatch"
            _assert_safe_path(caught.value, "/")


@pytest.mark.parametrize("invalid_uuid", [None, "not-a-uuid"])
def test_backend_import_requires_valid_node_and_handle_uuids(
    tmp_path: Path,
    invalid_uuid: str | None,
) -> None:
    template = _node_template(template_uuid=invalid_uuid)
    handle = _handle_template(handle_uuid=invalid_uuid)
    payload = NodeTemplateImport(template=template, handles=[handle])

    with (
        _open_catalog(tmp_path / f"{invalid_uuid or 'missing'}.db") as (
            _store,
            catalog,
        ),
        pytest.raises(TemplateCatalogImportError) as caught,
    ):
        catalog.replace(BACKEND_AUTHORITY, [payload])

    assert caught.value.code == "template_catalog_mismatch"
    _assert_safe_path(caught.value, "/")


def test_backend_import_preserves_exact_upstream_uuids(tmp_path: Path) -> None:
    with _open_catalog(tmp_path / "workflow.db") as (_store, catalog):
        snapshot = catalog.replace(BACKEND_AUTHORITY, _backend_pair())

    assert [node["uuid"] for node in snapshot.node_templates] == sorted(
        [NODE_A_UUID, NODE_B_UUID]
    )
    assert [handle["uuid"] for handle in snapshot.handle_templates] == [
        HANDLE_A_INPUT_UUID,
        HANDLE_A_OUTPUT_UUID,
        HANDLE_B_INPUT_UUID,
    ]
    assert snapshot.require_node(NODE_A_UUID)["name"] == "heat"
    assert (
        snapshot.require_handle(
            HANDLE_A_INPUT_UUID,
            node_template_uuid=NODE_A_UUID,
        )["handle_key"]
        == "material"
    )


def test_backend_business_key_can_be_republished_with_new_uuid(tmp_path: Path) -> None:
    with _open_catalog(tmp_path / "workflow.db") as (_store, catalog):
        catalog.replace(
            BACKEND_AUTHORITY,
            [_node_import(template_uuid=NODE_A_UUID)],
        )
        republished = catalog.replace(
            BACKEND_AUTHORITY,
            [
                _node_import(
                    template_uuid=NODE_A_REPUBLISHED_UUID,
                    handles=[_handle_template(handle_uuid=HANDLE_REPUBLISHED_UUID)],
                )
            ],
        )

        with pytest.raises(TemplateCatalogMismatch):
            republished.require_node(NODE_A_UUID)

    assert republished.require_node(NODE_A_REPUBLISHED_UUID)["name"] == "heat"
    assert (
        republished.require_handle(
            HANDLE_REPUBLISHED_UUID,
            node_template_uuid=NODE_A_REPUBLISHED_UUID,
        )["handle_key"]
        == "material"
    )


def test_backend_omission_can_restore_the_same_upstream_identity(
    tmp_path: Path,
) -> None:
    payload = [_node_import(template_uuid=NODE_A_UUID)]
    with _open_catalog(tmp_path / "workflow.db") as (_store, catalog):
        first = catalog.replace(BACKEND_AUTHORITY, payload)
        catalog.replace(BACKEND_AUTHORITY, [])
        restored = catalog.replace(BACKEND_AUTHORITY, payload)

    assert restored.require_node(NODE_A_UUID)["uuid"] == NODE_A_UUID
    assert (
        restored.require_handle(
            HANDLE_A_INPUT_UUID,
            node_template_uuid=NODE_A_UUID,
        )["uuid"]
        == HANDLE_A_INPUT_UUID
    )
    assert restored.fingerprint == first.fingerprint


def test_backend_uuid_cannot_be_rebound_to_a_different_business_key(
    tmp_path: Path,
) -> None:
    with _open_catalog(tmp_path / "workflow.db") as (_store, catalog):
        before = catalog.replace(
            BACKEND_AUTHORITY,
            [_node_import(template_uuid=NODE_A_UUID)],
        )
        with pytest.raises(TemplateCatalogImportError) as caught:
            catalog.replace(
                BACKEND_AUTHORITY,
                [
                    _node_import(
                        name="cool",
                        resource_template_uuid=RESOURCE_TEMPLATE_B_UUID,
                        template_uuid=NODE_A_UUID,
                    )
                ],
            )
        with catalog.snapshot(BACKEND_AUTHORITY) as after:
            after_projection = _snapshot_projection(after)

    assert caught.value.code == "template_catalog_mismatch"
    _assert_safe_path(caught.value, "/node_templates")
    assert after_projection == _snapshot_projection(before)


def test_backend_handle_uuid_cannot_move_to_another_parent(tmp_path: Path) -> None:
    before_payload = _backend_pair()
    moved_handle_payload = _backend_pair()
    moved_handle_payload[0] = NodeTemplateImport(
        template=moved_handle_payload[0].template,
        handles=[moved_handle_payload[0].handles[1]],
    )
    moved_handle_payload[1] = NodeTemplateImport(
        template=moved_handle_payload[1].template,
        handles=[
            *moved_handle_payload[1].handles,
            _handle_template(handle_uuid=HANDLE_A_INPUT_UUID),
        ],
    )

    with _open_catalog(tmp_path / "workflow.db") as (_store, catalog):
        before = catalog.replace(BACKEND_AUTHORITY, before_payload)
        with pytest.raises(TemplateCatalogImportError) as caught:
            catalog.replace(BACKEND_AUTHORITY, moved_handle_payload)
        with catalog.snapshot(BACKEND_AUTHORITY) as after:
            after_projection = _snapshot_projection(after)

    assert caught.value.code == "template_catalog_mismatch"
    _assert_safe_path(caught.value, "/handle_templates")
    assert after_projection == _snapshot_projection(before)


def test_replace_rejects_normalized_duplicate_business_keys(tmp_path: Path) -> None:
    duplicate_nodes = [
        _node_import(name=" Heat "),
        _node_import(name="hEaT", display_name="Duplicate"),
    ]
    duplicate_handles = [
        _node_import(
            handles=[
                _handle_template(handle_key=" Material "),
                _handle_template(handle_key="mAtErIaL", display_name="Duplicate"),
            ]
        )
    ]

    with _open_catalog(tmp_path / "workflow.db") as (_store, catalog):
        catalog.replace(LOCAL_AUTHORITY, [])
        for payload in (duplicate_nodes, duplicate_handles):
            with pytest.raises(TemplateCatalogImportError) as caught:
                catalog.replace(LOCAL_AUTHORITY, payload)
            assert caught.value.code == "template_catalog_mismatch"


def test_replace_rejects_handle_io_type_outside_source_and_target(
    tmp_path: Path,
) -> None:
    payload = _node_import(
        handles=[_handle_template(io_type="ready")],
    )

    with _open_catalog(tmp_path / "workflow.db") as (_store, catalog):
        catalog.replace(LOCAL_AUTHORITY, [])
        with pytest.raises(TemplateCatalogImportError) as caught:
            catalog.replace(LOCAL_AUTHORITY, [payload])

    assert caught.value.code == "template_catalog_mismatch"
    _assert_safe_path(caught.value, "/handle_templates")


def test_complete_replace_soft_deletes_omitted_nodes_and_handles(
    tmp_path: Path,
) -> None:
    with _open_catalog(tmp_path / "workflow.db") as (_store, catalog):
        first = catalog.replace(BACKEND_AUTHORITY, _backend_pair())
        heat_only = _node_import(
            template_uuid=NODE_A_UUID,
            handles=[_handle_template(handle_uuid=HANDLE_A_INPUT_UUID)],
        )
        second = catalog.replace(BACKEND_AUTHORITY, [heat_only])

        assert second.require_node(NODE_A_UUID)["uuid"] == NODE_A_UUID
        assert (
            second.require_handle(
                HANDLE_A_INPUT_UUID,
                node_template_uuid=NODE_A_UUID,
            )["uuid"]
            == HANDLE_A_INPUT_UUID
        )
        for removed_uuid in (NODE_B_UUID,):
            with pytest.raises(TemplateCatalogMismatch):
                second.require_node(removed_uuid)
        for removed_uuid, parent_uuid in (
            (HANDLE_A_OUTPUT_UUID, NODE_A_UUID),
            (HANDLE_B_INPUT_UUID, NODE_B_UUID),
        ):
            with pytest.raises(TemplateCatalogMismatch):
                second.require_handle(
                    removed_uuid,
                    node_template_uuid=parent_uuid,
                )

    assert second.fingerprint != first.fingerprint
    assert len(second.node_templates) == 1
    assert len(second.handle_templates) == 1


def test_invalid_full_replace_is_atomic_and_preserves_prior_snapshot(
    tmp_path: Path,
) -> None:
    invalid = _backend_pair()
    invalid[0].template["display_name"] = "Must roll back"
    invalid[1].template["uuid"] = NODE_A_UUID

    with _open_catalog(tmp_path / "workflow.db") as (_store, catalog):
        before = catalog.replace(BACKEND_AUTHORITY, _backend_pair())
        with pytest.raises(TemplateCatalogImportError):
            catalog.replace(BACKEND_AUTHORITY, invalid)
        with catalog.snapshot(BACKEND_AUTHORITY) as after:
            after_projection = _snapshot_projection(after)

    assert after_projection == _snapshot_projection(before)


def test_fingerprint_is_deterministic_for_input_order_repeat_and_restart(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"
    initial = _backend_pair()
    with _open_catalog(database_path) as (_store, catalog):
        first = catalog.replace(BACKEND_AUTHORITY, initial)
        reordered = [deepcopy(initial[1]), deepcopy(initial[0])]
        reordered[1] = NodeTemplateImport(
            template=reordered[1].template,
            handles=list(reversed(reordered[1].handles)),
        )
        second = catalog.replace(BACKEND_AUTHORITY, reordered)
        third = catalog.replace(BACKEND_AUTHORITY, deepcopy(initial))

    with (
        _open_catalog(database_path) as (_store, restarted),
        restarted.snapshot(BACKEND_AUTHORITY) as fourth,
    ):
        restarted_projection = _snapshot_projection(fourth)

    assert first.fingerprint == second.fingerprint == third.fingerprint
    assert restarted_projection == _snapshot_projection(third)
    assert FINGERPRINT_PATTERN.fullmatch(first.fingerprint)


@pytest.mark.parametrize(
    ("scope", "field", "changed"),
    [
        ("node", "description", "Changed description"),
        ("node", "meta_data", {"family": "changed"}),
        ("node", "resource_template_uuid", RESOURCE_TEMPLATE_B_UUID),
        ("node", "name", "heat-v2"),
        ("node", "display_name", "Heat v2"),
        ("node", "class", "heater-v2"),
        ("node", "goal", {"temperature": {"type": "integer"}}),
        ("node", "goal_default", {"temperature": 30}),
        ("node", "feedback", {"progress": {"type": "number"}}),
        ("node", "result", {"done": {"type": "boolean"}}),
        ("node", "schema", "action-contract-v2"),
        ("node", "type", "subprocess"),
        ("node", "icon", "flame"),
        ("node", "header", "Header v2"),
        ("node", "footer", "Footer v2"),
        ("node", "node_type", "control"),
        ("handle", "description", "Changed handle"),
        ("handle", "meta_data", {"source": "changed"}),
        ("handle", "handle_key", "sample"),
        ("handle", "io_type", "source"),
        ("handle", "display_name", "Sample"),
        ("handle", "type", "string"),
        ("handle", "required", False),
        ("handle", "data_source", "result"),
        ("handle", "data_key", "sample"),
    ],
)
def test_every_active_identity_or_contract_field_changes_fingerprint(
    tmp_path: Path,
    scope: str,
    field: str,
    changed: Any,
) -> None:
    baseline_payload = _node_import()
    changed_payload = deepcopy(baseline_payload)
    target = changed_payload.template if scope == "node" else changed_payload.handles[0]
    target[field] = changed

    with _open_catalog(tmp_path / f"{scope}-{field}.db") as (_store, catalog):
        baseline = catalog.replace(LOCAL_AUTHORITY, [baseline_payload])
        changed_snapshot = catalog.replace(LOCAL_AUTHORITY, [changed_payload])

    assert changed_snapshot.fingerprint != baseline.fingerprint


def test_authority_id_and_kind_are_part_of_fingerprint(tmp_path: Path) -> None:
    with _open_catalog(tmp_path / "authorities.db") as (_store, catalog):
        local_a = catalog.replace(LOCAL_AUTHORITY, [])
        local_b = catalog.replace(SECOND_LOCAL_AUTHORITY, [])

    same_id = "same-authority-id"
    with _open_catalog(tmp_path / "local-kind.db") as (_store, local_catalog):
        local_kind = local_catalog.replace(
            CatalogAuthority(authority_id=same_id, kind="local"),
            [],
        )
    with _open_catalog(tmp_path / "backend-kind.db") as (_store, backend_catalog):
        backend_kind = backend_catalog.replace(
            CatalogAuthority(authority_id=same_id, kind="backend"),
            [],
        )

    assert local_a.fingerprint != local_b.fingerprint
    assert local_kind.fingerprint != backend_kind.fingerprint


def test_snapshot_is_deeply_immutable_and_detached_from_import_payload(
    tmp_path: Path,
) -> None:
    payload = _node_import()
    with _open_catalog(tmp_path / "workflow.db") as (_store, catalog):
        snapshot = catalog.replace(LOCAL_AUTHORITY, [payload])
        original_display_name = snapshot.node_templates[0]["display_name"]
        original_family = snapshot.node_templates[0]["meta_data"]["family"]

        payload.template["display_name"] = "Caller mutation"
        payload.template["meta_data"]["family"] = "caller-mutation"

        with pytest.raises((AttributeError, TypeError)):
            snapshot.fingerprint = "sha256:mutable"  # type: ignore[misc]
        with pytest.raises(TypeError):
            snapshot.node_templates[0]["display_name"] = "mutation"  # type: ignore[index]
        with pytest.raises(TypeError):
            snapshot.node_templates[0]["meta_data"]["family"] = "mutation"  # type: ignore[index]

    assert snapshot.node_templates[0]["display_name"] == original_display_name
    assert snapshot.node_templates[0]["meta_data"]["family"] == original_family


@pytest.mark.parametrize("use_second_facade", [False, True])
def test_snapshot_guard_blocks_replace_until_context_exit(
    tmp_path: Path,
    use_second_facade: bool,
) -> None:
    store = WorkflowStore(tmp_path / f"guard-{use_second_facade}.db")
    reader = TemplateCatalog(store)
    writer = TemplateCatalog(store) if use_second_facade else reader
    reader.replace(LOCAL_AUTHORITY, [_node_import()])
    replace_started = threading.Event()
    replace_finished = threading.Event()
    outcome: dict[str, Any] = {}

    def replace_catalog() -> None:
        replace_started.set()
        try:
            outcome["snapshot"] = writer.replace(
                LOCAL_AUTHORITY,
                [_node_import(display_name="Blocked update")],
            )
        except BaseException as error:  # noqa: BLE001 - expose worker failure
            outcome["error"] = error
        finally:
            replace_finished.set()

    worker = threading.Thread(target=replace_catalog, daemon=True)
    try:
        with reader.snapshot(LOCAL_AUTHORITY) as held:
            held_fingerprint = held.fingerprint
            worker.start()
            assert replace_started.wait(timeout=1)
            assert not replace_finished.wait(timeout=0.2)

        assert replace_finished.wait(timeout=2)
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert "error" not in outcome
        assert outcome["snapshot"].fingerprint != held_fingerprint
    finally:
        worker.join(timeout=2)
        store.close()


def test_stable_unavailable_mismatch_and_stale_diagnostics(tmp_path: Path) -> None:
    with _open_catalog(tmp_path / "workflow.db") as (_store, catalog):
        unavailable_paths: list[str] = []
        for _ in range(2):
            with (
                pytest.raises(TemplateCatalogUnavailable) as unavailable,
                catalog.snapshot(LOCAL_AUTHORITY),
            ):
                pass
            assert unavailable.value.code == "template_catalog_unavailable"
            _assert_safe_path(unavailable.value, "/authority")
            unavailable_paths.append(unavailable.value.path)

        snapshot = catalog.replace(LOCAL_AUTHORITY, [_node_import()])
        node_uuid = snapshot.node_templates[0]["uuid"]
        handle_uuid = snapshot.handle_templates[0]["uuid"]

        with pytest.raises(TemplateCatalogMismatch) as missing_node:
            snapshot.require_node(UNKNOWN_UUID)
        with pytest.raises(TemplateCatalogMismatch) as wrong_parent:
            snapshot.require_handle(handle_uuid, node_template_uuid=UNKNOWN_UUID)
        with pytest.raises(TemplateCatalogStale) as stale:
            snapshot.assert_fingerprint(f"sha256:{'f' * 64}")

    assert unavailable_paths[0] == unavailable_paths[1]
    assert missing_node.value.code == "template_catalog_mismatch"
    assert wrong_parent.value.code == "template_catalog_mismatch"
    assert stale.value.code == "template_catalog_conflict"
    _assert_safe_path(missing_node.value, "/node_templates")
    _assert_safe_path(wrong_parent.value, "/handle_templates")
    _assert_safe_path(stale.value, "/authority")
    assert snapshot.require_node(node_uuid)["uuid"] == node_uuid


def test_snapshot_read_has_no_network_registry_or_importer_side_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_modules_before = {
        name for name in sys.modules if name.startswith("unilabos.registry")
    }

    def forbidden_side_effect(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Catalog read attempted an external side effect")

    monkeypatch.setattr(socket, "create_connection", forbidden_side_effect)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden_side_effect)

    with _open_catalog(tmp_path / "workflow.db") as (_store, catalog):
        imported = catalog.replace(LOCAL_AUTHORITY, [_node_import()])
        with catalog.snapshot(LOCAL_AUTHORITY) as snapshot:
            assert snapshot.fingerprint == imported.fingerprint
            assert snapshot.require_node(snapshot.node_templates[0]["uuid"])

    registry_modules_after = {
        name for name in sys.modules if name.startswith("unilabos.registry")
    }
    assert registry_modules_after == registry_modules_before


def test_catalog_does_not_synthesize_implicit_ready_or_heuristic_handles(
    tmp_path: Path,
) -> None:
    no_handles = _node_import(handles=[])
    no_handles.template["goal"] = {
        "ready": {"type": "boolean"},
        "material": {"type": "ResourceSlot"},
    }
    no_handles.template["result"] = {
        "material": {"type": "ResourceSlot"},
    }
    no_handles.template["meta_data"] = {
        "handles": ["legacy_runtime_heuristic"],
    }

    explicit_resource_slot = _node_import(
        name="transfer",
        resource_template_uuid=RESOURCE_TEMPLATE_B_UUID,
        handles=[
            _handle_template(
                handle_key="declared_material",
                value_type="ResourceSlot",
            )
        ],
    )

    with _open_catalog(tmp_path / "workflow.db") as (_store, catalog):
        snapshot = catalog.replace(
            LOCAL_AUTHORITY,
            [no_handles, explicit_resource_slot],
        )

    assert [
        (handle["handle_key"], handle["io_type"], handle["type"])
        for handle in snapshot.handle_templates
    ] == [("declared_material", "target", "ResourceSlot")]
