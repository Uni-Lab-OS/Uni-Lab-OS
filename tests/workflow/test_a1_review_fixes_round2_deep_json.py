"""A1 deep-JSON regression through public Apply, graph, and Task seams."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from tests.workflow.test_phase01_review_contract_round14_followup import (
    CATALOG_FINGERPRINT,
    DEEP_JSON_DEPTH,
    NODE_UUID,
    SOURCE,
    WORKFLOW_UUID,
    FollowupCompiler,
    _nested_json,
)
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.catalog import (
    CatalogAuthority,
    NodeTemplateImport,
    TemplateCatalog,
)
from unilabos.workflow.models import WorkflowNodeWrite
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore

NODE_TEMPLATE_UUID = "c0000000-0000-4000-8000-000000000001"
HANDLE_TEMPLATE_UUID = "c1000000-0000-4000-8000-000000000001"
RESOURCE_TEMPLATE_UUID = "c2000000-0000-4000-8000-000000000001"
AUTHORITY = CatalogAuthority(authority_id="deep-json", kind="backend")
TRUSTED_SOURCE = f'''from tests.deep import DeepConsumer
from unilabos.registry.annotations import JSONValue
from unilabos.workflow.authoring import device, workflow_definition


consumer: DeepConsumer = device()


@workflow_definition(
    workflow_uuid="{WORKFLOW_UUID}",
    displayname="Deep public path",
)
def deep_public_path(*, payload: dict[str, JSONValue]):
    # unilab:node_uuid={NODE_UUID}
    consumed = consumer.deep_consumer(payload=payload)
'''


def _deep_leaf(value: Any) -> Any:
    current = value
    while isinstance(current, (dict, list)):
        current = current["next"] if isinstance(current, dict) else current[0]
    return current


def _replace_deep_leaf(value: Any, replacement: Any) -> None:
    current = value
    while True:
        if isinstance(current, dict):
            child = current["next"]
            if isinstance(child, (dict, list)):
                current = child
                continue
            current["next"] = replacement
            return
        child = current[0]
        if isinstance(child, (dict, list)):
            current = child
            continue
        current[0] = replacement
        return


def _catalog_import() -> NodeTemplateImport:
    return NodeTemplateImport(
        template={
            "uuid": NODE_TEMPLATE_UUID,
            "description": "Deep object consumer",
            "meta_data": {},
            "resource_template_uuid": RESOURCE_TEMPLATE_UUID,
            "name": "deep_consumer",
            "display_name": "Deep consumer",
            "class": "tests.deep:DeepConsumer",
            "goal": {},
            "goal_default": {},
            "feedback": {},
            "result": {},
            "schema": {
                "type": "object",
                "properties": {"payload": {"type": "object"}},
            },
            "type": "action",
            "icon": None,
            "header": None,
            "footer": None,
            "node_type": "compute",
        },
        handles=[
            {
                "uuid": HANDLE_TEMPLATE_UUID,
                "description": "Deep workflow input",
                "meta_data": {"unilab": {"value_schema": {"type": "object"}}},
                "handle_key": "payload",
                "io_type": "target",
                "display_name": "Payload",
                "type": "object",
                "required": False,
                "data_source": "executor",
                "data_key": "payload",
            }
        ],
    )


def _input_contract(default: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "parameters": [
            {
                "name": "payload",
                "schema": {"type": "object"},
                "required": False,
                "default": default,
            }
        ],
    }


def _node(param: dict[str, Any]) -> WorkflowNodeWrite:
    return WorkflowNodeWrite(
        uuid=NODE_UUID,
        workflow_node_template_uuid=NODE_TEMPLATE_UUID,
        name="deep consumer",
        status="idle",
        type="compute",
        param=param,
        action_name="deep_consumer",
        meta_data={
            "unilab": {
                "input_bindings": {
                    HANDLE_TEMPLATE_UUID: {"parameter": "payload"},
                }
            }
        },
    )


def _materialize_and_apply(
    service: WorkflowService,
    *,
    python_source: str,
    expected_draft_hash: str | None,
    expected_workflow_revision: int,
) -> dict[str, Any]:
    draft = service.save_draft(
        WORKFLOW_UUID,
        python_source=python_source,
        expected_draft_hash=expected_draft_hash,
        expected_workflow_revision=expected_workflow_revision,
    )
    candidate = draft["candidate"]
    assert candidate is not None, draft["draft"]["diagnostics"]
    if draft["draft"]["python_source"] != candidate["normalized_python_source"]:
        draft = service.save_draft(
            WORKFLOW_UUID,
            python_source=candidate["normalized_python_source"],
            expected_draft_hash=draft["draft"]["draft_hash"],
            expected_workflow_revision=expected_workflow_revision,
        )
        candidate = draft["candidate"]
        assert candidate is not None, draft["draft"]["diagnostics"]
    service.apply_authoring(
        WORKFLOW_UUID,
        candidate_hash=candidate["candidate_hash"],
    )
    return draft


def test_deep_contract_survives_apply_save_read_and_task_without_aliasing(
    tmp_path: Path,
) -> None:
    assert DEEP_JSON_DEPTH > sys.getrecursionlimit()
    contract_default = _nested_json(DEEP_JSON_DEPTH, "contract-original")
    store = WorkflowStore(tmp_path / "workflow.db")
    try:
        catalog = TemplateCatalog(store)
        catalog.replace(AUTHORITY, [_catalog_import()])
        engine = WorkflowAuthoringEngine(catalog=catalog, authority=AUTHORITY)
        service = WorkflowService(store, compiler=engine)
        service.create_workflow(
            workflow_uuid=WORKFLOW_UUID,
            name="Deep public path",
            tags=[],
            description=None,
            meta_data={},
        )
        package_root = tmp_path / "package"
        package_root.mkdir()
        service.register_editable_source(
            workflow_uuid=WORKFLOW_UUID,
            package_id="deep_json",
            package_root=package_root,
            relative_path="workflows/deep.py",
        )
        trusted = _materialize_and_apply(
            service,
            python_source=TRUSTED_SOURCE,
            expected_draft_hash=None,
            expected_workflow_revision=1,
        )
        trusted_graph = service.get_graph(WORKFLOW_UUID)
        assert trusted_graph["nodes"][0]["meta_data"]["unilab"][
            "input_bindings"
        ] == {HANDLE_TEMPLATE_UUID: {"parameter": "payload"}}

        current = service.get_workflow(WORKFLOW_UUID)
        current_unilab = current["meta_data"]["unilab"]
        store.update_workflow(
            WORKFLOW_UUID,
            name=current["name"],
            tags=current["tags"],
            description=current.get("description"),
            meta_data={
                **current["meta_data"],
                "unilab": {
                    **current_unilab,
                    "input_contract": _input_contract(contract_default),
                },
            },
        )
        _replace_deep_leaf(contract_default, "contract-caller-mutated")

        service = WorkflowService(store, compiler=FollowupCompiler())
        draft = service.save_draft(
            WORKFLOW_UUID,
            python_source=SOURCE,
            expected_draft_hash=trusted["draft"]["draft_hash"],
            expected_workflow_revision=2,
        )
        candidate = draft["candidate"]
        assert candidate is not None
        assert candidate["template_catalog_fingerprint"] == CATALOG_FINGERPRINT
        candidate_default = candidate["graph"]["workflow"]["meta_data"]["unilab"][
            "input_contract"
        ]["parameters"][0]["default"]
        _replace_deep_leaf(candidate_default, "candidate-response-mutated")
        service.apply_authoring(
            WORKFLOW_UUID,
            candidate_hash=candidate["candidate_hash"],
        )

        after_apply = service.get_graph(WORKFLOW_UUID)
        persisted_default = after_apply["workflow"]["meta_data"]["unilab"][
            "input_contract"
        ]["parameters"][0]["default"]
        assert _deep_leaf(persisted_default) == "contract-original"

        caller_param = _nested_json(DEEP_JSON_DEPTH, "param-original")
        saved = service.save_graph(
            WORKFLOW_UUID,
            revision=2,
            nodes=[_node({"static": caller_param})],
            edges=[],
        )
        assert saved["workflow"]["revision"] == 3
        _replace_deep_leaf(caller_param, "param-caller-mutated")
        _replace_deep_leaf(
            saved["nodes"][0]["param"]["static"],
            "save-response-mutated",
        )

        read_back = service.get_graph(WORKFLOW_UUID)
        assert _deep_leaf(read_back["nodes"][0]["param"]["static"]) == (
            "param-original"
        )
        assert (
            _deep_leaf(
                read_back["workflow"]["meta_data"]["unilab"]["input_contract"][
                    "parameters"
                ][0]["default"]
            )
            == "contract-original"
        )

        task = service.create_workflow_task(
            workflow_uuid=WORKFLOW_UUID,
            run_mode="normal",
            target_node_uuid=None,
            input_value={},
            description=None,
            meta_data={},
        )
        jobs = service.list_workflow_node_jobs(task["uuid"])
        assert len(jobs) == 1
        assert _deep_leaf(task["input"]["payload"]) == "contract-original"
        assert _deep_leaf(jobs[0]["param"]["payload"]) == "contract-original"
        assert _deep_leaf(jobs[0]["param"]["static"]) == "param-original"

        _replace_deep_leaf(task["input"]["payload"], "task-response-mutated")
        _replace_deep_leaf(jobs[0]["param"]["static"], "job-response-mutated")
        persisted_task = service.get_workflow_task(task["uuid"])
        persisted_job = service.list_workflow_node_jobs(task["uuid"])[0]
        assert _deep_leaf(persisted_task["input"]["payload"]) == "contract-original"
        assert _deep_leaf(persisted_job["param"]["static"]) == "param-original"
    finally:
        store.close()


@pytest.mark.parametrize("fallback_field", ["goal_default", "goal"])
def test_none_param_uses_deep_template_fallback_without_recursion_or_aliasing(
    tmp_path: Path,
    fallback_field: str,
) -> None:
    assert DEEP_JSON_DEPTH > sys.getrecursionlimit()
    caller_fallback = _nested_json(DEEP_JSON_DEPTH, f"{fallback_field}-original")
    imported = _catalog_import()
    imported.template["goal_default"] = (
        caller_fallback if fallback_field == "goal_default" else {}
    )
    imported.template["goal"] = (
        caller_fallback if fallback_field == "goal" else {"shallow": "unused"}
    )
    store = WorkflowStore(tmp_path / f"{fallback_field}.db")
    try:
        catalog = TemplateCatalog(store)
        catalog.replace(AUTHORITY, [imported])
        _replace_deep_leaf(caller_fallback, f"{fallback_field}-caller-mutated")
        service = WorkflowService(store)
        service.create_workflow(
            workflow_uuid=WORKFLOW_UUID,
            name=f"Deep {fallback_field}",
            tags=[],
            description=None,
            meta_data={},
        )

        saved = service.save_graph(
            WORKFLOW_UUID,
            revision=1,
            nodes=[
                WorkflowNodeWrite(
                    uuid=NODE_UUID,
                    workflow_node_template_uuid=NODE_TEMPLATE_UUID,
                    name="deep fallback",
                    status="idle",
                    type="compute",
                    param=None,
                    action_name="deep_consumer",
                    meta_data={},
                )
            ],
            edges=[],
        )
        assert _deep_leaf(saved["nodes"][0]["param"]) == (
            f"{fallback_field}-original"
        )
        _replace_deep_leaf(
            saved["nodes"][0]["param"],
            f"{fallback_field}-save-response-mutated",
        )

        read_back = service.get_graph(WORKFLOW_UUID)
        assert _deep_leaf(read_back["nodes"][0]["param"]) == (
            f"{fallback_field}-original"
        )
        task = service.create_workflow_task(
            workflow_uuid=WORKFLOW_UUID,
            run_mode="normal",
            target_node_uuid=None,
            input_value={},
            description=None,
            meta_data={},
        )
        job = service.list_workflow_node_jobs(task["uuid"])[0]
        assert _deep_leaf(job["param"]) == f"{fallback_field}-original"
        _replace_deep_leaf(job["param"], f"{fallback_field}-job-response-mutated")
        persisted_job = service.list_workflow_node_jobs(task["uuid"])[0]
        assert _deep_leaf(persisted_job["param"]) == f"{fallback_field}-original"
    finally:
        store.close()
