"""M1R Inventory donor/caller 最终收口 RED。

行为测试只经过 public ``InventoryService``；静态测试冻结旧 Store、旧表与
EdgeScheduler 旧 Material 调用的完整退役边界。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import unilabos.app.scheduler.inventory as inventory_api

MATERIAL_UUID = "50000000-0000-4000-8000-000000000501"
RESOURCE_TEMPLATE_UUID = "20000000-0000-4000-8000-000000000501"
SITE_UUID = "60000000-0000-4000-8000-000000000501"

LEGACY_PUBLIC_METHODS = frozenset(
    {
        "upsert_template",
        "delete_template",
        "register_instance",
        "reserve_workflow",
        "consume_reservation",
        "release_reservation",
        "quarantine_reservation",
        "release_workflow",
        "deploy_instance",
        "move_instance",
        "detach_instance",
        "set_instance_parent",
        "consume_instance",
        "discard_instance",
        "update_content",
        "clear_content",
    }
)
FORBIDDEN_TABLE_LITERALS = (
    "resource_template",
    "material_instance",
    "resource_relation",
    "substance_content",
    "inventory_reservation",
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
INVENTORY_PACKAGE = REPOSITORY_ROOT / "unilabos/app/scheduler/inventory"
STORE_FREE_CALLERS = (
    INVENTORY_PACKAGE / "api.py",
    INVENTORY_PACKAGE / "commands.py",
    INVENTORY_PACKAGE / "sync.py",
    INVENTORY_PACKAGE / "layout.py",
    INVENTORY_PACKAGE / "warehouse.py",
    REPOSITORY_ROOT / "unilabos/app/scheduler/integration.py",
    REPOSITORY_ROOT / "unilabos/app/scheduler/main.py",
)
EDGE_SCHEDULER_SOURCE = REPOSITORY_ROOT / "unilabos/app/scheduler/service.py"


def _resource_templates() -> dict[str, inventory_api.ResourceTemplateIdentity]:
    identity = inventory_api.ResourceTemplateIdentity(
        uuid=RESOURCE_TEMPLATE_UUID,
        material_class="SampleTube",
    )
    return {identity.uuid: identity}


def _open_inventory(working_dir: Path) -> inventory_api.InventoryService:
    return inventory_api.InventoryService.open(
        working_dir=working_dir,
        resource_templates=_resource_templates(),
        edge_id="edge-m1r-retirement",
        lab_id="lab-m1r-retirement",
    )


def _create_material(inventory: inventory_api.InventoryService) -> None:
    inventory.create_material(
        material_uuid=MATERIAL_UUID,
        resource_template_uuid=RESOURCE_TEMPLATE_UUID,
        barcode="M1R-RETIREMENT-501",
        name="M1R retirement material",
    )


def _create_site(inventory: inventory_api.InventoryService) -> None:
    inventory.create_site(
        site_uuid=SITE_UUID,
        description="M1R retirement site",
        meta_data={"source": "retirement-red"},
        material_uuid=MATERIAL_UUID,
        name="slot-501",
        sort_order=0,
        allowed_resource_template_uuids=[RESOURCE_TEMPLATE_UUID],
        occupied_material_uuid=None,
        position_x=0.0,
        position_y=0.0,
        position_z=0.0,
        depth=1.0,
        length=1.0,
        width=1.0,
    )


class _CollectingSender:
    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []

    def __call__(self, events: list[dict[str, Any]]) -> int:
        self.received.extend(events)
        return events[-1]["sequence"]


def _store_dependency_findings(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    findings: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and (
                node.module == "unilabos.app.scheduler.inventory.store"
                or any(alias.name == "InventoryStore" for alias in node.names)
            )
        ) or (
            isinstance(node, ast.Import)
            and any(
                alias.name == "unilabos.app.scheduler.inventory.store"
                for alias in node.names
            )
        ):
            findings.append(f"{path.relative_to(REPOSITORY_ROOT)}:{node.lineno}:import")
        elif isinstance(node, ast.Attribute) and node.attr == "store":
            findings.append(f"{path.relative_to(REPOSITORY_ROOT)}:{node.lineno}:.store")
    return tuple(sorted(findings))


def _legacy_table_literal_findings() -> tuple[str, ...]:
    patterns = {
        table: re.compile(rf"(?<![A-Za-z0-9_]){re.escape(table)}(?![A-Za-z0-9_])")
        for table in FORBIDDEN_TABLE_LITERALS
    }
    findings: list[str] = []
    for path in sorted(INVENTORY_PACKAGE.glob("*.py")):
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            matched = sorted(
                table for table, pattern in patterns.items() if pattern.search(line)
            )
            if matched:
                findings.append(
                    f"{path.relative_to(REPOSITORY_ROOT)}:{line_number}:"
                    f"{','.join(matched)}"
                )
    return tuple(findings)


def test_inventory_service_exposes_only_canonical_public_surface(
    tmp_path: Path,
) -> None:
    inventory = _open_inventory(tmp_path)
    try:
        assert not hasattr(inventory, "store")
        assert not hasattr(inventory_api, "InventoryStore")
        assert "InventoryStore" not in inventory_api.__all__
        assert (
            sorted(
                method for method in LEGACY_PUBLIC_METHODS if hasattr(inventory, method)
            )
            == []
        )
    finally:
        inventory.close()


def test_outbox_worker_flushes_and_reopens_through_inventory_service(
    tmp_path: Path,
) -> None:
    inventory = _open_inventory(tmp_path)
    sender = _CollectingSender()
    try:
        _create_material(inventory)
        worker = inventory_api.OutboxWorker(inventory, sender)

        assert inventory.get_acknowledged_sequence(consumer="cloud") == 0
        assert worker.flush_all() == 1
        assert worker.backlog() == 0
        assert len(sender.received) == 1
        event = sender.received[0]
        assert event["event_type"] == "material.created"
        assert event["aggregate_id"] == MATERIAL_UUID
        assert event["payload"]["material"]["uuid"] == MATERIAL_UUID
        acknowledged_sequence = event["sequence"]
        assert (
            inventory.get_acknowledged_sequence(consumer="cloud")
            == acknowledged_sequence
        )
    finally:
        inventory.close()

    reopened = _open_inventory(tmp_path)
    try:
        assert (
            reopened.get_acknowledged_sequence(consumer="cloud")
            == acknowledged_sequence
        )
        assert (
            reopened.read_outbox(
                after_sequence=acknowledged_sequence,
                limit=100,
            )
            == ()
        )
    finally:
        reopened.close()


def test_snapshot_uses_only_canonical_inventory_service_projections(
    tmp_path: Path,
) -> None:
    inventory = _open_inventory(tmp_path)
    try:
        _create_material(inventory)
        _create_site(inventory)

        snapshot = inventory_api.build_snapshot(inventory)
        events = inventory.read_outbox(after_sequence=0, limit=100)

        assert set(snapshot) == {
            "materials",
            "sites",
            "material_reservations",
            "inventory_lots",
            "snapshot_sequence",
        }
        assert [row["uuid"] for row in snapshot["materials"]] == [MATERIAL_UUID]
        assert [row["uuid"] for row in snapshot["sites"]] == [SITE_UUID]
        assert snapshot["material_reservations"] == []
        assert snapshot["inventory_lots"] == []
        assert snapshot["snapshot_sequence"] == max(event.sequence for event in events)
    finally:
        inventory.close()


def test_inventory_adapters_do_not_depend_on_inventory_store() -> None:
    findings = tuple(
        finding
        for path in STORE_FREE_CALLERS
        for finding in _store_dependency_findings(path)
    )

    assert findings == ()


def test_inventory_production_has_no_legacy_table_literals() -> None:
    assert _legacy_table_literal_findings() == ()


def test_edge_scheduler_does_not_call_legacy_material_methods() -> None:
    tree = ast.parse(
        EDGE_SCHEDULER_SOURCE.read_text(encoding="utf-8"),
        filename=str(EDGE_SCHEDULER_SOURCE),
    )
    calls = tuple(
        sorted(
            f"{node.func.attr}:{node.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in LEGACY_PUBLIC_METHODS
        )
    )

    assert calls == ()
