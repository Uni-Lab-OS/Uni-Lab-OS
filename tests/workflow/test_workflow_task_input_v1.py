"""Round 02H WorkflowTask input preflight 的独立公共合同测试。

所有行为观察都通过 ``WorkflowService`` 或真实 Workflow HTTP route。测试中的
SQLite transaction 只用于构造普通写接口无法表达的历史/恶意 persisted fixture；
不会直接调用 Task input 实现 helper，也不查询私有 Task/Job 表。
"""

from __future__ import annotations

import json
import socket
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.json_codec import MAX_BACKEND_JSON_DEPTH
from unilabos.workflow.models import WorkflowEdgeWrite, WorkflowNodeWrite
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SOURCE_NODE_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-000000000001"
TARGET_NODE_UUID = "bbbbbbbb-bbbb-4bbb-8bbb-000000000002"
DISABLED_NODE_UUID = "cccccccc-cccc-4ccc-8ccc-000000000003"

SOURCE_TEMPLATE_UUID = "10000000-0000-4000-8000-000000000001"
TARGET_TEMPLATE_UUID = "10000000-0000-4000-8000-000000000002"
FOREIGN_TEMPLATE_UUID = "10000000-0000-4000-8000-000000000003"
RESOURCE_TEMPLATE_UUID = "20000000-0000-4000-8000-000000000001"
OTHER_RESOURCE_TEMPLATE_UUID = "20000000-0000-4000-8000-000000000002"

SOURCE_HANDLE_UUID = "30000000-0000-4000-8000-000000000001"
DEPENDENCY_SOURCE_HANDLE_UUID = "30000000-0000-4000-8000-000000000002"
TARGET_HANDLE_UUID = "30000000-0000-4000-8000-000000000003"
SECOND_TARGET_HANDLE_UUID = "30000000-0000-4000-8000-000000000004"
FOREIGN_TARGET_HANDLE_UUID = "30000000-0000-4000-8000-000000000005"
UNKNOWN_HANDLE_UUID = "30000000-0000-4000-8000-000000000099"
EDGE_UUID = "40000000-0000-4000-8000-000000000001"

MATERIAL_A_UUID = "50000000-0000-4000-8000-000000000001"
MATERIAL_B_UUID = "50000000-0000-4000-8000-000000000002"
MISSING_MATERIAL_UUID = "50000000-0000-4000-8000-000000000099"

EMPTY_CONTRACT = {"version": 1, "parameters": []}


@dataclass(frozen=True)
class _ResolvedSlot:
    uuid: str
    resource_template_uuid: str


class _RecordingResolver:
    def __init__(
        self,
        *,
        template_by_material: dict[str, str] | None = None,
        fail_by_material: dict[str, str] | None = None,
        malicious_by_material: dict[str, Any] | None = None,
    ) -> None:
        self.template_by_material = template_by_material or {
            MATERIAL_A_UUID: RESOURCE_TEMPLATE_UUID,
            MATERIAL_B_UUID: RESOURCE_TEMPLATE_UUID,
        }
        self.fail_by_material = fail_by_material or {}
        self.malicious_by_material = malicious_by_material or {}
        self.calls: list[tuple[str, tuple[str, ...] | None]] = []

    def resolve(
        self,
        *,
        material_uuid: str,
        allowed_resource_template_uuids: tuple[str, ...] | None,
    ) -> Any:
        self.calls.append((material_uuid, allowed_resource_template_uuids))
        if material_uuid in self.fail_by_material:
            raise WorkflowError(self.fail_by_material[material_uuid])
        if material_uuid in self.malicious_by_material:
            return self.malicious_by_material[material_uuid]
        return _ResolvedSlot(
            uuid=material_uuid,
            resource_template_uuid=self.template_by_material[material_uuid],
        )


@pytest.fixture()
def store(tmp_path: Path) -> WorkflowStore:
    opened = WorkflowStore(tmp_path / "workflow.db")
    yield opened
    opened.close()


def _input_contract(*parameters: dict[str, Any]) -> dict[str, Any]:
    return {"version": 1, "parameters": list(parameters)}


def _parameter(
    name: str,
    schema: dict[str, Any],
    *,
    required: bool = True,
    default: Any = None,
) -> dict[str, Any]:
    descriptor: dict[str, Any] = {
        "name": name,
        "schema": schema,
        "required": required,
    }
    if not required:
        descriptor["default"] = default
    return descriptor


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


def _slot_schema(
    *allowed_resource_template_uuids: str,
) -> dict[str, Any]:
    schema: dict[str, Any] = {"$slot": "ResourceSlot"}
    if allowed_resource_template_uuids:
        schema["allowed_resource_template_uuids"] = list(
            allowed_resource_template_uuids
        )
    return schema


def _create_workflow(
    store: WorkflowStore,
    *,
    contract: Any = EMPTY_CONTRACT,
    include_contract: bool = True,
    unilab: Any | None = None,
) -> WorkflowService:
    if unilab is not None:
        meta_data = {"unilab": unilab}
    elif include_contract:
        meta_data = {"unilab": {"input_contract": deepcopy(contract)}}
    else:
        meta_data = {}
    store.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="Round 02H contract",
        tags=[],
        description=None,
        meta_data=meta_data,
    )
    return WorkflowService(store)


def _service_with_resolver(
    store: WorkflowStore,
    resolver: Any,
) -> WorkflowService:
    return WorkflowService(store, resource_resolver=resolver)


def _node(
    node_uuid: str,
    *,
    template_uuid: str | None = None,
    param: dict[str, Any] | None = None,
    bindings: Any | None = None,
    disabled: bool = False,
) -> WorkflowNodeWrite:
    meta_data: dict[str, Any] = {}
    if bindings is not None:
        meta_data["unilab"] = {"input_bindings": deepcopy(bindings)}
    return WorkflowNodeWrite(
        uuid=node_uuid,
        workflow_node_template_uuid=template_uuid,
        name=node_uuid,
        status="idle",
        type="compute",
        pose={},
        param={} if param is None else deepcopy(param),
        execution_policy={},
        disabled=disabled,
        minimized=False,
        meta_data=meta_data,
    )


def _edge(*, dependency_only: bool = False) -> WorkflowEdgeWrite:
    return WorkflowEdgeWrite(
        uuid=EDGE_UUID,
        source_node_uuid=SOURCE_NODE_UUID,
        target_node_uuid=TARGET_NODE_UUID,
        source_handle_uuid=(
            DEPENDENCY_SOURCE_HANDLE_UUID if dependency_only else SOURCE_HANDLE_UUID
        ),
        target_handle_uuid=TARGET_HANDLE_UUID,
        meta_data={},
    )


def _seed_template_catalog(store: WorkflowStore) -> None:
    timestamp = "2026-08-01T00:00:00Z"
    with store.transaction() as connection:
        for template_uuid, name in (
            (SOURCE_TEMPLATE_UUID, "source"),
            (TARGET_TEMPLATE_UUID, "target"),
            (FOREIGN_TEMPLATE_UUID, "foreign"),
        ):
            connection.execute(
                """
                INSERT INTO workflow_node_template(
                    uuid, create_time, update_time, meta_data, authority_id,
                    resource_template_uuid, name, display_name, goal,
                    goal_default, feedback, result, schema, type, node_type
                ) VALUES (?, ?, ?, '{}', 'os-local', ?, ?, ?, '{}', '{}',
                          '{}', '{}', NULL, 'action', 'compute')
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
        handles = (
            (
                SOURCE_HANDLE_UUID,
                SOURCE_TEMPLATE_UUID,
                "result",
                "source",
                "number",
                0,
                "executor",
                "result",
            ),
            (
                DEPENDENCY_SOURCE_HANDLE_UUID,
                SOURCE_TEMPLATE_UUID,
                "ready",
                "source",
                "number",
                0,
                "dependency",
                "ready",
            ),
            (
                TARGET_HANDLE_UUID,
                TARGET_TEMPLATE_UUID,
                "volume",
                "target",
                "number",
                1,
                None,
                "wrapped@@@volume",
            ),
            (
                SECOND_TARGET_HANDLE_UUID,
                TARGET_TEMPLATE_UUID,
                "temperature",
                "target",
                "number",
                0,
                None,
                "temperature",
            ),
            (
                FOREIGN_TARGET_HANDLE_UUID,
                FOREIGN_TEMPLATE_UUID,
                "foreign",
                "target",
                "number",
                0,
                None,
                "foreign",
            ),
        )
        for (
            handle_uuid,
            template_uuid,
            handle_key,
            io_type,
            value_type,
            required,
            data_source,
            data_key,
        ) in handles:
            connection.execute(
                """
                INSERT INTO workflow_handle_template(
                    uuid, create_time, update_time, meta_data, authority_id,
                    workflow_node_template_uuid, handle_key, io_type,
                    display_name, type, required, data_source, data_key
                ) VALUES (?, ?, ?, '{}', 'os-local', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    handle_uuid,
                    timestamp,
                    timestamp,
                    template_uuid,
                    handle_key,
                    io_type,
                    handle_key,
                    value_type,
                    required,
                    data_source,
                    data_key,
                ),
            )


def _save_graph(
    store: WorkflowStore,
    *,
    nodes: list[WorkflowNodeWrite],
    edges: list[WorkflowEdgeWrite] | None = None,
) -> dict[str, Any]:
    return store.save_graph(
        WORKFLOW_UUID,
        revision=1,
        nodes=nodes,
        edges=[] if edges is None else edges,
    )


def _create_task(
    service: WorkflowService,
    input_value: dict[str, Any] | None = None,
    *,
    run_mode: str = "normal",
    target_node_uuid: str | None = None,
) -> dict[str, Any]:
    return service.create_workflow_task(
        workflow_uuid=WORKFLOW_UUID,
        run_mode=run_mode,
        target_node_uuid=target_node_uuid,
        input_value={} if input_value is None else input_value,
        description=None,
        meta_data={},
    )


def _assert_no_tasks(service: WorkflowService) -> None:
    page = service.list_workflow_tasks(workflow_uuid=WORKFLOW_UUID)
    assert page["total"] == 0
    assert page["items"] == []


def _replace_workflow_unilab(store: WorkflowStore, unilab: Any) -> None:
    with store.transaction() as connection:
        connection.execute(
            "UPDATE workflow SET meta_data = ? WHERE uuid = ?",
            (json.dumps({"unilab": unilab}), WORKFLOW_UUID),
        )


def _replace_node_metadata(
    store: WorkflowStore,
    node_uuid: str,
    metadata: Any,
) -> None:
    with store.transaction() as connection:
        connection.execute(
            "UPDATE workflow_node SET meta_data = ? WHERE uuid = ?",
            (json.dumps(metadata), node_uuid),
        )


def _replace_node_param(
    store: WorkflowStore,
    node_uuid: str,
    param: dict[str, Any],
) -> None:
    with store.transaction() as connection:
        connection.execute(
            "UPDATE workflow_node SET param = ? WHERE uuid = ?",
            (json.dumps(param), node_uuid),
        )


def _replace_target_handle_type(
    store: WorkflowStore,
    handle_type: str,
) -> None:
    with store.transaction() as connection:
        connection.execute(
            "UPDATE workflow_handle_template SET type = ? WHERE uuid = ?",
            (handle_type, TARGET_HANDLE_UUID),
        )


@pytest.mark.parametrize("include_contract", [False, True], ids=["absent", "empty"])
def test_absent_or_empty_contract_keeps_legacy_empty_task_success(
    store: WorkflowStore,
    include_contract: bool,
) -> None:
    service = _create_workflow(store, include_contract=include_contract)

    task = _create_task(service)

    assert task["input"] == {}
    assert task["workflow_snapshot"]["workflow"]["uuid"] == WORKFLOW_UUID
    assert service.list_workflow_node_jobs(task["uuid"]) == []


def test_resolved_input_is_contract_ordered_and_applies_omission_defaults(
    store: WorkflowStore,
) -> None:
    contract = _input_contract(
        _parameter("required_z", {"type": "integer"}),
        _parameter("default_a", {"type": "string"}, required=False, default="x"),
        _parameter(
            "nullable_m",
            _nullable({"type": "number"}),
            required=False,
            default=None,
        ),
    )
    service = _create_workflow(store, contract=contract)

    task = _create_task(
        service,
        {"nullable_m": None, "required_z": 3.0},
    )

    assert list(task["input"]) == ["required_z", "default_a", "nullable_m"]
    assert task["input"] == {
        "required_z": 3,
        "default_a": "x",
        "nullable_m": None,
    }


def test_missing_required_parameter_rejects_before_any_write(
    store: WorkflowStore,
) -> None:
    service = _create_workflow(
        store,
        contract=_input_contract(_parameter("required", {"type": "string"})),
    )

    with pytest.raises(WorkflowError) as failure:
        _create_task(service)

    assert failure.value.code == "invalid_input"
    _assert_no_tasks(service)


def test_declared_null_is_omission_but_unknown_null_is_still_rejected(
    store: WorkflowStore,
) -> None:
    service = _create_workflow(
        store,
        contract=_input_contract(
            _parameter("label", {"type": "string"}, required=False, default="safe")
        ),
    )
    first = _create_task(service, {"label": None})
    assert first["input"] == {"label": "safe"}

    with pytest.raises(WorkflowError) as failure:
        _create_task(service, {"ghost": None})

    assert failure.value.code == "invalid_input"
    assert service.list_workflow_tasks(workflow_uuid=WORKFLOW_UUID)["total"] == 1


def test_strict_values_constraints_and_deep_copies_are_canonical(
    store: WorkflowStore,
) -> None:
    contract = _input_contract(
        _parameter("text", {"type": "string", "minLength": 2, "maxLength": 4}),
        _parameter("flag", {"type": "boolean"}),
        _parameter("count", {"type": "integer", "minimum": 1, "maximum": 3}),
        _parameter("ratio", {"type": "number", "minimum": 0, "maximum": 10}),
        _parameter("payload", {"type": "object"}),
        _parameter(
            "labels",
            {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "maxItems": 3,
            },
        ),
    )
    service = _create_workflow(store, contract=contract)
    raw_payload = {"nested": [1, {"ok": True}]}
    raw_labels = ["a", "a"]
    raw = {
        "text": "abcd",
        "flag": False,
        "count": 3.0,
        "ratio": 10,
        "payload": raw_payload,
        "labels": raw_labels,
    }

    task = _create_task(service, raw)
    raw_payload["nested"].append("mutated")
    raw_labels.append("mutated")
    task["input"]["payload"]["nested"].append("returned-mutation")

    stored = service.get_workflow_task(task["uuid"])
    assert stored["input"] == {
        "text": "abcd",
        "flag": False,
        "count": 3,
        "ratio": 10,
        "payload": {"nested": [1, {"ok": True}]},
        "labels": ["a", "a"],
    }


def test_mutable_defaults_are_fresh_for_each_task_and_each_read(
    store: WorkflowStore,
) -> None:
    contract = _input_contract(
        _parameter(
            "payload",
            {"type": "object"},
            required=False,
            default={"nested": [1]},
        ),
        _parameter(
            "labels",
            {"type": "array", "items": {"type": "string"}},
            required=False,
            default=["same"],
        ),
    )
    service = _create_workflow(store, contract=contract)

    first = _create_task(service)
    first["input"]["payload"]["nested"].append(2)
    first["input"]["labels"].append("mutated")
    second = _create_task(service)

    assert service.get_workflow_task(first["uuid"])["input"] == {
        "payload": {"nested": [1]},
        "labels": ["same"],
    }
    assert second["input"] == {
        "payload": {"nested": [1]},
        "labels": ["same"],
    }


@pytest.mark.parametrize(
    ("schema", "value"),
    [
        ({"type": "string"}, 1),
        ({"type": "boolean"}, 1),
        ({"type": "integer"}, True),
        ({"type": "number"}, float("inf")),
        ({"type": "object"}, []),
        ({"type": "array", "items": {"type": "string"}}, [["nested"]]),
        ({"type": "array", "items": {"type": "string"}}, ["ok", 1]),
        ({"type": "array", "items": {"type": "string"}}, ["ok", None]),
        ({"type": "integer", "minimum": 1}, 0),
        ({"type": "integer", "maximum": 3}, 4),
        ({"type": "string", "minLength": 2}, "x"),
        ({"type": "string", "enum": ["red", "blue"]}, "green"),
        ({"type": "array", "items": {"type": "string"}, "maxItems": 1}, ["a", "b"]),
    ],
    ids=[
        "string-no-coercion",
        "bool-no-one",
        "integer-no-bool",
        "finite-number",
        "object-only",
        "one-dimensional-list",
        "homogeneous-list",
        "non-null-list-items",
        "inclusive-minimum",
        "inclusive-maximum",
        "inclusive-min-length",
        "strict-enum-membership",
        "inclusive-max-items",
    ],
)
def test_invalid_strict_value_is_rejected_without_partial_write(
    store: WorkflowStore,
    schema: dict[str, Any],
    value: Any,
) -> None:
    service = _create_workflow(
        store,
        contract=_input_contract(_parameter("value", schema)),
    )

    with pytest.raises(WorkflowError) as failure:
        _create_task(service, {"value": value})

    assert failure.value.code == "invalid_input"
    _assert_no_tasks(service)


def test_too_deep_raw_object_and_non_string_top_level_key_are_rejected(
    store: WorkflowStore,
) -> None:
    service = _create_workflow(
        store,
        contract=_input_contract(_parameter("payload", {"type": "object"})),
    )
    too_deep: dict[str, Any] = {}
    cursor = too_deep
    for _ in range(MAX_BACKEND_JSON_DEPTH + 1):
        child: dict[str, Any] = {}
        cursor["child"] = child
        cursor = child

    for raw in ({"payload": too_deep}, {1: "not-a-string-key"}):
        with pytest.raises(WorkflowError) as failure:
            _create_task(service, raw)  # type: ignore[arg-type]
        assert failure.value.code == "invalid_input"
    _assert_no_tasks(service)


@pytest.mark.parametrize(
    "unilab",
    [
        [],
        {"input_contract": []},
        {"input_contract": {"version": 1, "parameters": {}}},
        {
            "input_contract": {
                "version": 1,
                "parameters": [
                    {"name": "value", "schema": {"type": "mystery"}, "required": True}
                ],
            }
        },
        {
            "input_contract": {
                "version": 1,
                "parameters": [
                    {
                        "name": "value",
                        "schema": {"type": "integer", "minimum": 2},
                        "required": False,
                        "default": 1,
                    }
                ],
            }
        },
    ],
    ids=["unilab", "contract", "parameters", "schema", "default"],
)
def test_malformed_persisted_contract_fails_closed_before_write(
    store: WorkflowStore,
    unilab: Any,
) -> None:
    service = _create_workflow(store, include_contract=False)
    _replace_workflow_unilab(store, unilab)

    with pytest.raises(WorkflowError) as failure:
        _create_task(service)

    assert failure.value.code == "invalid_input"
    _assert_no_tasks(service)


def test_resource_slot_resolver_receives_canonical_uuid_and_ordered_allowlist(
    store: WorkflowStore,
) -> None:
    contract = _input_contract(
        _parameter(
            "material",
            _slot_schema(RESOURCE_TEMPLATE_UUID, OTHER_RESOURCE_TEMPLATE_UUID),
        )
    )
    _create_workflow(store, contract=contract)
    resolver = _RecordingResolver()
    service = _service_with_resolver(store, resolver)

    task = _create_task(service, {"material": {"uuid": MATERIAL_A_UUID.upper()}})

    assert resolver.calls == [
        (
            MATERIAL_A_UUID,
            (RESOURCE_TEMPLATE_UUID, OTHER_RESOURCE_TEMPLATE_UUID),
        )
    ]
    assert task["input"] == {
        "material": {
            "uuid": MATERIAL_A_UUID,
            "resource_template_uuid": RESOURCE_TEMPLATE_UUID,
        }
    }


@pytest.mark.parametrize(
    "external",
    [
        MATERIAL_A_UUID,
        {},
        {"uuid": "not-a-uuid"},
        {"uuid": MATERIAL_A_UUID, "resource_template_uuid": RESOURCE_TEMPLATE_UUID},
        {"uuid": MATERIAL_A_UUID, "name": "forged"},
    ],
    ids=["bare-uuid", "missing-uuid", "invalid-uuid", "caller-template", "sibling"],
)
def test_resource_slot_external_shape_is_closed(
    store: WorkflowStore,
    external: Any,
) -> None:
    _create_workflow(
        store,
        contract=_input_contract(_parameter("material", _slot_schema())),
    )
    resolver = _RecordingResolver()
    service = _service_with_resolver(store, resolver)

    with pytest.raises(WorkflowError) as failure:
        _create_task(service, {"material": external})

    assert failure.value.code == "invalid_input"
    assert resolver.calls == []
    _assert_no_tasks(service)


def test_resource_slot_list_preserves_order_duplicates_and_independent_resolution(
    store: WorkflowStore,
) -> None:
    _create_workflow(
        store,
        contract=_input_contract(
            _parameter(
                "materials",
                {"type": "array", "items": _slot_schema(RESOURCE_TEMPLATE_UUID)},
            )
        ),
    )
    resolver = _RecordingResolver()
    service = _service_with_resolver(store, resolver)

    task = _create_task(
        service,
        {
            "materials": [
                {"uuid": MATERIAL_B_UUID},
                {"uuid": MATERIAL_A_UUID},
                {"uuid": MATERIAL_B_UUID},
            ]
        },
    )

    assert [call[0] for call in resolver.calls] == [
        MATERIAL_B_UUID,
        MATERIAL_A_UUID,
        MATERIAL_B_UUID,
    ]
    assert {call[1] for call in resolver.calls} == {(RESOURCE_TEMPLATE_UUID,)}
    assert [item["uuid"] for item in task["input"]["materials"]] == [
        MATERIAL_B_UUID,
        MATERIAL_A_UUID,
        MATERIAL_B_UUID,
    ]


@pytest.mark.parametrize("code", ["invalid_input", "not_found", "conflict"])
def test_resource_resolver_errors_keep_stable_classification_and_zero_writes(
    store: WorkflowStore,
    code: str,
) -> None:
    _create_workflow(
        store,
        contract=_input_contract(_parameter("material", _slot_schema())),
    )
    resolver = _RecordingResolver(fail_by_material={MATERIAL_A_UUID: code})
    service = _service_with_resolver(store, resolver)

    with pytest.raises(WorkflowError) as failure:
        _create_task(service, {"material": {"uuid": MATERIAL_A_UUID}})

    assert failure.value.code == code
    assert resolver.calls == [(MATERIAL_A_UUID, None)]
    _assert_no_tasks(service)


@pytest.mark.parametrize(
    "malicious",
    [
        _ResolvedSlot(MATERIAL_B_UUID, RESOURCE_TEMPLATE_UUID),
        _ResolvedSlot(MATERIAL_A_UUID, OTHER_RESOURCE_TEMPLATE_UUID),
        {
            "uuid": MATERIAL_A_UUID,
            "resource_template_uuid": RESOURCE_TEMPLATE_UUID,
            "body": {},
        },
        {"uuid": "not-a-uuid", "resource_template_uuid": RESOURCE_TEMPLATE_UUID},
    ],
    ids=[
        "changed-identity",
        "template-mismatch",
        "unknown-field",
        "invalid-return-uuid",
    ],
)
def test_malicious_resolver_return_is_invalid_input_and_not_persisted(
    store: WorkflowStore,
    malicious: Any,
) -> None:
    _create_workflow(
        store,
        contract=_input_contract(
            _parameter("material", _slot_schema(RESOURCE_TEMPLATE_UUID))
        ),
    )
    resolver = _RecordingResolver(malicious_by_material={MATERIAL_A_UUID: malicious})
    service = _service_with_resolver(store, resolver)

    with pytest.raises(WorkflowError) as failure:
        _create_task(service, {"material": {"uuid": MATERIAL_A_UUID}})

    assert failure.value.code == "invalid_input"
    _assert_no_tasks(service)


def test_second_slot_failure_rolls_back_the_entire_task_and_all_jobs(
    store: WorkflowStore,
) -> None:
    _create_workflow(
        store,
        contract=_input_contract(
            _parameter(
                "materials",
                {"type": "array", "items": _slot_schema()},
            )
        ),
    )
    resolver = _RecordingResolver(fail_by_material={MISSING_MATERIAL_UUID: "not_found"})
    service = _service_with_resolver(store, resolver)

    with pytest.raises(WorkflowError) as failure:
        _create_task(
            service,
            {
                "materials": [
                    {"uuid": MATERIAL_A_UUID},
                    {"uuid": MISSING_MATERIAL_UUID},
                ]
            },
        )

    assert failure.value.code == "not_found"
    assert [call[0] for call in resolver.calls] == [
        MATERIAL_A_UUID,
        MISSING_MATERIAL_UUID,
    ]
    _assert_no_tasks(service)


def test_unconfigured_production_material_adapter_fails_closed_but_empty_slots_work(
    store: WorkflowStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _input_contract(
        _parameter(
            "optional_material",
            _nullable(_slot_schema()),
            required=False,
            default=None,
        ),
        _parameter(
            "materials",
            {"type": "array", "items": _slot_schema()},
            required=False,
            default=[],
        ),
    )
    service = _create_workflow(store, contract=contract)

    def reject_remote_fallback(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("02H must not use a remote Material fallback")

    monkeypatch.setattr(socket, "create_connection", reject_remote_fallback)

    empty_task = _create_task(
        service,
        {"optional_material": None, "materials": []},
    )
    assert empty_task["input"] == {
        "optional_material": None,
        "materials": [],
    }

    with pytest.raises(WorkflowError) as failure:
        _create_task(
            service,
            {"materials": [{"uuid": MATERIAL_A_UUID}]},
        )

    assert failure.value.code == "conflict"
    assert service.list_workflow_tasks(workflow_uuid=WORKFLOW_UUID)["total"] == 1


def test_real_handle_uuid_binding_projects_to_plan_and_job_without_mutating_graph(
    store: WorkflowStore,
) -> None:
    contract = _input_contract(_parameter("amount", {"type": "number"}))
    service = _create_workflow(store, contract=contract)
    _seed_template_catalog(store)
    bindings = {TARGET_HANDLE_UUID: {"parameter": "amount"}}
    _save_graph(
        store,
        nodes=[
            _node(
                TARGET_NODE_UUID,
                template_uuid=TARGET_TEMPLATE_UUID,
                param={"persisted": {"x": 1}},
                bindings=bindings,
            )
        ],
    )

    task = _create_task(service, {"amount": 7})
    job = service.list_workflow_node_jobs(task["uuid"])[0]
    planned = task["execution_plan"]["nodes"][0]
    current_node = service.get_graph(WORKFLOW_UUID)["nodes"][0]

    assert planned["param"] == {"persisted": {"x": 1}, "volume": 7}
    assert job["param"] == planned["param"]
    assert planned["param"] is not job["param"]
    planned["param"]["persisted"]["x"] = 99
    assert job["param"]["persisted"]["x"] == 1
    assert current_node["param"] == {"persisted": {"x": 1}}
    assert task["workflow_snapshot"]["nodes"][0]["param"] == {"persisted": {"x": 1}}
    assert (
        task["workflow_snapshot"]["nodes"][0]["meta_data"]["unilab"]["input_bindings"]
        == bindings
    )


@pytest.mark.parametrize("provider", ["static", "edge"], ids=["static", "edge"])
def test_one_active_static_or_edge_provider_remains_valid(
    store: WorkflowStore,
    provider: str,
) -> None:
    service = _create_workflow(store)
    _seed_template_catalog(store)
    if provider == "static":
        _save_graph(
            store,
            nodes=[
                _node(
                    TARGET_NODE_UUID,
                    template_uuid=TARGET_TEMPLATE_UUID,
                    param={"volume": 4},
                )
            ],
        )
    else:
        _save_graph(
            store,
            nodes=[
                _node(SOURCE_NODE_UUID, template_uuid=SOURCE_TEMPLATE_UUID),
                _node(TARGET_NODE_UUID, template_uuid=TARGET_TEMPLATE_UUID),
            ],
            edges=[_edge()],
        )

    task = _create_task(service)

    assert len(service.list_workflow_node_jobs(task["uuid"])) == len(
        task["execution_plan"]["nodes"]
    )


@pytest.mark.parametrize("bad_unilab", [[], {"input_bindings": []}])
def test_malformed_persisted_node_unilab_is_rejected_even_when_disabled(
    store: WorkflowStore,
    bad_unilab: Any,
) -> None:
    service = _create_workflow(store)
    _seed_template_catalog(store)
    _save_graph(
        store,
        nodes=[
            _node(
                DISABLED_NODE_UUID,
                template_uuid=TARGET_TEMPLATE_UUID,
                param={"volume": 1},
                disabled=True,
            )
        ],
    )
    _replace_node_metadata(store, DISABLED_NODE_UUID, {"unilab": bad_unilab})

    with pytest.raises(WorkflowError) as failure:
        _create_task(service)

    assert failure.value.code == "invalid_input"
    _assert_no_tasks(service)


@pytest.mark.parametrize(
    "binding",
    [
        {UNKNOWN_HANDLE_UUID: {"parameter": "amount"}},
        {FOREIGN_TARGET_HANDLE_UUID: {"parameter": "amount"}},
        {SOURCE_HANDLE_UUID: {"parameter": "amount"}},
        {TARGET_HANDLE_UUID: {"parameter": "missing"}},
        {TARGET_HANDLE_UUID: {"parameter": "amount", "source": "workflow_input"}},
        {TARGET_HANDLE_UUID: {"parameter": "amount", "extra": True}},
    ],
    ids=[
        "unknown-handle",
        "foreign-handle",
        "source-handle",
        "unknown-parameter",
        "legacy-source-field",
        "unknown-binding-field",
    ],
)
def test_persisted_binding_identity_and_shape_fail_closed_at_task_preflight(
    store: WorkflowStore,
    binding: dict[str, Any],
) -> None:
    service = _create_workflow(
        store,
        contract=_input_contract(_parameter("amount", {"type": "number"})),
    )
    _seed_template_catalog(store)
    _save_graph(
        store,
        nodes=[
            _node(
                TARGET_NODE_UUID,
                template_uuid=TARGET_TEMPLATE_UUID,
                bindings={TARGET_HANDLE_UUID: {"parameter": "amount"}},
            )
        ],
    )
    _replace_node_metadata(
        store,
        TARGET_NODE_UUID,
        {"unilab": {"input_bindings": binding}},
    )

    with pytest.raises(WorkflowError) as failure:
        _create_task(service, {"amount": 1})

    assert failure.value.code == "invalid_input"
    _assert_no_tasks(service)


def test_binding_value_must_match_the_real_target_handle_type(
    store: WorkflowStore,
) -> None:
    service = _create_workflow(
        store,
        contract=_input_contract(_parameter("amount", {"type": "string"})),
    )
    _seed_template_catalog(store)
    _save_graph(
        store,
        nodes=[
            _node(
                TARGET_NODE_UUID,
                template_uuid=TARGET_TEMPLATE_UUID,
                bindings={TARGET_HANDLE_UUID: {"parameter": "amount"}},
            )
        ],
    )

    with pytest.raises(WorkflowError) as failure:
        _create_task(service, {"amount": "not-a-number"})

    assert failure.value.code == "invalid_input"
    _assert_no_tasks(service)


def test_persisted_static_provider_must_match_the_real_target_handle_type(
    store: WorkflowStore,
) -> None:
    service = _create_workflow(store)
    _seed_template_catalog(store)
    _save_graph(
        store,
        nodes=[
            _node(
                TARGET_NODE_UUID,
                template_uuid=TARGET_TEMPLATE_UUID,
                param={"volume": 1},
            )
        ],
    )
    _replace_node_param(store, TARGET_NODE_UUID, {"volume": "not-a-number"})

    with pytest.raises(WorkflowError) as failure:
        _create_task(service)

    assert failure.value.code == "invalid_input"
    _assert_no_tasks(service)


@pytest.mark.parametrize(
    ("handle_type", "schema", "value"),
    [
        ("str", {"type": "integer"}, 1),
        ("ResourceSlot", {"type": "string"}, "not-a-slot"),
        ("list[str]", {"type": "object"}, {"not": "a-list"}),
        ("dict", {"type": "string"}, "not-an-object"),
        ("json", {"type": "array", "items": {"type": "string"}}, ["not-object"]),
        ("list[int]", {"type": "array", "items": {"type": "string"}}, ["one"]),
        ("list[float]", {"type": "array", "items": {"type": "boolean"}}, [True]),
        ("list[bool]", {"type": "array", "items": {"type": "integer"}}, [1]),
    ],
    ids=[
        "str-vs-integer",
        "resource-slot-vs-string",
        "string-list-vs-object",
        "dict-vs-string",
        "json-vs-list",
        "integer-list-vs-string-list",
        "float-list-vs-boolean-list",
        "boolean-list-vs-integer-list",
    ],
)
def test_known_v1_handle_vocabulary_rejects_incompatible_binding_values(
    store: WorkflowStore,
    handle_type: str,
    schema: dict[str, Any],
    value: Any,
) -> None:
    service = _create_workflow(
        store,
        contract=_input_contract(_parameter("value", schema)),
    )
    _seed_template_catalog(store)
    _save_graph(
        store,
        nodes=[
            _node(
                TARGET_NODE_UUID,
                template_uuid=TARGET_TEMPLATE_UUID,
                bindings={TARGET_HANDLE_UUID: {"parameter": "value"}},
            )
        ],
    )
    _replace_target_handle_type(store, handle_type)

    with pytest.raises(WorkflowError) as failure:
        _create_task(service, {"value": value})

    assert failure.value.code == "invalid_input"
    _assert_no_tasks(service)


@pytest.mark.parametrize(
    ("handle_type", "schema", "value"),
    [
        ("str", {"type": "string"}, "value"),
        ("string", {"type": "string"}, "value"),
        ("dict", {"type": "object"}, {"nested": [1]}),
        ("json", {"type": "object"}, {"nested": [1]}),
        ("list[str]", {"type": "array", "items": {"type": "string"}}, ["a", "a"]),
    ],
    ids=["str", "string", "dict", "json", "string-list"],
)
def test_known_v1_handle_vocabulary_accepts_compatible_binding_values(
    store: WorkflowStore,
    handle_type: str,
    schema: dict[str, Any],
    value: Any,
) -> None:
    service = _create_workflow(
        store,
        contract=_input_contract(_parameter("value", schema)),
    )
    _seed_template_catalog(store)
    _save_graph(
        store,
        nodes=[
            _node(
                TARGET_NODE_UUID,
                template_uuid=TARGET_TEMPLATE_UUID,
                bindings={TARGET_HANDLE_UUID: {"parameter": "value"}},
            )
        ],
    )
    _replace_target_handle_type(store, handle_type)

    task = _create_task(service, {"value": value})
    job = service.list_workflow_node_jobs(task["uuid"])[0]

    assert job["param"]["volume"] == value


def test_resource_slot_handle_accepts_only_the_canonical_resolved_slot(
    store: WorkflowStore,
) -> None:
    _create_workflow(
        store,
        contract=_input_contract(_parameter("value", _slot_schema())),
    )
    _seed_template_catalog(store)
    _save_graph(
        store,
        nodes=[
            _node(
                TARGET_NODE_UUID,
                template_uuid=TARGET_TEMPLATE_UUID,
                bindings={TARGET_HANDLE_UUID: {"parameter": "value"}},
            )
        ],
    )
    _replace_target_handle_type(store, "ResourceSlot")
    resolver = _RecordingResolver()
    service = _service_with_resolver(store, resolver)

    task = _create_task(service, {"value": {"uuid": MATERIAL_A_UUID}})
    job = service.list_workflow_node_jobs(task["uuid"])[0]
    canonical = {
        "uuid": MATERIAL_A_UUID,
        "resource_template_uuid": RESOURCE_TEMPLATE_UUID,
    }

    assert task["input"]["value"] == canonical
    assert job["param"]["volume"] == canonical


@pytest.mark.parametrize("provider", ["static", "edge"], ids=["static", "edge"])
def test_multiple_handle_providers_are_rejected_at_task_preflight(
    store: WorkflowStore,
    provider: str,
) -> None:
    service = _create_workflow(
        store,
        contract=_input_contract(_parameter("amount", {"type": "number"})),
    )
    _seed_template_catalog(store)
    binding = {TARGET_HANDLE_UUID: {"parameter": "amount"}}
    if provider == "static":
        _save_graph(
            store,
            nodes=[
                _node(
                    TARGET_NODE_UUID,
                    template_uuid=TARGET_TEMPLATE_UUID,
                    bindings=binding,
                )
            ],
        )
        _replace_node_param(store, TARGET_NODE_UUID, {"volume": 4})
    else:
        _save_graph(
            store,
            nodes=[
                _node(SOURCE_NODE_UUID, template_uuid=SOURCE_TEMPLATE_UUID),
                _node(TARGET_NODE_UUID, template_uuid=TARGET_TEMPLATE_UUID),
            ],
            edges=[_edge()],
        )
        _replace_node_metadata(
            store,
            TARGET_NODE_UUID,
            {"unilab": {"input_bindings": binding}},
        )

    with pytest.raises(WorkflowError) as failure:
        _create_task(service, {"amount": 1})

    assert failure.value.code == "invalid_input"
    _assert_no_tasks(service)


def test_required_handle_rejects_nullable_none_and_dependency_only_edge(
    store: WorkflowStore,
) -> None:
    nullable_contract = _input_contract(
        _parameter(
            "amount",
            _nullable({"type": "number"}),
            required=False,
            default=None,
        )
    )
    service = _create_workflow(store, contract=nullable_contract)
    _seed_template_catalog(store)
    _save_graph(
        store,
        nodes=[
            _node(
                TARGET_NODE_UUID,
                template_uuid=TARGET_TEMPLATE_UUID,
                bindings={TARGET_HANDLE_UUID: {"parameter": "amount"}},
            )
        ],
    )

    with pytest.raises(WorkflowError) as null_failure:
        _create_task(service)
    assert null_failure.value.code == "invalid_input"
    _assert_no_tasks(service)

    _replace_node_metadata(store, TARGET_NODE_UUID, {})
    with store.transaction() as connection:
        connection.execute(
            "DELETE FROM workflow_node WHERE workflow_uuid = ?",
            (WORKFLOW_UUID,),
        )
        connection.execute(
            "UPDATE workflow SET revision = 1 WHERE uuid = ?",
            (WORKFLOW_UUID,),
        )
    _save_graph(
        store,
        nodes=[
            _node(SOURCE_NODE_UUID, template_uuid=SOURCE_TEMPLATE_UUID),
            _node(TARGET_NODE_UUID, template_uuid=TARGET_TEMPLATE_UUID),
        ],
        edges=[_edge()],
    )
    # Graph PUT 应正确拒绝 dependency-only Edge 冒充 required input provider；
    # 这里模拟旧版本/恶意持久化事实，专门验证 Task transaction 会再次 fail closed。
    with store.transaction() as connection:
        connection.execute(
            """
            UPDATE workflow_edge
            SET source_handle_uuid = ?
            WHERE uuid = ?
            """,
            (DEPENDENCY_SOURCE_HANDLE_UUID, EDGE_UUID),
        )

    with pytest.raises(WorkflowError) as edge_failure:
        _create_task(service)
    assert edge_failure.value.code == "invalid_input"
    _assert_no_tasks(service)


def test_disabled_node_binding_is_validated_but_creates_no_job(
    store: WorkflowStore,
) -> None:
    service = _create_workflow(
        store,
        contract=_input_contract(_parameter("amount", {"type": "number"})),
    )
    _seed_template_catalog(store)
    _save_graph(
        store,
        nodes=[
            _node(SOURCE_NODE_UUID),
            _node(
                DISABLED_NODE_UUID,
                template_uuid=TARGET_TEMPLATE_UUID,
                bindings={TARGET_HANDLE_UUID: {"parameter": "amount"}},
                disabled=True,
            ),
        ],
    )

    task = _create_task(service, {"amount": 2})
    assert [
        job["workflow_node_uuid"]
        for job in service.list_workflow_node_jobs(task["uuid"])
    ] == [SOURCE_NODE_UUID]

    _replace_node_metadata(
        store,
        DISABLED_NODE_UUID,
        {"unilab": {"input_bindings": {UNKNOWN_HANDLE_UUID: {"parameter": "amount"}}}},
    )
    with pytest.raises(WorkflowError) as failure:
        _create_task(service, {"amount": 2})
    assert failure.value.code == "invalid_input"
    assert service.list_workflow_tasks(workflow_uuid=WORKFLOW_UUID)["total"] == 1


def test_single_node_scope_cannot_use_an_edge_cut_out_of_the_active_plan(
    store: WorkflowStore,
) -> None:
    service = _create_workflow(store)
    _seed_template_catalog(store)
    _save_graph(
        store,
        nodes=[
            _node(SOURCE_NODE_UUID, template_uuid=SOURCE_TEMPLATE_UUID),
            _node(TARGET_NODE_UUID, template_uuid=TARGET_TEMPLATE_UUID),
        ],
        edges=[_edge()],
    )

    with pytest.raises(WorkflowError) as failure:
        _create_task(
            service,
            run_mode="single_node",
            target_node_uuid=TARGET_NODE_UUID,
        )

    assert failure.value.code == "invalid_input"
    _assert_no_tasks(service)


def test_snapshot_input_binding_and_job_are_immutable_across_edit_and_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "restart.db"
    store = WorkflowStore(database)
    contract = _input_contract(_parameter("amount", {"type": "number"}))
    service = _create_workflow(store, contract=contract)
    _seed_template_catalog(store)
    original_binding = {TARGET_HANDLE_UUID: {"parameter": "amount"}}
    _save_graph(
        store,
        nodes=[
            _node(
                TARGET_NODE_UUID,
                template_uuid=TARGET_TEMPLATE_UUID,
                param={"persisted": 1},
                bindings=original_binding,
            )
        ],
    )
    task = _create_task(service, {"amount": 8})
    task_uuid = task["uuid"]
    job_uuid = service.list_workflow_node_jobs(task_uuid)[0]["uuid"]

    _replace_workflow_unilab(
        store,
        {
            "input_contract": _input_contract(
                _parameter(
                    "replacement", {"type": "string"}, required=False, default="new"
                )
            )
        },
    )
    _replace_node_metadata(store, TARGET_NODE_UUID, {})
    _replace_node_param(store, TARGET_NODE_UUID, {"persisted": 2})
    store.close()

    restarted_store = WorkflowStore(database)
    try:
        restarted = WorkflowService(restarted_store)
        frozen = restarted.get_workflow_task(task_uuid)
        frozen_job = restarted.get_workflow_node_job(job_uuid)
    finally:
        restarted_store.close()

    assert frozen["input"] == {"amount": 8}
    assert frozen["execution_plan"]["nodes"][0]["param"] == {
        "persisted": 1,
        "volume": 8,
    }
    assert frozen_job["param"] == {"persisted": 1, "volume": 8}
    assert (
        frozen["workflow_snapshot"]["workflow"]["meta_data"]["unilab"]["input_contract"]
        == contract
    )
    assert (
        frozen["workflow_snapshot"]["nodes"][0]["meta_data"]["unilab"]["input_bindings"]
        == original_binding
    )


def test_second_invalid_binding_prevents_all_task_and_job_writes(
    store: WorkflowStore,
) -> None:
    contract = _input_contract(
        _parameter("amount", {"type": "number"}),
        _parameter("temperature", {"type": "number"}),
    )
    service = _create_workflow(store, contract=contract)
    _seed_template_catalog(store)
    valid_bindings = {
        TARGET_HANDLE_UUID: {"parameter": "amount"},
        SECOND_TARGET_HANDLE_UUID: {"parameter": "temperature"},
    }
    _save_graph(
        store,
        nodes=[
            _node(
                TARGET_NODE_UUID,
                template_uuid=TARGET_TEMPLATE_UUID,
                bindings=valid_bindings,
            )
        ],
    )
    invalid_second = {
        TARGET_HANDLE_UUID: {"parameter": "amount"},
        SECOND_TARGET_HANDLE_UUID: {"parameter": "missing"},
    }
    _replace_node_metadata(
        store,
        TARGET_NODE_UUID,
        {"unilab": {"input_bindings": invalid_second}},
    )

    with pytest.raises(WorkflowError) as failure:
        _create_task(service, {"amount": 1, "temperature": 2})

    assert failure.value.code == "invalid_input"
    _assert_no_tasks(service)


def test_http_task_route_returns_backend_envelope_and_frozen_error_codes(
    store: WorkflowStore,
) -> None:
    contract = _input_contract(
        _parameter("count", {"type": "integer"}),
        _parameter("material", _slot_schema()),
    )
    _create_workflow(store, contract=contract)
    resolver = _RecordingResolver(
        fail_by_material={
            MISSING_MATERIAL_UUID: "not_found",
            MATERIAL_B_UUID: "conflict",
        }
    )
    service = _service_with_resolver(store, resolver)

    with TestClient(
        create_workflow_app(service),
        raise_server_exceptions=False,
    ) as client:
        success = client.post(
            "/api/v1/workflow-tasks",
            json={
                "workflow_uuid": WORKFLOW_UUID,
                "input": {"count": 3.0, "material": {"uuid": MATERIAL_A_UUID}},
            },
        )
        invalid = client.post(
            "/api/v1/workflow-tasks",
            json={
                "workflow_uuid": WORKFLOW_UUID,
                "input": {"count": True, "material": {"uuid": MATERIAL_A_UUID}},
            },
        )
        missing = client.post(
            "/api/v1/workflow-tasks",
            json={
                "workflow_uuid": WORKFLOW_UUID,
                "input": {"count": 1, "material": {"uuid": MISSING_MATERIAL_UUID}},
            },
        )
        conflict = client.post(
            "/api/v1/workflow-tasks",
            json={
                "workflow_uuid": WORKFLOW_UUID,
                "input": {"count": 1, "material": {"uuid": MATERIAL_B_UUID}},
            },
        )

    assert success.status_code == 201
    assert success.json()["data"]["input"]["count"] == 3
    for response, status, code in (
        (invalid, 400, "invalid_input"),
        (missing, 404, "not_found"),
        (conflict, 409, "conflict"),
    ):
        assert response.status_code == status
        assert response.json()["error"]["code"] == code
        serialized = json.dumps(response.json())
        assert "input_contract" not in serialized
        assert MISSING_MATERIAL_UUID not in serialized
        assert MATERIAL_B_UUID not in serialized
