"""C1 R2 D-064 放宽规则必须要求权威的 Published Workflow。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from unilabos.package_manager.consumers import (
    PackageCatalogPublishedWorkflowResolver,
)
from unilabos.workflow.composite import (
    CompositeCatalogMismatch,
    project_published_workflow_contract,
)

from .c1_r2_static_expansion_fixture import (
    CHILD_WORKFLOW_UUID,
    MATERIAL_TEMPLATE_B_UUID,
    make_nested_resource_world,
    resource_slot_schema,
)
from .c1_r2_static_expansion_fixture import (
    HOST_RESOURCE_TEMPLATE_UUID as R2_HOST_RESOURCE_TEMPLATE_UUID,
)
from .test_c1_published_workflow_contract import (
    ACTION_TEMPLATE_UUID,
    ACTION_VALUE_TARGET_UUID,
    HOST_RESOURCE_TEMPLATE_UUID,
    _applied_snapshot,
    _package_catalog,
)

SPOOF_WORKFLOW_UUID = "56000000-0000-4000-8000-000000000001"
OTHER_WORKFLOW_UUID = "56000000-0000-4000-8000-000000000002"
SPOOF_MODULE = "spoof_package.workflows.child"
SPOOF_SYMBOL = "child"
SPOOF_DEFINITION_FQID = "spoof_package.workflows.child"
SPOOF_PACKAGE_DIGEST = "sha256:" + "8" * 64
SPOOF_DEFINITION_HASH = "sha256:" + "9" * 64
SPOOF_APPLIED_HASH = "sha256:" + "a" * 64
SPOOF_CONTRACT_DIGEST = "sha256:" + "b" * 64


def _source() -> Any:
    return PackageCatalogPublishedWorkflowResolver((_package_catalog(),)).resolve(
        "c1_published_lab.workflows.child",
        "prepare_sample",
    )


def _closed_workflow_extension() -> dict[str, Any]:
    return {
        "version": 1,
        "compatibility_version": 1,
        "workflow_uuid": SPOOF_WORKFLOW_UUID,
        "workflow_revision": 3,
        "applied_source_hash": SPOOF_APPLIED_HASH,
        "contract_digest": SPOOF_CONTRACT_DIGEST,
        "composition_allow_transparent": False,
        "input_order": ["value"],
        "output_order": ["value"],
    }


def _closed_workflow_provenance() -> dict[str, Any]:
    return {
        "kind": "package",
        "definition_fqid": SPOOF_DEFINITION_FQID,
        "module": SPOOF_MODULE,
        "symbol": SPOOF_SYMBOL,
        "package_catalog_digest": SPOOF_PACKAGE_DIGEST,
        "definition_content_hash": SPOOF_DEFINITION_HASH,
    }


def _spoofed_action_snapshot(case: str) -> dict[str, Any]:
    """伪造权威事实，同时保留 Action template/Handle identity。"""

    snapshot = deepcopy(_applied_snapshot())
    workflow_unilab = snapshot["workflow"]["meta_data"]["unilab"]
    workflow_unilab["input_contract"] = {
        "version": 1,
        "parameters": [
            {"name": "value", "schema": resource_slot_schema(), "required": True}
        ],
    }
    workflow_unilab["output_contract"] = {
        "version": 1,
        "outputs": [
            {
                "name": "value",
                "schema": resource_slot_schema(),
                "implicit": True,
            }
        ],
    }
    workflow_unilab["output_bindings"] = {
        "value": {"kind": "workflow_input", "parameter": "value"}
    }

    target = next(
        handle
        for handle in snapshot["handle_templates"]
        if handle["uuid"] == ACTION_VALUE_TARGET_UUID
    )
    target["type"] = "ResourceSlot"
    target["meta_data"] = {
        "unilab": {
            "value_schema": resource_slot_schema(MATERIAL_TEMPLATE_B_UUID),
            "editor_control": "material_port",
            "allowed_resource_template_uuids": [MATERIAL_TEMPLATE_B_UUID],
        }
    }

    action = snapshot["node_templates"][0]
    assert action["uuid"] == ACTION_TEMPLATE_UUID
    action.update(
        {
            "name": f"workflow:{SPOOF_WORKFLOW_UUID}",
            "class": f"{SPOOF_MODULE}:{SPOOF_SYMBOL}",
            "type": "workflow",
            "node_type": "workflow",
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "goal": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"value": resource_slot_schema()},
                        "required": ["value"],
                    },
                    "result": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"value": resource_slot_schema()},
                        "required": ["value"],
                    },
                },
                "required": ["goal", "result"],
                "x-unilabos-workflow-contract": _closed_workflow_extension(),
            },
            "meta_data": {
                "unilab": {
                    "framework_owner_only": True,
                    "workflow_source": _closed_workflow_provenance(),
                }
            },
        }
    )

    extension = action["schema"]["x-unilabos-workflow-contract"]
    provenance = action["meta_data"]["unilab"]["workflow_source"]
    if case == "wrong-name":
        action["name"] = "measure"
    elif case == "extension-arbitrary-minimal":
        action["schema"]["x-unilabos-workflow-contract"] = {"arbitrary": True}
    elif case == "extension-extra-field":
        extension["unexpected"] = True
    elif case.startswith("extension-missing-"):
        extension.pop(case.removeprefix("extension-missing-"))
    elif case == "provenance-extra-field":
        provenance["unexpected"] = True
    elif case == "provenance-kind-only":
        action["meta_data"]["unilab"]["workflow_source"] = {"kind": "package"}
    elif case.startswith("provenance-missing-"):
        provenance.pop(case.removeprefix("provenance-missing-"))
    elif case == "provenance-relative-module":
        provenance["module"] = ".child"
        action["class"] = ".child:child"
    elif case == "provenance-non-symbol":
        provenance["symbol"] = "child.run"
        action["class"] = f"{SPOOF_MODULE}:child.run"
    elif case == "class-module-symbol-mismatch":
        action["class"] = "spoof_package.workflows.other:other"
    elif case == "template-contract-workflow-uuid-mismatch":
        extension["workflow_uuid"] = OTHER_WORKFLOW_UUID
    else:  # pragma: no cover - 参数表有意保持封闭
        raise AssertionError(f"未知的非法权威用例：{case}")
    return snapshot


MALFORMED_AUTHORITY_CASES = [
    "wrong-name",
    "extension-arbitrary-minimal",
    "extension-extra-field",
    "extension-missing-version",
    "extension-missing-compatibility_version",
    "extension-missing-workflow_uuid",
    "extension-missing-workflow_revision",
    "extension-missing-applied_source_hash",
    "extension-missing-contract_digest",
    "extension-missing-composition_allow_transparent",
    "extension-missing-input_order",
    "extension-missing-output_order",
    "provenance-extra-field",
    "provenance-kind-only",
    "provenance-missing-kind",
    "provenance-missing-definition_fqid",
    "provenance-missing-module",
    "provenance-missing-symbol",
    "provenance-missing-package_catalog_digest",
    "provenance-missing-definition_content_hash",
    "provenance-relative-module",
    "provenance-non-symbol",
    "class-module-symbol-mismatch",
    "template-contract-workflow-uuid-mismatch",
]


@pytest.mark.parametrize("case", MALFORMED_AUTHORITY_CASES)
def test_malformed_action_spoof_never_receives_composite_resource_relaxation(
    case: str,
) -> None:
    snapshot = _spoofed_action_snapshot(case)

    with pytest.raises(CompositeCatalogMismatch) as caught:
        project_published_workflow_contract(
            source=_source(),
            applied_snapshot=snapshot,
            host_node_resource_template_uuid=HOST_RESOURCE_TEMPLATE_UUID,
        )

    assert caught.value.code == "composite_catalog_mismatch"
    assert caught.value.path == "/published_workflow/io_contract"


def test_complete_published_workflow_authority_still_allows_nested_narrowing(
    tmp_path: Path,
) -> None:
    world = make_nested_resource_world(
        tmp_path,
        parent_schema=resource_slot_schema(),
        direct_schema=resource_slot_schema(),
        leaf_schema=resource_slot_schema(MATERIAL_TEMPLATE_B_UUID),
    )
    try:
        projected = project_published_workflow_contract(
            source=world.child.source,
            applied_snapshot=world.store.get_published_workflow_snapshot(
                CHILD_WORKFLOW_UUID
            ),
            host_node_resource_template_uuid=R2_HOST_RESOURCE_TEMPLATE_UUID,
        )

        assert projected is not None
        assert projected.template["name"] == f"workflow:{CHILD_WORKFLOW_UUID}"
        assert projected.template["class"] == (
            f"{world.child.source.module}:{world.child.source.symbol}"
        )
        assert projected.template["meta_data"]["unilab"]["workflow_source"] == {
            "kind": "package",
            "definition_fqid": world.child.source.definition_fqid,
            "module": world.child.source.module,
            "symbol": world.child.source.symbol,
            "package_catalog_digest": world.child.source.package_catalog_digest,
            "definition_content_hash": world.child.source.definition_content_hash,
        }
    finally:
        world.close()
