"""M1D Task-owned Material Reservation 的独立公共合同 RED。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from fastapi.testclient import TestClient

from unilabos.app.workflow_api import create_workflow_app
from unilabos.resources.authority import (
    MaterialConflict,
    MaterialModule,
    ResourceTemplateIdentity,
)
from unilabos.resources.authority.sqlite import SQLiteMaterialAdapter
from unilabos.workflow import composition
from unilabos.workflow.catalog import (
    CatalogAuthority,
    NodeTemplateImport,
    TemplateCatalog,
)
from unilabos.workflow.material_resolver import MaterialResourceSlotResolver
from unilabos.workflow.models import WorkflowNodeWrite
from unilabos.workflow.runtime import WorkflowRuntimeCoordinator
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import StoreConflict, WorkflowStore

RESOURCE_TEMPLATE_UUID = "2abcdef0-1234-4abc-8def-000000000017"
MATERIAL_A_UUID = "5abcdef0-1234-4abc-8def-000000000017"
MATERIAL_B_UUID = "5abcdef0-1234-4abc-8def-000000000018"
MATERIAL_C_UUID = "5abcdef0-1234-4abc-8def-000000000019"

PLAIN_WORKFLOW_UUID = "10000000-0000-4000-8000-000000000001"
PLAIN_NODE_UUID = "20000000-0000-4000-8000-000000000001"
SINGLE_WORKFLOW_UUID = "10000000-0000-4000-8000-000000000002"
SINGLE_NODE_UUID = "20000000-0000-4000-8000-000000000002"
MULTI_WORKFLOW_UUID = "10000000-0000-4000-8000-000000000003"
MULTI_NODE_UUID = "20000000-0000-4000-8000-000000000003"
ACTION_WORKFLOW_UUID = "10000000-0000-4000-8000-000000000004"
ACTION_NODE_UUID = "20000000-0000-4000-8000-000000000004"
ACTION_DEVICE_TEMPLATE_UUID = "2abcdef0-1234-4abc-8def-000000000099"
CALLER_TEMPLATE_UUID = "2abcdef0-1234-4abc-8def-000000000098"
ACTION_AUTHORITY = CatalogAuthority(authority_id="m1d-local", kind="local")


def _resource_templates() -> Mapping[str, object]:
    identity = ResourceTemplateIdentity(
        uuid=RESOURCE_TEMPLATE_UUID,
        material_class="SampleTube",
    )
    return MappingProxyType({identity.uuid: identity})


@contextmanager
def _open_authority(
    database_path: Path,
) -> Iterator[tuple[WorkflowStore, MaterialModule]]:
    store = WorkflowStore(database_path)
    try:
        adapter = SQLiteMaterialAdapter.from_runtime_authority(store)
        yield (
            store,
            MaterialModule(
                adapter,
                resource_templates=_resource_templates(),
            ),
        )
    finally:
        store.close()


def _create_material(
    materials: MaterialModule,
    material_uuid: str,
    *,
    uow: Any | None = None,
) -> None:
    materials.create_business_material(
        material_uuid=material_uuid,
        resource_template_uuid=RESOURCE_TEMPLATE_UUID,
        barcode=f"M1D-{material_uuid[-3:]}",
        name=f"M1D material {material_uuid[-3:]}",
        uow=uow,
    )


def _resource_slot_parameter(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "schema": {
            "$slot": "ResourceSlot",
            "allowed_resource_template_uuids": [RESOURCE_TEMPLATE_UUID],
        },
        "required": True,
    }


def _seed_workflow(
    store: WorkflowStore,
    *,
    workflow_uuid: str,
    node_uuid: str,
    parameters: Sequence[dict[str, Any]],
) -> None:
    store.create_workflow(
        workflow_uuid=workflow_uuid,
        name=f"M1D workflow {workflow_uuid[-1]}",
        tags=[],
        description=None,
        meta_data={
            "unilab": {
                "input_contract": {
                    "version": 1,
                    "parameters": list(parameters),
                }
            }
        },
    )
    store.save_graph(
        workflow_uuid,
        revision=1,
        nodes=[
            WorkflowNodeWrite(
                uuid=node_uuid,
                workflow_node_template_uuid=None,
                name="M1D active node",
                status="idle",
                type="compute",
                pose={},
                param={},
                execution_policy={},
                disabled=False,
                minimized=False,
                meta_data={},
            )
        ],
        edges=[],
    )


def _create_plain_task(service: WorkflowService) -> dict[str, Any]:
    return service.create_workflow_task(
        workflow_uuid=PLAIN_WORKFLOW_UUID,
        run_mode="normal",
        target_node_uuid=None,
        input_value={},
        description=None,
        meta_data={},
    )


def _reservation_rows(
    connection: sqlite3.Connection,
    task_uuid: str,
) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    headers = connection.execute(
        """
        SELECT uuid, workflow_task_uuid, set_fingerprint, status,
               create_time, released_at
        FROM material_reservation
        WHERE workflow_task_uuid = ?
        ORDER BY create_time, uuid
        """,
        (task_uuid,),
    ).fetchall()
    members = connection.execute(
        """
        SELECT member.reservation_uuid, member.material_uuid,
               member.root_material_uuid, member.acquired_version,
               member.released_at
        FROM material_reservation_member AS member
        JOIN material_reservation AS reservation
          ON reservation.uuid = member.reservation_uuid
        WHERE reservation.workflow_task_uuid = ?
        ORDER BY member.material_uuid
        """,
        (task_uuid,),
    ).fetchall()
    return headers, members


def _reservation_snapshot(
    database_path: Path,
    task_uuid: str,
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        headers, members = _reservation_rows(connection, task_uuid)
        return [tuple(row) for row in headers], [tuple(row) for row in members]
    finally:
        connection.close()


def _table_count(database_path: Path, table: str) -> int:
    connection = sqlite3.connect(database_path)
    try:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        connection.close()


def _post_task(
    client: TestClient,
    *,
    workflow_uuid: str,
    input_value: dict[str, Any],
) -> Any:
    return client.post(
        "/api/v1/workflow-tasks",
        json={"workflow_uuid": workflow_uuid, "input": input_value},
    )


def _publish_typed_action_catalog(
    store: WorkflowStore,
) -> tuple[str, str]:
    snapshot = TemplateCatalog(store).replace(
        ACTION_AUTHORITY,
        [
            NodeTemplateImport(
                template={
                    "description": "Consume one concrete sample",
                    "meta_data": {"source": "@action"},
                    "resource_template_uuid": ACTION_DEVICE_TEMPLATE_UUID,
                    "name": "consume_sample",
                    "display_name": "Consume sample",
                    "class": "m1d.actions:Consumer",
                    "goal": {},
                    "goal_default": {},
                    "feedback": {},
                    "result": {},
                    "schema": None,
                    "type": "action",
                    "icon": None,
                    "header": None,
                    "footer": None,
                    "node_type": "device_action",
                },
                handles=[
                    {
                        "description": "Concrete business Material root",
                        "meta_data": {
                            "unilab": {
                                "value_schema": {"$slot": "ResourceSlot"},
                                "editor_control": "material_port",
                                "allowed_resource_template_uuids": [
                                    RESOURCE_TEMPLATE_UUID
                                ],
                                "implicit_passthrough": False,
                            }
                        },
                        "handle_key": "sample",
                        "io_type": "target",
                        "display_name": "Sample",
                        "type": "ResourceSlot",
                        "required": True,
                        "data_source": "goal",
                        "data_key": "sample",
                    }
                ],
            )
        ],
    )
    node_template = snapshot.node_templates[0]
    target_handle = snapshot.handle_templates[0]
    assert target_handle["workflow_node_template_uuid"] == node_template["uuid"]
    unilab_meta = target_handle["meta_data"]["unilab"]
    assert unilab_meta["value_schema"]["$slot"] == "ResourceSlot"
    assert unilab_meta["editor_control"] == "material_port"
    assert tuple(unilab_meta["allowed_resource_template_uuids"]) == (
        RESOURCE_TEMPLATE_UUID,
    )
    assert unilab_meta["implicit_passthrough"] is False
    return str(node_template["uuid"]), str(target_handle["uuid"])


def _save_action_literal_workflow(
    store: WorkflowStore,
    *,
    node_template_uuid: str,
    revision: int,
    sample: dict[str, str],
) -> None:
    store.save_graph(
        ACTION_WORKFLOW_UUID,
        revision=revision,
        nodes=[
            WorkflowNodeWrite(
                uuid=ACTION_NODE_UUID,
                workflow_node_template_uuid=node_template_uuid,
                name="consume_sample",
                status="idle",
                type="device",
                pose={},
                param={"sample": sample},
                action_name="consume_sample",
                execution_policy={},
                disabled=False,
                minimized=False,
                meta_data={},
            )
        ],
        edges=[],
    )


def _create_action_literal_task(service: WorkflowService) -> dict[str, Any]:
    return service.create_workflow_task(
        workflow_uuid=ACTION_WORKFLOW_UUID,
        run_mode="normal",
        target_node_uuid=None,
        input_value={},
        description=None,
        meta_data={},
    )


def test_typed_action_literal_is_canonicalized_reserved_and_dispatch_guarded(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"
    with _open_authority(database_path) as (store, materials):
        _create_material(materials, MATERIAL_A_UUID)
        node_template_uuid, target_handle_uuid = _publish_typed_action_catalog(store)
        store.create_workflow(
            workflow_uuid=ACTION_WORKFLOW_UUID,
            name="M1D typed Action literal",
            tags=[],
            description=None,
            meta_data={},
        )
        _save_action_literal_workflow(
            store,
            node_template_uuid=node_template_uuid,
            revision=1,
            sample={"uuid": MATERIAL_A_UUID.upper()},
        )

        saved_graph = store.get_graph(ACTION_WORKFLOW_UUID)
        saved_target = next(
            handle
            for handle in saved_graph["handle_templates"]
            if handle["uuid"] == target_handle_uuid
        )
        assert saved_target["io_type"] == "target"
        assert saved_target["type"] == "ResourceSlot"
        assert saved_target["meta_data"]["unilab"]["value_schema"] == {
            "$slot": "ResourceSlot"
        }

        material_authority = MaterialResourceSlotResolver(materials)
        service = WorkflowService(
            store,
            resource_resolver=material_authority,
            material_reservations=material_authority,
        )
        coordinator = WorkflowRuntimeCoordinator(
            store,
            material_reservations=material_authority,
        )

        winner = _create_action_literal_task(service)
        winner_job = service.list_workflow_node_jobs(winner["uuid"])[0]
        with store.transaction() as uow:
            assert materials.has_complete_task_reservation(
                uow,
                task_uuid=winner["uuid"],
                root_material_uuids=(MATERIAL_A_UUID,),
            )

        canonical_slot = {
            "uuid": MATERIAL_A_UUID,
            "resource_template_uuid": RESOURCE_TEMPLATE_UUID,
        }
        frozen_node = next(
            node
            for node in winner["workflow_snapshot"]["nodes"]
            if node["uuid"] == ACTION_NODE_UUID
        )
        planned_node = next(
            node
            for node in winner["execution_plan"]["nodes"]
            if node["uuid"] == ACTION_NODE_UUID
        )
        assert frozen_node["param"] == {"sample": canonical_slot}
        assert planned_node["param"] == {"sample": canonical_slot}
        assert winner_job["param"] == {"sample": canonical_slot}

        contender = _create_action_literal_task(service)
        contender_job = service.list_workflow_node_jobs(contender["uuid"])[0]
        assert contender["status"] == "pending"
        assert _reservation_snapshot(database_path, contender["uuid"]) == ([], [])
        with store.transaction() as uow:
            assert not materials.has_complete_task_reservation(
                uow,
                task_uuid=contender["uuid"],
                root_material_uuids=(MATERIAL_A_UUID,),
            )

        assert (
            coordinator.transition_job(winner_job["uuid"], "dispatched")["status"]
            == "dispatched"
        )
        with pytest.raises(StoreConflict):
            coordinator.transition_job(contender_job["uuid"], "dispatched")
        assert store.get_job(contender_job["uuid"])["status"] == "pending"

        current_revision = store.get_graph(ACTION_WORKFLOW_UUID)["workflow"]["revision"]
        _save_action_literal_workflow(
            store,
            node_template_uuid=node_template_uuid,
            revision=current_revision,
            sample={
                "uuid": MATERIAL_A_UUID,
                "resource_template_uuid": CALLER_TEMPLATE_UUID,
            },
        )
        before_invalid = service.list_workflow_tasks(
            workflow_uuid=ACTION_WORKFLOW_UUID
        )["total"]

        with pytest.raises(WorkflowError) as failure:
            _create_action_literal_task(service)

        assert failure.value.code == "invalid_input"
        assert (
            service.list_workflow_tasks(workflow_uuid=ACTION_WORKFLOW_UUID)["total"]
            == before_invalid
        )


def test_public_reservation_primitive_is_complete_deterministic_and_idempotent(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"
    with _open_authority(database_path) as (store, materials):
        _seed_workflow(
            store,
            workflow_uuid=PLAIN_WORKFLOW_UUID,
            node_uuid=PLAIN_NODE_UUID,
            parameters=(),
        )
        service = WorkflowService(store)
        single_task = _create_plain_task(service)
        ordered_set_task = _create_plain_task(service)

        with store.transaction() as uow:
            for material_uuid in (
                MATERIAL_A_UUID,
                MATERIAL_B_UUID,
                MATERIAL_C_UUID,
            ):
                _create_material(materials, material_uuid, uow=uow)

            single_outcome = materials.reserve_task_materials(
                uow,
                task_uuid=single_task["uuid"],
                root_material_uuids=(MATERIAL_A_UUID,),
            )
            assert single_outcome is not None

            headers, members = _reservation_rows(uow, single_task["uuid"])
            assert len(headers) == 1
            assert headers[0]["workflow_task_uuid"] == single_task["uuid"]
            assert headers[0]["status"] == "active"
            assert headers[0]["set_fingerprint"]
            assert headers[0]["create_time"]
            assert headers[0]["released_at"] is None
            assert [
                (
                    row["reservation_uuid"],
                    row["material_uuid"],
                    row["root_material_uuid"],
                    row["acquired_version"],
                    row["released_at"],
                )
                for row in members
            ] == [
                (
                    headers[0]["uuid"],
                    MATERIAL_A_UUID,
                    MATERIAL_A_UUID,
                    1,
                    None,
                )
            ]

        durable_single = _reservation_snapshot(database_path, single_task["uuid"])
        assert len(durable_single[0]) == 1
        assert len(durable_single[1]) == 1

        with store.transaction() as uow:
            first_outcome = materials.reserve_task_materials(
                uow,
                task_uuid=ordered_set_task["uuid"],
                root_material_uuids=(MATERIAL_C_UUID, MATERIAL_B_UUID),
            )
        before_replay = _reservation_snapshot(
            database_path,
            ordered_set_task["uuid"],
        )

        with store.transaction() as uow:
            replay_outcome = materials.reserve_task_materials(
                uow,
                task_uuid=ordered_set_task["uuid"],
                root_material_uuids=(MATERIAL_B_UUID, MATERIAL_C_UUID),
            )

        assert replay_outcome == first_outcome
        assert (
            _reservation_snapshot(
                database_path,
                ordered_set_task["uuid"],
            )
            == before_replay
        )
        assert [row[1] for row in before_replay[1]] == [
            MATERIAL_B_UUID,
            MATERIAL_C_UUID,
        ]
        assert all(row[3] == 1 for row in before_replay[1])

        with pytest.raises(MaterialConflict), store.transaction() as uow:
            materials.reserve_task_materials(
                uow,
                task_uuid=ordered_set_task["uuid"],
                root_material_uuids=(MATERIAL_B_UUID,),
            )

        assert (
            _reservation_snapshot(
                database_path,
                ordered_set_task["uuid"],
            )
            == before_replay
        )


def test_task_create_contention_is_all_or_none_and_blocks_dispatch(
    tmp_path: Path,
) -> None:
    working_dir = tmp_path / "unilabos_data"
    working_dir.mkdir()
    database_path = working_dir / "workflow.db"
    composition.reset_workflow_service_for_test()
    with _open_authority(database_path) as (store, materials):
        _create_material(materials, MATERIAL_A_UUID)
        _create_material(materials, MATERIAL_B_UUID)
        _seed_workflow(
            store,
            workflow_uuid=SINGLE_WORKFLOW_UUID,
            node_uuid=SINGLE_NODE_UUID,
            parameters=(_resource_slot_parameter("sample"),),
        )
        _seed_workflow(
            store,
            workflow_uuid=MULTI_WORKFLOW_UUID,
            node_uuid=MULTI_NODE_UUID,
            parameters=(
                _resource_slot_parameter("sample_a"),
                _resource_slot_parameter("sample_b"),
            ),
        )

    try:
        service = composition.compose_workflow_runtime(working_dir)
        with TestClient(
            create_workflow_app(service),
            raise_server_exceptions=False,
        ) as client:
            winner_response = _post_task(
                client,
                workflow_uuid=SINGLE_WORKFLOW_UUID,
                input_value={"sample": {"uuid": MATERIAL_B_UUID}},
            )
            contender_response = _post_task(
                client,
                workflow_uuid=SINGLE_WORKFLOW_UUID,
                input_value={"sample": {"uuid": MATERIAL_B_UUID}},
            )
            multi_root_response = _post_task(
                client,
                workflow_uuid=MULTI_WORKFLOW_UUID,
                input_value={
                    "sample_a": {"uuid": MATERIAL_A_UUID},
                    "sample_b": {"uuid": MATERIAL_B_UUID},
                },
            )

        assert winner_response.status_code == 201
        assert contender_response.status_code == 201
        assert multi_root_response.status_code == 201
        winner = winner_response.json()["data"]
        contender = contender_response.json()["data"]
        multi_root = multi_root_response.json()["data"]
        assert (
            winner["status"]
            == contender["status"]
            == multi_root["status"]
            == ("pending")
        )

        winner_job = service.list_workflow_node_jobs(winner["uuid"])[0]
        contender_job = service.list_workflow_node_jobs(contender["uuid"])[0]
        multi_root_job = service.list_workflow_node_jobs(multi_root["uuid"])[0]
    finally:
        composition.reset_workflow_service_for_test()

    winner_reservation = _reservation_snapshot(database_path, winner["uuid"])
    contender_reservation = _reservation_snapshot(database_path, contender["uuid"])
    multi_root_reservation = _reservation_snapshot(database_path, multi_root["uuid"])
    assert len(winner_reservation[0]) == 1
    assert [row[1] for row in winner_reservation[1]] == [MATERIAL_B_UUID]
    assert contender_reservation == ([], [])
    assert multi_root_reservation == ([], [])

    # multi-root 的 A 在遇到已占用 B 后也不能留下 partial member。
    connection = sqlite3.connect(database_path)
    try:
        assert (
            connection.execute(
                """
            SELECT COUNT(*)
            FROM material_reservation_member
            WHERE material_uuid = ?
            """,
                (MATERIAL_A_UUID,),
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()

    store = WorkflowStore(database_path)
    try:
        coordinator = WorkflowRuntimeCoordinator(store)
        assert (
            coordinator.transition_job(winner_job["uuid"], "dispatched")["status"]
            == "dispatched"
        )
        for blocked_job in (contender_job, multi_root_job):
            with pytest.raises(StoreConflict):
                coordinator.transition_job(blocked_job["uuid"], "dispatched")
            assert store.get_job(blocked_job["uuid"])["status"] == "pending"
    finally:
        store.close()


def test_task_job_and_reservation_roll_back_together_on_job_insert_failure(
    tmp_path: Path,
) -> None:
    working_dir = tmp_path / "unilabos_data"
    working_dir.mkdir()
    database_path = working_dir / "workflow.db"
    composition.reset_workflow_service_for_test()
    with _open_authority(database_path) as (store, materials):
        _create_material(materials, MATERIAL_A_UUID)
        _seed_workflow(
            store,
            workflow_uuid=SINGLE_WORKFLOW_UUID,
            node_uuid=SINGLE_NODE_UUID,
            parameters=(_resource_slot_parameter("sample"),),
        )
        with store.transaction() as uow:
            # 只有 complete Reservation 已在同一 transaction 可见时才注入故障；
            # 若 Job 先于 Reservation INSERT，trigger 不会掩盖错误时序。
            uow.execute(
                """
                CREATE TRIGGER m1d_abort_job_insert
                BEFORE INSERT ON workflow_node_job
                WHEN (
                    SELECT COUNT(*)
                    FROM material_reservation
                    WHERE workflow_task_uuid = NEW.workflow_task_uuid
                      AND status = 'active'
                ) = 1
                AND (
                    SELECT COUNT(*)
                    FROM material_reservation_member AS member
                    JOIN material_reservation AS reservation
                      ON reservation.uuid = member.reservation_uuid
                    WHERE reservation.workflow_task_uuid = NEW.workflow_task_uuid
                      AND member.released_at IS NULL
                ) = 1
                BEGIN
                    SELECT RAISE(ABORT, 'm1d injected job insert failure');
                END
                """
            )

    try:
        service = composition.compose_workflow_runtime(working_dir)
        with TestClient(
            create_workflow_app(service),
            raise_server_exceptions=False,
        ) as client:
            response = _post_task(
                client,
                workflow_uuid=SINGLE_WORKFLOW_UUID,
                input_value={"sample": {"uuid": MATERIAL_A_UUID}},
            )
        assert response.status_code == 500
    finally:
        composition.reset_workflow_service_for_test()

    assert _table_count(database_path, "workflow_task") == 0
    assert _table_count(database_path, "workflow_node_job") == 0
    assert _table_count(database_path, "material_reservation") == 0
    assert _table_count(database_path, "material_reservation_member") == 0
