"""C1 R1 Published Workflow Contract 的公共 tests-only RED。

测试只依赖冻结的 public resolver/projection seam 与现有 TemplateCatalog；不读取
Package 源文件，不 import/exec Workflow module，也不触碰 projection 私有 helper。
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from unilabos.workflow.composite import project_published_workflow_contract

from unilabos.package_manager import (
    DefinitionCatalog,
    DefinitionRecord,
    DistributionIdentity,
    PackageCatalog,
)
from unilabos.package_manager.consumers import (
    PackageCatalogPublishedWorkflowResolver,
)
from unilabos.workflow.catalog import (
    CatalogAuthority,
    NodeTemplateImport,
    TemplateCatalog,
    TemplateCatalogMismatch,
)
from unilabos.workflow.store import WorkflowStore

AUTHORITY = CatalogAuthority(authority_id="os-c1-r1", kind="local")
WORKFLOW_UUID = "51000000-0000-4000-8000-000000000001"
HOST_RESOURCE_TEMPLATE_UUID = "52000000-0000-4000-8000-000000000001"
ACTION_RESOURCE_TEMPLATE_UUID = "52000000-0000-4000-8000-000000000002"
ACTION_TEMPLATE_UUID = "53000000-0000-4000-8000-000000000001"
ACTION_READY_TARGET_UUID = "54000000-0000-4000-8000-000000000001"
ACTION_READY_SOURCE_UUID = "54000000-0000-4000-8000-000000000002"
ACTION_VALUE_TARGET_UUID = "54000000-0000-4000-8000-000000000003"
ACTION_VALUE_SOURCE_UUID = "54000000-0000-4000-8000-000000000004"
ACTION_NODE_UUID = "55000000-0000-4000-8000-000000000001"
DEFINITION_CONTENT_HASH = "sha256:" + "1" * 64
PACKAGE_CATALOG_DIGEST = (
    "sha256:2298f7eabe6c5fe362929ac65d49fbc65326c8c7e8a202d4d52ed9e926a49234"
)
APPLIED_SOURCE_HASH = "sha256:" + "3" * 64
CONTRACT_DIGEST = (
    "sha256:689aaac733eba27d13279d242a71fc3c8bc41f0c144d41261dc160a52b46a1cf"
)


def _package_catalog(
    *, module: str = "c1_published_lab.workflows.child"
) -> PackageCatalog:
    catalog = PackageCatalog.create(
        distribution=DistributionIdentity(
            name="c1-published-lab",
            normalized_name="c1-published-lab",
            version="1.0.0",
            requires_python=">=3.11",
        ),
        import_package="c1_published_lab",
        namespace="community.c1_published_lab",
        definitions=DefinitionCatalog(
            workflows=(
                DefinitionRecord(
                    kind="workflow",
                    id="prepare_sample",
                    fqid="c1_published_lab.workflows.prepare_sample",
                    module=module,
                    symbol="prepare_sample",
                    declaring_file="c1_published_lab/workflows/child.py",
                    content_hash=DEFINITION_CONTENT_HASH,
                    displayname="Published sample preparation",
                    description="C1 R1 fixture",
                    details={
                        "workflow_uuid": WORKFLOW_UUID,
                        "source_uri": ("package://c1_published_lab/workflows/child.py"),
                    },
                ),
            )
        ),
        content_digest="sha256:" + "2" * 64,
    )
    assert catalog.catalog_digest == PACKAGE_CATALOG_DIGEST
    return catalog


def _applied_snapshot() -> dict[str, Any]:
    timestamp = "2026-08-02T00:00:00Z"
    input_descriptor = {
        "name": "value",
        "schema": {"type": "number"},
        "required": True,
    }
    output_descriptor = {
        "name": "result",
        "schema": {"type": "number"},
        "implicit": False,
    }
    return {
        "workflow": {
            "uuid": WORKFLOW_UUID,
            "revision": 7,
            "name": "Published sample preparation",
            "tags": [],
            "description": "C1 R1 fixture",
            "create_time": timestamp,
            "update_time": timestamp,
            "meta_data": {
                "unilab": {
                    "input_contract": {
                        "version": 1,
                        "parameters": [input_descriptor],
                    },
                    "output_contract": {
                        "version": 1,
                        "outputs": [output_descriptor],
                    },
                    "output_bindings": {
                        "result": {
                            "kind": "node_output",
                            "workflow_node_uuid": ACTION_NODE_UUID,
                            "source_handle_uuid": ACTION_VALUE_SOURCE_UUID,
                        }
                    },
                }
            },
        },
        "applied_source": {
            "workflow_revision": 7,
            "source_hash": APPLIED_SOURCE_HASH,
            "python_source": "def prepare_sample(*, value: float): ...\n",
            "source_map": [],
            "compiler_version": "c1-fixture",
            "template_catalog_fingerprint": "sha256:" + "4" * 64,
        },
        "nodes": [
            {
                "uuid": ACTION_NODE_UUID,
                "workflow_uuid": WORKFLOW_UUID,
                "workflow_node_template_uuid": ACTION_TEMPLATE_UUID,
                "parent_uuid": None,
                "name": "measure",
                "status": "idle",
                "type": "device",
                "pose": {},
                "param": {},
                "execution_policy": {},
                "disabled": False,
                "minimized": False,
                "meta_data": {
                    "unilab": {
                        "input_bindings": {
                            ACTION_VALUE_TARGET_UUID: {"parameter": "value"}
                        }
                    }
                },
                "create_time": timestamp,
                "update_time": timestamp,
            }
        ],
        "edges": [],
        "node_templates": [
            {
                "uuid": ACTION_TEMPLATE_UUID,
                "resource_template_uuid": ACTION_RESOURCE_TEMPLATE_UUID,
                "name": "measure",
                "display_name": "Measure",
                "description": "",
                "meta_data": {},
                "goal": {},
                "goal_default": {},
                "feedback": {},
                "result": {},
                "schema": None,
                "type": "action",
                "node_type": "device",
            }
        ],
        "handle_templates": [
            _handle(ACTION_VALUE_TARGET_UUID, "value", "target", "number", True),
            _handle(ACTION_READY_TARGET_UUID, "ready", "target", "any", False),
            _handle(ACTION_VALUE_SOURCE_UUID, "result", "source", "number", False),
            _handle(ACTION_READY_SOURCE_UUID, "ready", "source", "any", False),
        ],
    }


def _handle(
    handle_uuid: str,
    key: str,
    io_type: str,
    value_type: str,
    required: bool,
) -> dict[str, Any]:
    return {
        "uuid": handle_uuid,
        "workflow_node_template_uuid": ACTION_TEMPLATE_UUID,
        "handle_key": key,
        "io_type": io_type,
        "display_name": key.title(),
        "description": "",
        "type": value_type,
        "required": required,
        "data_source": "dependency" if key == "ready" else "executor",
        "data_key": key,
        "meta_data": {"unilab": {"value_schema": {"type": value_type}}},
    }


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _framework_import(name: str, node_type: str) -> NodeTemplateImport:
    return NodeTemplateImport(
        template={
            "resource_template_uuid": HOST_RESOURCE_TEMPLATE_UUID,
            "name": name,
            "display_name": name.replace("_", " ").title(),
            "description": "fixture",
            "class": f"unilabos.workflow.authoring:{name}",
            "goal": {},
            "goal_default": {},
            "feedback": {},
            "result": {},
            "schema": None,
            "type": node_type,
            "node_type": node_type,
            "meta_data": {"unilab": {"framework_owner_only": True}},
        },
        handles=(),
    )


def test_package_catalog_resolver_returns_one_frozen_static_source() -> None:
    resolver = PackageCatalogPublishedWorkflowResolver((_package_catalog(),))

    source = resolver.resolve(
        "c1_published_lab.workflows.child",
        "prepare_sample",
    )

    assert source.workflow_uuid == WORKFLOW_UUID
    assert source.definition_fqid == "c1_published_lab.workflows.prepare_sample"
    assert source.module == "c1_published_lab.workflows.child"
    assert source.symbol == "prepare_sample"
    assert source.package_catalog_digest == PACKAGE_CATALOG_DIGEST
    assert source.definition_content_hash == DEFINITION_CONTENT_HASH


def test_package_catalog_resolver_rejects_missing_duplicate_and_dynamic_identity() -> (
    None
):
    resolver = PackageCatalogPublishedWorkflowResolver((_package_catalog(),))
    with pytest.raises(LookupError):
        resolver.resolve("c1_published_lab.workflows.missing", "missing")

    with pytest.raises(ValueError):
        PackageCatalogPublishedWorkflowResolver(
            (_package_catalog(), deepcopy(_package_catalog()))
        )

    with pytest.raises(ValueError):
        PackageCatalogPublishedWorkflowResolver((_package_catalog(module=".child"),))


def test_applied_workflow_projects_exact_owner_schema_digest_and_provenance() -> None:
    source = PackageCatalogPublishedWorkflowResolver((_package_catalog(),)).resolve(
        "c1_published_lab.workflows.child",
        "prepare_sample",
    )

    projected = project_published_workflow_contract(
        source=source,
        applied_snapshot=_applied_snapshot(),
        host_node_resource_template_uuid=HOST_RESOURCE_TEMPLATE_UUID,
    )

    template = _plain(projected.template)
    assert template["resource_template_uuid"] == HOST_RESOURCE_TEMPLATE_UUID
    assert template["name"] == f"workflow:{WORKFLOW_UUID}"
    assert (template["type"], template["node_type"]) == ("workflow", "workflow")
    assert template["meta_data"]["unilab"]["framework_owner_only"] is True
    assert template["meta_data"]["unilab"]["workflow_source"] == {
        "kind": "package",
        "definition_fqid": "c1_published_lab.workflows.prepare_sample",
        "module": "c1_published_lab.workflows.child",
        "symbol": "prepare_sample",
        "package_catalog_digest": PACKAGE_CATALOG_DIGEST,
        "definition_content_hash": DEFINITION_CONTENT_HASH,
    }
    extension = template["schema"]["x-unilabos-workflow-contract"]
    assert extension == {
        "version": 1,
        "compatibility_version": 1,
        "workflow_uuid": WORKFLOW_UUID,
        "workflow_revision": 7,
        "applied_source_hash": APPLIED_SOURCE_HASH,
        "contract_digest": CONTRACT_DIGEST,
        "composition_allow_transparent": False,
        "input_order": ["value"],
        "output_order": ["result"],
    }


def test_projection_emits_i1_value_handles_and_separate_ready_handles() -> None:
    source = PackageCatalogPublishedWorkflowResolver((_package_catalog(),)).resolve(
        "c1_published_lab.workflows.child",
        "prepare_sample",
    )
    projected = project_published_workflow_contract(
        source=source,
        applied_snapshot=_applied_snapshot(),
        host_node_resource_template_uuid=HOST_RESOURCE_TEMPLATE_UUID,
    )

    handles = [
        {
            "key": item["handle_key"],
            "io": item["io_type"],
            "type": item["type"],
            "required": item["required"],
            "data_source": item["data_source"],
        }
        for item in map(_plain, projected.handles)
    ]
    assert handles == [
        {
            "key": "value",
            "io": "target",
            "type": "number",
            "required": True,
            "data_source": "goal",
        },
        {
            "key": "result",
            "io": "source",
            "type": "number",
            "required": False,
            "data_source": "result",
        },
        {
            "key": "ready",
            "io": "target",
            "type": "any",
            "required": False,
            "data_source": "dependency",
        },
        {
            "key": "ready",
            "io": "source",
            "type": "any",
            "required": False,
            "data_source": "dependency",
        },
    ]


def test_unapplied_or_stale_workflow_is_not_projected() -> None:
    source = PackageCatalogPublishedWorkflowResolver((_package_catalog(),)).resolve(
        "c1_published_lab.workflows.child",
        "prepare_sample",
    )
    for applied_source in (
        None,
        {**_applied_snapshot()["applied_source"], "workflow_revision": 6},
    ):
        snapshot = _applied_snapshot()
        snapshot["applied_source"] = applied_source
        assert (
            project_published_workflow_contract(
                source=source,
                applied_snapshot=snapshot,
                host_node_resource_template_uuid=HOST_RESOURCE_TEMPLATE_UUID,
            )
            is None
        )


def test_complete_replace_keeps_framework_identities_when_workflow_is_added(
    tmp_path: Path,
) -> None:
    source = PackageCatalogPublishedWorkflowResolver((_package_catalog(),)).resolve(
        "c1_published_lab.workflows.child",
        "prepare_sample",
    )
    workflow_import = project_published_workflow_contract(
        source=source,
        applied_snapshot=_applied_snapshot(),
        host_node_resource_template_uuid=HOST_RESOURCE_TEMPLATE_UUID,
    )
    assert workflow_import is not None
    base = (
        _framework_import("transfer", "device"),
        _framework_import("material_source", "material_source"),
        _framework_import("group", "group"),
    )
    store = WorkflowStore(tmp_path / "workflow.db")
    try:
        catalog = TemplateCatalog(store)
        before = catalog.replace(AUTHORITY, base)
        before_ids = {item["name"]: item["uuid"] for item in before.node_templates}

        after = catalog.replace(AUTHORITY, (*base, workflow_import))
        after_ids = {item["name"]: item["uuid"] for item in after.node_templates}

        assert set(after_ids) == {
            "transfer",
            "material_source",
            "group",
            f"workflow:{WORKFLOW_UUID}",
        }
        assert {name: after_ids[name] for name in before_ids} == before_ids
    finally:
        store.close()


def test_missing_host_owner_fails_without_replacing_the_previous_catalog(
    tmp_path: Path,
) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    try:
        catalog = TemplateCatalog(store)
        base = (_framework_import("group", "group"),)
        before = catalog.replace(AUTHORITY, base)
        source = PackageCatalogPublishedWorkflowResolver((_package_catalog(),)).resolve(
            "c1_published_lab.workflows.child",
            "prepare_sample",
        )

        with pytest.raises(TemplateCatalogMismatch) as caught:
            project_published_workflow_contract(
                source=source,
                applied_snapshot=_applied_snapshot(),
                host_node_resource_template_uuid=None,
            )

        assert caught.value.code == "template_catalog_mismatch"
        assert caught.value.path == "/host_node/resource_template_uuid"
        with catalog.snapshot(AUTHORITY) as current:
            assert current.fingerprint == before.fingerprint
            assert [item["name"] for item in current.node_templates] == ["group"]
    finally:
        store.close()
