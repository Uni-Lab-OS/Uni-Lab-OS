"""C1 R1 reviewer findings：public contract projection 回归 RED。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from unilabos.package_manager.consumers import (
    PackageCatalogPublishedWorkflowResolver,
)
from unilabos.registry.catalog_consumer import (
    workflow_template_imports_from_registry_snapshot,
)
from unilabos.workflow.catalog import TemplateCatalog
from unilabos.workflow.composite import project_published_workflow_contract
from unilabos.workflow.store import WorkflowStore

from .test_c1_catalog_publication_lifecycle import (
    _registry_snapshot,
    _StaticResourceTemplateIdentityIndex,
)
from .test_c1_published_workflow_contract import (
    AUTHORITY,
    HOST_RESOURCE_TEMPLATE_UUID,
    _applied_snapshot,
    _package_catalog,
    _plain,
)

COLLECTION_RESOURCE_TEMPLATE_UUID = "72000000-0000-4000-8000-000000000001"


def _source() -> Any:
    return PackageCatalogPublishedWorkflowResolver((_package_catalog(),)).resolve(
        "c1_published_lab.workflows.child",
        "prepare_sample",
    )


def _project(snapshot: dict[str, Any]) -> Any:
    return project_published_workflow_contract(
        source=_source(),
        applied_snapshot=snapshot,
        host_node_resource_template_uuid=HOST_RESOURCE_TEMPLATE_UUID,
    )


def _handles(projected: Any) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (str(handle["handle_key"]), str(handle["io_type"])): _plain(handle)
        for handle in projected.handles
    }


def test_published_ready_handles_match_registry_a1_structural_shape() -> None:
    published = _handles(_project(_applied_snapshot()))
    identity_index = _StaticResourceTemplateIdentityIndex(include_host=True)
    registry_imports = workflow_template_imports_from_registry_snapshot(
        _registry_snapshot(include_host=True),
        authority_id=AUTHORITY.authority_id,
        resource_template_identity_resolver=identity_index.resolve_symbol,
    )
    action = next(
        item for item in registry_imports if item.template["name"] == "measure"
    )
    registry_ready = {
        (str(handle["handle_key"]), str(handle["io_type"])): _plain(handle)
        for handle in action.handles
        if handle["handle_key"] == "ready"
    }

    for key in (("ready", "target"), ("ready", "source")):
        assert (
            {
                field: published[key][field]
                for field in ("type", "required", "data_source", "data_key")
            }
            == {
                field: registry_ready[key][field]
                for field in ("type", "required", "data_source", "data_key")
            }
            == {
                "type": "boolean",
                "required": False,
                "data_source": "dependency",
                "data_key": "ready",
            }
        )
        assert published[key]["meta_data"] == registry_ready[key]["meta_data"]


def test_nullable_resource_slot_collection_keeps_array_material_handle_contract() -> (
    None
):
    value_schema = {
        "anyOf": [
            {
                "type": "array",
                "items": {
                    "$slot": "ResourceSlot",
                    "allowed_resource_template_uuids": [
                        COLLECTION_RESOURCE_TEMPLATE_UUID
                    ],
                },
            },
            {"type": "null"},
        ]
    }
    snapshot = _applied_snapshot()
    unilab = snapshot["workflow"]["meta_data"]["unilab"]
    unilab["input_contract"] = {
        "version": 1,
        "parameters": [
            {
                "name": "samples",
                "schema": deepcopy(value_schema),
                "required": False,
                "default": None,
            }
        ],
    }
    unilab["output_contract"] = {
        "version": 1,
        "outputs": [
            {
                "name": "samples",
                "schema": deepcopy(value_schema),
                "implicit": True,
            }
        ],
    }
    unilab["output_bindings"] = {
        "samples": {"kind": "workflow_input", "parameter": "samples"}
    }
    snapshot["nodes"] = []
    snapshot["edges"] = []
    snapshot["node_templates"] = []
    snapshot["handle_templates"] = []

    handles = _handles(_project(snapshot))

    for key in (("samples", "target"), ("samples", "source")):
        handle = handles[key]
        assert handle["type"] == "array"
        assert handle["meta_data"]["unilab"] == {
            "value_schema": value_schema,
            "editor_control": "material_port",
            "allowed_resource_template_uuids": [COLLECTION_RESOURCE_TEMPLATE_UUID],
            "implicit_passthrough": key[1] == "source",
        }


def _with_presentation(
    snapshot: dict[str, Any],
    *,
    input_title: str,
    output_title: str,
) -> dict[str, Any]:
    changed = deepcopy(snapshot)
    unilab = changed["workflow"]["meta_data"]["unilab"]
    unilab["input_contract"]["parameters"][0].update(
        {
            "title": input_title,
            "description": f"{input_title} description",
        }
    )
    unilab["output_contract"]["outputs"][0].update(
        {
            "title": output_title,
            "description": f"{output_title} description",
        }
    )
    return changed


def test_presentation_changes_handles_and_catalog_but_not_contract_digest(
    tmp_path: Path,
) -> None:
    first_import = _project(
        _with_presentation(
            _applied_snapshot(),
            input_title="Measured value",
            output_title="Computed result",
        )
    )
    second_import = _project(
        _with_presentation(
            _applied_snapshot(),
            input_title="输入数值",
            output_title="输出结果",
        )
    )
    first_extension = first_import.template["schema"]["x-unilabos-workflow-contract"]
    second_extension = second_import.template["schema"]["x-unilabos-workflow-contract"]
    assert first_extension["contract_digest"] == second_extension["contract_digest"]

    first_handles = _handles(first_import)
    second_handles = _handles(second_import)
    assert (
        first_handles[("value", "target")]["display_name"],
        first_handles[("value", "target")]["description"],
    ) == ("Measured value", "Measured value description")
    assert (
        second_handles[("value", "target")]["display_name"],
        second_handles[("value", "target")]["description"],
    ) == ("输入数值", "输入数值 description")
    assert (
        first_handles[("result", "source")]["display_name"],
        second_handles[("result", "source")]["display_name"],
    ) == ("Computed result", "输出结果")

    store = WorkflowStore(tmp_path / "workflow.db")
    try:
        catalog = TemplateCatalog(store)
        first = catalog.replace(AUTHORITY, (first_import,))
        second = catalog.replace(AUTHORITY, (second_import,))
        assert first.fingerprint != second.fingerprint
        assert [item["uuid"] for item in first.node_templates] == [
            item["uuid"] for item in second.node_templates
        ]
        assert [item["uuid"] for item in first.handle_templates] == [
            item["uuid"] for item in second.handle_templates
        ]
    finally:
        store.close()
