"""A1 reviewer round-3 regressions for trusted metadata and Candidate typing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.workflow.test_a1_review_fixes_round2 import (
    WORKFLOW_UUID as ORDINARY_WORKFLOW_UUID,
)
from tests.workflow.test_a1_review_fixes_round2 import (
    _final_node,
    _input_contract,
    _ordinary_service,
)
from tests.workflow.test_authoring_engine import (
    ANALYZE_LABEL_TARGET,
    AUTHORITY,
    FINAL_REPORT_SOURCE,
    FINAL_REPORT_TARGET,
    FINAL_TEMPLATE_UUID,
    PREPARE_CYCLES_TARGET,
    PREPARE_NOTE_TARGET,
    WORKFLOW_UUID,
    _catalog_imports,
    _compile,
    _opened_engine,
    _source,
)
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.catalog import TemplateCatalog
from unilabos.workflow.json_codec import decode_json_bytes, encode_json
from unilabos.workflow.models import WorkflowEdgeWrite, WorkflowNodeWrite
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore

SECOND_NODE_UUID = "d0000000-0000-4000-8000-000000000001"
EDGE_UUID = "d1000000-0000-4000-8000-000000000001"
_INVALID_CANDIDATE_HASH = f"sha256:{'0' * 64}"


def _final_node_with_uuid(node_uuid: str) -> WorkflowNodeWrite:
    return WorkflowNodeWrite(
        uuid=node_uuid,
        workflow_node_template_uuid=FINAL_TEMPLATE_UUID,
        name=f"finalize {node_uuid[-1]}",
        status="idle",
        type="compute",
        param={},
        action_name="finalize",
        meta_data={},
    )


def test_ordinary_graph_put_cannot_create_input_bindings_on_a_new_node(
    tmp_path: Path,
) -> None:
    store, service = _ordinary_service(
        tmp_path,
        input_contract=_input_contract("report", {"type": "string"}),
    )
    try:
        saved = service.save_graph(
            ORDINARY_WORKFLOW_UUID,
            revision=1,
            nodes=[
                _final_node(
                    meta_data={
                        "caller": "preserved",
                        "unilab": {
                            "input_bindings": {
                                FINAL_REPORT_TARGET: {"parameter": "report"}
                            }
                        },
                    }
                )
            ],
            edges=[],
        )

        assert saved["nodes"][0]["meta_data"] == {"caller": "preserved"}
        persisted = service.get_graph(ORDINARY_WORKFLOW_UUID)
        assert persisted["nodes"][0]["meta_data"] == {"caller": "preserved"}
    finally:
        store.close()


def test_ordinary_graph_put_cannot_create_node_owned_metadata_on_a_new_edge(
    tmp_path: Path,
) -> None:
    store, service = _ordinary_service(tmp_path)
    try:
        saved = service.save_graph(
            ORDINARY_WORKFLOW_UUID,
            revision=1,
            nodes=[
                _final_node_with_uuid("b1000000-0000-4000-8000-000000000001"),
                _final_node_with_uuid(SECOND_NODE_UUID),
            ],
            edges=[
                WorkflowEdgeWrite(
                    uuid=EDGE_UUID,
                    source_node_uuid="b1000000-0000-4000-8000-000000000001",
                    target_node_uuid=SECOND_NODE_UUID,
                    source_handle_uuid=FINAL_REPORT_SOURCE,
                    target_handle_uuid=FINAL_REPORT_TARGET,
                    meta_data={
                        "caller": "preserved",
                        "unilab": {
                            "input_bindings": {
                                FINAL_REPORT_TARGET: {"parameter": "report"}
                            },
                            "executor_binding": {
                                "mode": "fixed",
                                "device_id": "edge-cannot-own-this",
                            },
                        },
                    },
                )
            ],
        )

        assert saved["edges"][0]["meta_data"] == {"caller": "preserved"}
        assert service.get_graph(ORDINARY_WORKFLOW_UUID)["edges"][0]["meta_data"] == {
            "caller": "preserved"
        }
    finally:
        store.close()


def _integer_mode_source() -> str:
    return _source().replace(
        '    mode: Literal["fast", "safe"] = "safe",',
        "    mode: int = 1,",
        1,
    )


def test_python_compile_rejects_workflow_parameter_handle_type_mismatch(
    tmp_path: Path,
) -> None:
    with _opened_engine(tmp_path / "compile.db") as context:
        result = _compile(context.engine, _integer_mode_source())

    assert not result.valid
    assert result.graph is None
    assert any(item["severity"] == "error" for item in result.diagnostics)


def test_public_validate_rejects_incompatible_candidate_binding(
    tmp_path: Path,
) -> None:
    with _opened_engine(tmp_path / "validate.db") as context:
        compiled = _compile(context.engine)
        assert compiled.valid and compiled.graph is not None
        incompatible_graph = decode_json_bytes(encode_json(compiled.graph))
        parameters = incompatible_graph["workflow"]["meta_data"]["unilab"][
            "input_contract"
        ]["parameters"]
        mode = next(item for item in parameters if item["name"] == "mode")
        mode["schema"] = {"type": "integer"}
        mode["default"] = 1

        result = context.engine.validate(
            workflow_uuid=WORKFLOW_UUID,
            workflow_revision=7,
            graph=incompatible_graph,
            python_source=_integer_mode_source(),
            source_uri="package://lab/workflows/incompatible.py",
        )

    assert not result.valid
    assert result.graph is None
    assert any(item["severity"] == "error" for item in result.diagnostics)


def test_persistent_candidate_rejects_incompatible_binding_before_apply(
    tmp_path: Path,
) -> None:
    store = WorkflowStore(tmp_path / "workflow.db")
    try:
        catalog = TemplateCatalog(store)
        catalog.replace(AUTHORITY, _catalog_imports())
        engine = WorkflowAuthoringEngine(catalog=catalog, authority=AUTHORITY)
        service = WorkflowService(store, compiler=engine)
        service.create_workflow(
            workflow_uuid=WORKFLOW_UUID,
            name="Incompatible binding",
            tags=[],
            description=None,
            meta_data={},
        )
        package_root = tmp_path / "package"
        package_root.mkdir()
        service.register_editable_source(
            workflow_uuid=WORKFLOW_UUID,
            package_id="lab",
            package_root=package_root,
            relative_path="workflows/incompatible.py",
        )
        draft = service.save_draft(
            WORKFLOW_UUID,
            python_source=_integer_mode_source(),
            expected_draft_hash=None,
            expected_workflow_revision=1,
        )

        assert draft["candidate"] is None
        assert draft["state"] == "draft_invalid"
        assert any(
            item["severity"] == "error" for item in draft["draft"]["diagnostics"]
        )
        with pytest.raises(WorkflowError) as failure:
            service.apply_authoring(
                WORKFLOW_UUID,
                candidate_hash=_INVALID_CANDIDATE_HASH,
            )
        assert failure.value.code == "draft_invalid"
        graph = service.get_graph(WORKFLOW_UUID)
        assert graph["workflow"]["revision"] == 1
        assert graph["nodes"] == []
    finally:
        store.close()


def _compatible_case(case: str) -> tuple[list[Any], str]:
    imports = _catalog_imports()
    source = _source()
    if case == "integer-to-number":
        for template in imports:
            for handle in template.handles:
                if handle["uuid"] == PREPARE_CYCLES_TARGET:
                    handle["type"] = "number"
    elif case == "array":
        for template in imports:
            for handle in template.handles:
                if handle["uuid"] == PREPARE_NOTE_TARGET:
                    handle["type"] = "list[string]"
                    handle["meta_data"]["unilab"]["value_schema"] = {
                        "type": "array",
                        "items": {"type": "string"},
                    }
        source = source.replace(
            "    note: Annotated[str | None, Field(max_length=200)] = None,",
            "    note: list[str] = [],",
            1,
        )
    else:
        assert case == "nullable-and-resource-slot"
    return imports, source


@pytest.mark.parametrize(
    "case",
    ["nullable-and-resource-slot", "integer-to-number", "array"],
)
def test_compatible_python_candidate_bindings_remain_valid(
    tmp_path: Path,
    case: str,
) -> None:
    imports, source = _compatible_case(case)
    with _opened_engine(tmp_path / f"{case}.db", imports=imports) as context:
        compiled = _compile(context.engine, source)
        assert compiled.valid and compiled.graph is not None, compiled.diagnostics
        validated = context.engine.validate(
            workflow_uuid=WORKFLOW_UUID,
            workflow_revision=7,
            graph=compiled.graph,
            python_source=compiled.normalized_python_source,
            source_uri="package://lab/workflows/compatible.py",
        )

    assert validated.valid, validated.diagnostics
    assert validated.graph == compiled.graph
    analyze = next(
        node for node in compiled.graph["nodes"] if node["name"] == "analyzed"
    )
    if case == "nullable-and-resource-slot":
        assert ANALYZE_LABEL_TARGET in analyze["meta_data"]["unilab"]["input_bindings"]
