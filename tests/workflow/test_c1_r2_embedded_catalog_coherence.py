"""C1 R2 embedded Published Workflow facts must equal the guarded Catalog."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow.composite import CompositeAuthoring
from unilabos.workflow.store import WorkflowStore

from .test_c1_r2_catalog_order_and_handle_authority import (
    ORDER_AUTHORITY,
    ORDER_INVOCATION_UUID,
    ORDER_LEAF_WORKFLOW_UUID,
    ORDER_OUTER_WORKFLOW_UUID,
    ORDER_PARENT_WORKFLOW_UUID,
    _make_ordered_world,
)

OTHER_CONTRACT_DIGEST = "sha256:" + "c" * 64


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _leaf_template(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return next(
        template
        for template in snapshot["node_templates"]
        if template["name"] == f"workflow:{ORDER_LEAF_WORKFLOW_UUID}"
    )


def _leaf_handles(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    template_uuid = _leaf_template(snapshot)["uuid"]
    return [
        handle
        for handle in snapshot["handle_templates"]
        if handle["workflow_node_template_uuid"] == template_uuid
    ]


def _business_source(
    snapshot: Mapping[str, Any],
    data_key: str,
) -> dict[str, Any]:
    return next(
        handle
        for handle in _leaf_handles(snapshot)
        if handle["io_type"] == "source" and handle["data_key"] == data_key
    )


def _ready_target(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return next(
        handle
        for handle in _leaf_handles(snapshot)
        if handle["io_type"] == "target"
        and handle["meta_data"]["unilab"].get("structural_role") == "ready"
    )


class _EmbeddedDriftStore(WorkflowStore):
    """A public Store adapter that drifts only one returned embedded fact."""

    def __init__(
        self,
        database_path: Path,
        mutation: Callable[[dict[str, Any]], None],
    ) -> None:
        super().__init__(database_path)
        self._mutation = mutation

    def get_published_workflow_snapshot(
        self,
        workflow_uuid: str,
    ) -> dict[str, Any]:
        snapshot = super().get_published_workflow_snapshot(workflow_uuid)
        if workflow_uuid == ORDER_OUTER_WORKFLOW_UUID:
            self._mutation(snapshot)
        return snapshot


def _catalog_snapshot(world: Any) -> dict[str, Any]:
    with world.catalog.snapshot(ORDER_AUTHORITY) as snapshot:
        return {
            "fingerprint": snapshot.fingerprint,
            "nodes": _plain(snapshot.node_templates),
            "handles": _plain(snapshot.handle_templates),
        }


def _compile(world: Any, store: WorkflowStore) -> Any:
    return CompositeAuthoring(
        store=store,
        catalog=world.catalog,
        authority=ORDER_AUTHORITY,
        resolver=world.resolver,
    ).compile_invocation(
        parent_workflow_uuid=ORDER_PARENT_WORKFLOW_UUID,
        invocation_uuid=ORDER_INVOCATION_UUID,
        module=world.outer_source.module,
        symbol=world.outer_source.symbol,
        keyword_arguments={
            "zeta": {"kind": "workflow_input", "parameter": "zeta"},
            "alpha": {"kind": "workflow_input", "parameter": "alpha"},
        },
    )


def _mutate_embedded(snapshot: dict[str, Any], case: str) -> None:
    template = _leaf_template(snapshot)
    zeta = _business_source(snapshot, "zeta")
    ready = _ready_target(snapshot)
    assert zeta["meta_data"]["unilab"]["implicit_passthrough"] is True
    assert ready["meta_data"]["unilab"]["editor_control"] == "variable_selector"
    assert ready["meta_data"]["unilab"]["allowed_resource_template_uuids"] is None
    assert set(ready["meta_data"]) == {"unilab"}
    if case == "source-implicit-passthrough":
        zeta["meta_data"]["unilab"]["implicit_passthrough"] = False
    elif case == "ready-editor-control":
        ready["meta_data"]["unilab"]["editor_control"] = "evil"
    elif case == "ready-allowlist":
        ready["meta_data"]["unilab"]["allowed_resource_template_uuids"] = [
            "81000000-0000-4000-8000-000000000099"
        ]
    elif case == "handle-metadata-sibling":
        ready["meta_data"]["evil"] = True
    elif case == "contract-digest":
        extension = template["schema"]["x-unilabos-workflow-contract"]
        assert extension["contract_digest"] != OTHER_CONTRACT_DIGEST
        extension["contract_digest"] = OTHER_CONTRACT_DIGEST
    else:  # pragma: no cover - parameter table is intentionally closed
        raise AssertionError(case)


@pytest.mark.parametrize(
    "case",
    [
        "source-implicit-passthrough",
        "ready-editor-control",
        "ready-allowlist",
        "handle-metadata-sibling",
        "contract-digest",
    ],
)
def test_embedded_single_field_drift_from_current_catalog_fails_closed(
    tmp_path: Path,
    case: str,
) -> None:
    world = _make_ordered_world(tmp_path)
    drifted = _EmbeddedDriftStore(
        tmp_path / "workflow.db",
        lambda snapshot: _mutate_embedded(snapshot, case),
    )
    try:
        before = _catalog_snapshot(world)

        expansion = _compile(world, drifted)

        diagnostics = _plain(expansion.diagnostics)
        assert expansion.invocation_node is None
        assert len(diagnostics) == 1
        assert diagnostics[0]["code"] == "composite_catalog_mismatch"
        assert diagnostics[0]["severity"] == "error"
        assert _catalog_snapshot(world) == before
    finally:
        drifted.close()
        world.close()


def test_genuine_implicit_false_outputs_and_unchanged_catalog_remain_authoritative(
    tmp_path: Path,
) -> None:
    world = _make_ordered_world(tmp_path)
    try:
        before = _catalog_snapshot(world)
        outer = world.store.get_published_workflow_snapshot(ORDER_OUTER_WORKFLOW_UUID)
        assert (
            _business_source(outer, "zeta")["meta_data"]["unilab"][
                "implicit_passthrough"
            ]
            is True
        )
        assert (
            _business_source(outer, "omega")["meta_data"]["unilab"][
                "implicit_passthrough"
            ]
            is False
        )
        assert (
            _business_source(outer, "beta")["meta_data"]["unilab"][
                "implicit_passthrough"
            ]
            is False
        )

        expansion = _compile(world, world.store)

        assert _plain(expansion.diagnostics) == []
        assert expansion.invocation_node is not None
        assert _catalog_snapshot(world) == before
    finally:
        world.close()
