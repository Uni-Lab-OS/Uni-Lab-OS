"""Phase 01 contracts for the local Backend-shaped Workflow authority."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from unilabos.workflow.composition import (
    reset_workflow_service_for_test,
    setup_workflow_service,
)
from unilabos.workflow.models import (
    WorkflowEdgeWrite,
    WorkflowNodeWrite,
)
from unilabos.workflow.service import (
    WorkflowConflict,
    WorkflowError,
    WorkflowService,
)
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
NODE_A_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
NODE_B_UUID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
EDGE_UUID = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
SOURCE_HANDLE_UUID = "11111111-aaaa-4aaa-8aaa-111111111111"
TARGET_HANDLE_UUID = "22222222-bbbb-4bbb-8bbb-222222222222"
TEMPLATE_A_UUID = "33333333-aaaa-4aaa-8aaa-333333333333"
TEMPLATE_B_UUID = "44444444-bbbb-4bbb-8bbb-444444444444"
RESOURCE_TEMPLATE_UUID = "55555555-5555-4555-8555-555555555555"


def _node(node_uuid: str, name: str) -> WorkflowNodeWrite:
    return WorkflowNodeWrite(
        uuid=node_uuid,
        workflow_node_template_uuid=(
            TEMPLATE_A_UUID if node_uuid == NODE_A_UUID else TEMPLATE_B_UUID
        ),
        name=name,
        status="idle",
        type="compute",
        pose={},
        param={},
        execution_policy={},
        disabled=False,
        minimized=False,
        meta_data={},
    )


def _edge() -> WorkflowEdgeWrite:
    return WorkflowEdgeWrite(
        uuid=EDGE_UUID,
        source_node_uuid=NODE_A_UUID,
        target_node_uuid=NODE_B_UUID,
        source_handle_uuid=SOURCE_HANDLE_UUID,
        target_handle_uuid=TARGET_HANDLE_UUID,
        meta_data={},
    )


def _seed_template_catalog(store: WorkflowStore) -> None:
    timestamp = "2026-07-30T00:00:00Z"
    with store.transaction() as connection:
        for template_uuid, name in (
            (TEMPLATE_A_UUID, "source"),
            (TEMPLATE_B_UUID, "target"),
        ):
            connection.execute(
                """
                INSERT INTO workflow_node_template(
                    uuid, create_time, update_time, meta_data, authority_id,
                    resource_template_uuid, name, display_name, goal,
                    goal_default, feedback, result, type, node_type
                ) VALUES (?, ?, ?, '{}', 'os-local', ?, ?, ?, '{}', '{}',
                          '{}', '{}', 'action', 'compute')
                """,
                (
                    template_uuid,
                    timestamp,
                    timestamp,
                    RESOURCE_TEMPLATE_UUID,
                    name,
                    name,
                ),
            )
        for handle_uuid, template_uuid, key, io_type in (
            (
                SOURCE_HANDLE_UUID,
                TEMPLATE_A_UUID,
                "result",
                "source",
            ),
            (
                TARGET_HANDLE_UUID,
                TEMPLATE_B_UUID,
                "value",
                "target",
            ),
        ):
            connection.execute(
                """
                INSERT INTO workflow_handle_template(
                    uuid, create_time, update_time, meta_data, authority_id,
                    workflow_node_template_uuid, handle_key, io_type,
                    display_name, type, required, data_key
                ) VALUES (?, ?, ?, '{}', 'os-local', ?, ?, ?, ?, 'any', 0, ?)
                """,
                (
                    handle_uuid,
                    timestamp,
                    timestamp,
                    template_uuid,
                    key,
                    io_type,
                    key,
                    key,
                ),
            )


@pytest.fixture()
def store(tmp_path: Path) -> WorkflowStore:
    opened = WorkflowStore(tmp_path / "workflow.db")
    _seed_template_catalog(opened)
    yield opened
    opened.close()


@pytest.fixture()
def service(store: WorkflowStore) -> WorkflowService:
    return WorkflowService(store)


def test_graph_revision_conflict_is_atomic_and_reconcile_preserves_uuid(
    service: WorkflowService,
) -> None:
    created = service.create_workflow(
        name="phase-01",
        tags=["migration"],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )
    assert created["uuid"] == WORKFLOW_UUID
    assert created["revision"] == 1

    first = service.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=[_node(NODE_A_UUID, "first")],
        edges=[],
    )
    assert first["workflow"]["revision"] == 2
    assert [node["uuid"] for node in first["nodes"]] == [NODE_A_UUID]
    assert service._store.count_rows("workflow_task") == 0

    with pytest.raises(WorkflowConflict) as conflict:
        service.save_graph(
            WORKFLOW_UUID,
            revision=1,
            nodes=[_node(NODE_A_UUID, "stale overwrite")],
            edges=[],
        )
    assert conflict.value.code == "conflict"
    after_conflict = service.get_graph(WORKFLOW_UUID)
    assert after_conflict["workflow"]["revision"] == 2
    assert after_conflict["nodes"][0]["name"] == "first"

    second = service.save_graph(
        WORKFLOW_UUID,
        revision=2,
        nodes=[
            _node(NODE_A_UUID, "updated in place"),
            _node(NODE_B_UUID, "second"),
        ],
        edges=[_edge()],
    )
    assert second["workflow"]["revision"] == 3
    assert [node["uuid"] for node in second["nodes"]] == [
        NODE_A_UUID,
        NODE_B_UUID,
    ]
    assert second["edges"][0]["uuid"] == EDGE_UUID
    assert second["nodes"][0]["create_time"] == first["nodes"][0]["create_time"]

    reconciled = service.save_graph(
        WORKFLOW_UUID,
        revision=3,
        nodes=[_node(NODE_B_UUID, "only remaining node")],
        edges=[],
    )
    assert reconciled["workflow"]["revision"] == 4
    assert [node["uuid"] for node in reconciled["nodes"]] == [NODE_B_UUID]
    assert reconciled["edges"] == []
    assert service._store.count_rows("workflow_node", include_deleted=True) == 2
    assert service._store.count_rows("workflow_edge", include_deleted=True) == 1


def test_task_snapshot_and_pending_jobs_are_created_in_one_transaction(
    service: WorkflowService,
) -> None:
    service.create_workflow(
        name="snapshot",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )
    service.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=[
            _node(NODE_A_UUID, "first"),
            _node(NODE_B_UUID, "second"),
        ],
        edges=[_edge()],
    )

    task = service.create_workflow_task(
        workflow_uuid=WORKFLOW_UUID,
        run_mode="normal",
        target_node_uuid=None,
        input_value={},
        description=None,
        meta_data={},
    )
    assert task["uuid"] != WORKFLOW_UUID
    assert task["workflow_uuid"] == WORKFLOW_UUID
    assert task["status"] == "pending"
    assert task["control_status"] == "active"
    assert task["cleanup_status"] == "none"
    assert task["workflow_snapshot"]["workflow"]["revision"] == 2

    jobs = service.list_workflow_node_jobs(task["uuid"])
    assert [job["workflow_node_uuid"] for job in jobs] == [
        NODE_A_UUID,
        NODE_B_UUID,
    ]
    assert [job["topological_index"] for job in jobs] == [0, 1]
    assert {job["status"] for job in jobs} == {"pending"}

    service.save_graph(
        WORKFLOW_UUID,
        revision=2,
        nodes=[_node(NODE_A_UUID, "changed after task creation")],
        edges=[],
    )
    persisted = service.get_workflow_task(task["uuid"])
    assert persisted["workflow_snapshot"] == task["workflow_snapshot"]
    assert len(persisted["workflow_snapshot"]["nodes"]) == 2


def test_workflow_graph_task_and_jobs_survive_store_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "workflow.db"
    first = WorkflowStore(db_path)
    _seed_template_catalog(first)
    service = WorkflowService(first)
    service.create_workflow(
        name="restart",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )
    service.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=[_node(NODE_A_UUID, "persistent")],
        edges=[],
    )
    task = service.create_workflow_task(
        workflow_uuid=WORKFLOW_UUID,
        run_mode="normal",
        target_node_uuid=None,
        input_value={},
        description=None,
        meta_data={},
    )
    first.close()

    reopened = WorkflowStore(db_path)
    reopened_service = WorkflowService(reopened)
    assert reopened_service.get_graph(WORKFLOW_UUID)["nodes"][0]["uuid"] == (
        NODE_A_UUID
    )
    assert reopened_service.get_workflow_task(task["uuid"])["uuid"] == task["uuid"]
    assert len(reopened_service.list_workflow_node_jobs(task["uuid"])) == 1
    reopened.close()


def test_public_models_reject_non_uuid_execution_identities() -> None:
    with pytest.raises(ValueError):
        _node("legacy-node-id", "invalid")
    assert UUID(NODE_A_UUID).version == 4


def test_phase_01_does_not_invent_unfrozen_task_input_shape(
    service: WorkflowService,
) -> None:
    service.create_workflow(
        name="input-gate",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )
    service.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=[_node(NODE_A_UUID, "node")],
        edges=[],
    )

    with pytest.raises(WorkflowError) as failure:
        service.create_workflow_task(
            workflow_uuid=WORKFLOW_UUID,
            run_mode="normal",
            target_node_uuid=None,
            input_value={"unfrozen": "value"},
            description=None,
            meta_data={},
        )

    assert failure.value.code == "invalid_input"
    assert service._store.count_rows("workflow_task") == 0


def test_workspace_composition_owns_one_fixed_workflow_database(
    tmp_path: Path,
) -> None:
    working_dir = tmp_path / "unilabos_data"
    try:
        first = setup_workflow_service(working_dir)
        second = setup_workflow_service(working_dir)

        assert first is second
        assert Path(first._store.path) == working_dir / "workflow.db"
        assert (working_dir / "workflow.db").is_file()
        with pytest.raises(RuntimeError):
            setup_workflow_service(tmp_path / "another_workspace")
    finally:
        reset_workflow_service_for_test()
