"""精确任意 DAG sidecar 的作者入口与 managed 启动生命周期合同。"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from unilabos.workflow.exact_graph_sidecar import (
    ExactGraphSidecarError,
    apply_declared_exact_graph,
    build_exact_graph_from_live,
)
from unilabos.workflow.models import WorkflowNodeWrite
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.source_discovery import (
    EditableSourceDiscoveryPlan,
    EditableSourceRegistration,
)
from unilabos.workflow.store import WorkflowStore

from .test_authoring_engine import WORKFLOW_UUID, _engine, _source

NODE_UUID = "20000000-0000-4000-8000-000000000001"
def _exact_node(*, sidecar: bool) -> dict[str, Any]:
    """Build one semantically identical live/sidecar node with distinct display data."""

    executor = {"mode": "fixed", "device_id": "robot-a"}
    metadata: dict[str, Any] = {
        "unilab": {
            "executor_binding": executor,
            "owner": "sidecar-must-not-overwrite" if sidecar else "server",
        }
    }
    if sidecar:
        metadata["bioyond"] = {
            "material_transfer": {"hardware_executable": False}
        }
    else:
        metadata["live_marker"] = {"server_owned": True}
    return {
        "uuid": NODE_UUID,
        "workflow_node_template_uuid": None,
        "parent_uuid": None,
        "material_uuid": None,
        "name": "material transfer",
        "type": "group",
        "icon": None,
        "pose": {},
        "param": {"resource": {"uuid": NODE_UUID}},
        "footer": None,
        "action_name": None,
        "action_type": None,
        "execution_policy": {},
        "disabled": False,
        "minimized": False,
        "script": None,
        "description": "Virtual-only: uncalibrated" if sidecar else "live template",
        "meta_data": metadata,
    }


def _exact_document(*, sidecar: bool) -> dict[str, Any]:
    """Build a closed five-set graph around the lifecycle test node."""

    return {
        "workflow": {"uuid": WORKFLOW_UUID, "meta_data": {}},
        "nodes": [_exact_node(sidecar=sidecar)],
        "edges": [],
        "node_templates": [],
        "handle_templates": [],
    }


def test_public_apply_rejects_exact_graph_source_before_graph_mutation(
    tmp_path: Path,
) -> None:
    """只有 managed 固定点可以发布 serial seed 与精确 sidecar。"""

    package_root = tmp_path / "package"
    workflows = package_root / "workflows"
    workflows.mkdir(parents=True)
    source_path = workflows / "sample.py"
    source_path.write_text("", encoding="utf-8")
    exact_path = workflows / "sample.exact.json"
    exact_path.write_text("{}\n", encoding="utf-8")
    metadata = package_root.lstat()
    root_identity = (metadata.st_dev, metadata.st_ino)
    service = WorkflowService(
        WorkflowStore(tmp_path / "workflow.db"),
        compiler=_engine(),
    )
    service.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="Exact package workflow",
        tags=[],
        description=None,
        meta_data={},
    )
    service.replace_discovered_source_authorizations(
        EditableSourceDiscoveryPlan(
            registrations=(
                EditableSourceRegistration(
                    workflow_uuid=WORKFLOW_UUID,
                    package_id="lab",
                    package_root=package_root,
                    relative_path="workflows/sample.py",
                    source_uri="package://lab/workflows/sample.py",
                    exact_graph_relative_path="workflows/sample.exact.json",
                    exact_graph_content_hash=f"sha256:{'0' * 64}",
                    package_root_identity=root_identity,
                ),
            ),
            root_identities=((package_root, root_identity),),
        )
    )
    try:
        initial_authoring = service.get_authoring(WORKFLOW_UUID)
        assert initial_authoring["topology_authoring"] == {
            "authority": "managed_exact_graph",
            "graph_mode": "read_only",
            "graph_to_python": "unsupported",
        }
        current_draft = initial_authoring["draft"]
        draft = service.save_draft(
            WORKFLOW_UUID,
            python_source=_source(),
            expected_draft_hash=current_draft["draft_hash"],
            expected_workflow_revision=1,
        )
        candidate = draft["candidate"]
        assert candidate is not None
        before_graph = service.get_graph(WORKFLOW_UUID)

        with pytest.raises(WorkflowError) as caught:
            service.apply_authoring(
                WORKFLOW_UUID,
                candidate_hash=candidate["candidate_hash"],
            )

        assert caught.value.code == "candidate_invalid"
        assert service.get_graph(WORKFLOW_UUID) == before_graph
        assert service.get_authoring(WORKFLOW_UUID)["candidate"] is not None
    finally:
        service.close()


def test_exact_sidecar_node_display_metadata_survives_public_save_and_cold_start(
    tmp_path: Path,
) -> None:
    """Trusted node display fields survive first apply and an idempotent cold start."""

    package_root = tmp_path / "package"
    exact_path = package_root / "workflows" / "sample.exact.json"
    exact_path.parent.mkdir(parents=True)
    exact_payload = json.dumps(
        _exact_document(sidecar=True),
        sort_keys=True,
    ).encode("utf-8")
    exact_path.write_bytes(exact_payload)
    metadata = package_root.lstat()
    registration = SimpleNamespace(
        workflow_uuid=WORKFLOW_UUID,
        package_root=package_root,
        package_root_identity=(metadata.st_dev, metadata.st_ino),
        exact_graph_relative_path="workflows/sample.exact.json",
        exact_graph_content_hash=f"sha256:{hashlib.sha256(exact_payload).hexdigest()}",
    )
    database = tmp_path / "workflow.db"
    store = WorkflowStore(database)
    service = WorkflowService(store, compiler=_engine())
    service.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="Exact package workflow",
        tags=[],
        description=None,
        meta_data={},
    )
    live_node = _exact_node(sidecar=False)
    store.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=[WorkflowNodeWrite.model_validate(live_node)],
        edges=[],
        protect_reserved_metadata=False,
        validate_workflow_io_contract=True,
    )
    try:
        first = apply_declared_exact_graph(
            service=service,
            registration=registration,
        )
        first_node = service.get_graph(WORKFLOW_UUID)["nodes"][0]
        assert first["status"] == "applied"
        assert first_node["description"] == "Virtual-only: uncalibrated"
        assert first_node["meta_data"]["bioyond"] == {
            "material_transfer": {"hardware_executable": False}
        }
        assert first_node["meta_data"]["unilab"] == live_node["meta_data"][
            "unilab"
        ]
        assert "live_marker" not in first_node["meta_data"]
    finally:
        service.close()

    repeated = WorkflowService(WorkflowStore(database), compiler=_engine())
    try:
        receipt = apply_declared_exact_graph(
            service=repeated,
            registration=registration,
        )
        repeated_node = repeated.get_graph(WORKFLOW_UUID)["nodes"][0]
        assert receipt["status"] == "unchanged"
        assert repeated_node["description"] == "Virtual-only: uncalibrated"
        assert repeated_node["meta_data"]["bioyond"]["material_transfer"][
            "hardware_executable"
        ] is False
        assert repeated_node["meta_data"]["unilab"] == live_node["meta_data"][
            "unilab"
        ]
    finally:
        repeated.close()


def test_exact_sidecar_rejects_node_semantic_mutation_before_display_merge() -> None:
    """Writable display data never weakens the closed node semantic check."""

    live = _exact_document(sidecar=False)
    before = copy.deepcopy(live)
    sidecar = _exact_document(sidecar=True)
    sidecar["nodes"][0]["action_name"] = "wrong_action"

    with pytest.raises(ExactGraphSidecarError) as caught:
        build_exact_graph_from_live(
            workflow_uuid=WORKFLOW_UUID,
            sidecar=sidecar,
            live_graph=live,
        )
    assert caught.value.code == "exact_graph_node_semantics_mismatch"
    assert live == before
