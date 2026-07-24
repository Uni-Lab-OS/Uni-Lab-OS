"""RED contracts for workflow-level parameters and Runtime preflight.

The Canonical ordered contract is the single source used by Python authoring,
Runtime validation, and the shared Local/Cloud form.  These tests intentionally
exercise only OS behavior; they do not introduce a second UI or Go dependency.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from unilabos.runtime.service import RuntimeService
from unilabos.scheduler.dag_model import DagValidationError, TaskDag
from unilabos.workflow.canonical import WorkflowRevision
from unilabos.workflow.from_python_script import compile_python_script


ACTION_CATALOG: dict[str, dict[str, Any]] = {
    "station.aspirate": {
        "inputs": {
            "plate_no": {"type": "string", "required": True},
            "well": {"type": "string", "required": True},
            "volume_ml": {"type": "number", "required": True},
            "mix_cycles": {"type": "integer", "required": True},
            "touch_tip": {"type": "boolean", "required": True},
        },
        "outputs": {},
    }
}


def _parameter_payload() -> list[dict[str, Any]]:
    return [
        {
            "name": "plate_no",
            "type": "string",
            "required": True,
            "title": "plate_no",
            "description": "",
        },
        {
            "name": "well",
            "type": "string",
            "required": True,
            "title": "well",
            "description": "",
        },
        {
            "name": "volume_ml",
            "type": "number",
            "required": False,
            "default": 5.0,
            "title": "volume_ml",
            "description": "",
        },
        {
            "name": "mix_cycles",
            "type": "integer",
            "required": False,
            "default": 2,
            "title": "mix_cycles",
            "description": "",
        },
        {
            "name": "touch_tip",
            "type": "boolean",
            "required": False,
            "default": False,
            "title": "touch_tip",
            "description": "",
        },
    ]


def _canonical_payload(
    *, parameters: list[dict[str, Any]] | None | object = ...,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "2",
        "revision_id": "draft",
        "workflow_id": "sampling",
        "invocations": [
            {
                "node_id": "aspirate-1",
                "action_ref": "station.aspirate",
                "input_bindings": {
                    name: {"kind": "runtime_parameter", "parameter": name}
                    for name in (
                        "plate_no",
                        "well",
                        "volume_ml",
                        "mix_cycles",
                        "touch_tip",
                    )
                },
            }
        ],
    }
    if parameters is not ...:
        payload["parameters"] = parameters
    return payload


def test_canonical_parameter_contract_is_ordered_and_distinguishes_legacy() -> None:
    revision = WorkflowRevision.model_validate(
        _canonical_payload(parameters=_parameter_payload())
    )

    assert [parameter.name for parameter in revision.parameters] == [
        "plate_no",
        "well",
        "volume_ml",
        "mix_cycles",
        "touch_tip",
    ]
    assert revision.parameters[0].model_dump(mode="json", exclude_unset=True) == {
        "name": "plate_no",
        "type": "string",
        "required": True,
        "title": "plate_no",
        "description": "",
    }
    assert WorkflowRevision.model_validate(
        _canonical_payload(parameters=[])
    ).parameters == []
    assert WorkflowRevision.model_validate(_canonical_payload()).parameters is None


def test_canonical_parameter_contract_rejects_duplicates_and_bad_defaults() -> None:
    duplicate = _parameter_payload()
    duplicate[1] = {**duplicate[1], "name": "plate_no"}
    with pytest.raises(ValueError, match="(?i)duplicate|unique"):
        WorkflowRevision.model_validate(_canonical_payload(parameters=duplicate))

    bad_default = _parameter_payload()
    bad_default[2] = {**bad_default[2], "default": True}
    with pytest.raises(ValueError, match="INVALID_WORKFLOW_PARAMETER_DEFAULT"):
        WorkflowRevision.model_validate(_canonical_payload(parameters=bad_default))


def test_python_function_signature_compiles_ordered_contract_and_runtime_refs() -> None:
    source = '''
@workflow_definition(workflow_id="sampling", revision="draft")
def sampling(
    plate_no: str,
    well: str,
    volume_ml: float = 5.0,
    mix_cycles: int = 2,
    touch_tip: bool = False,
):
    """吸取指定孔位的样品。"""
    station.aspirate(
        plate_no=plate_no,
        well=well,
        volume_ml=volume_ml,
        mix_cycles=mix_cycles,
        touch_tip=touch_tip,
    )
'''

    revision = compile_python_script(source, action_catalog=ACTION_CATALOG)

    assert revision.workflow_id == "sampling"
    assert revision.revision_id == "draft"
    assert [
        parameter.model_dump(mode="json", exclude_none=True)
        for parameter in revision.parameters
    ] == [
        {
            "name": "plate_no",
            "type": "string",
            "required": True,
            "title": "plate_no",
            "description": "",
        },
        {
            "name": "well",
            "type": "string",
            "required": True,
            "title": "well",
            "description": "",
        },
        {
            "name": "volume_ml",
            "type": "number",
            "required": False,
            "default": 5.0,
            "title": "volume_ml",
            "description": "",
        },
        {
            "name": "mix_cycles",
            "type": "integer",
            "required": False,
            "default": 2,
            "title": "mix_cycles",
            "description": "",
        },
        {
            "name": "touch_tip",
            "type": "boolean",
            "required": False,
            "default": False,
            "title": "touch_tip",
            "description": "",
        },
    ]
    invocation = revision.invocations[0]
    for name in ("plate_no", "well", "volume_ml", "mix_cycles", "touch_tip"):
        binding = invocation.input_bindings[name]
        assert binding.kind == "runtime_parameter"
        assert binding.parameter == name
        assert binding.default is None, "defaults belong only to the workflow contract"


class _RecordingSchedule:
    def __init__(self) -> None:
        self.submitted: list[TaskDag] = []

    def on_job_status(self, callback: Any) -> None:
        del callback

    async def submit_dag(self, dag: TaskDag) -> object:
        self.submitted.append(dag)
        return type("AcceptedRun", (), {"dag": dag})()

    def get_run(self, run_id: str) -> None:
        del run_id
        return None


@pytest.mark.parametrize(
    ("parameters", "error_code"),
    [
        (
            {"plate_no": "P1", "well": "A1", "surprise": 1},
            "UNKNOWN_WORKFLOW_PARAMETER",
        ),
        ({"plate_no": "P1"}, "MISSING_WORKFLOW_PARAMETER"),
        (
            {"plate_no": "P1", "well": "A1", "volume_ml": True},
            "WORKFLOW_PARAMETER_TYPE_MISMATCH",
        ),
        (
            {"plate_no": "P1", "well": "A1", "mix_cycles": True},
            "WORKFLOW_PARAMETER_TYPE_MISMATCH",
        ),
    ],
)
def test_runtime_parameter_preflight_rejects_before_any_dispatch(
    parameters: dict[str, Any],
    error_code: str,
) -> None:
    schedule = _RecordingSchedule()
    service = RuntimeService(schedule, action_catalog=ACTION_CATALOG)

    with pytest.raises(DagValidationError, match=error_code):
        asyncio.run(
            service.start_run(
                {
                    "source": {
                        "format": "canonical_workflow_v2",
                        "payload": _canonical_payload(
                            parameters=_parameter_payload()
                        ),
                    },
                    "parameters": parameters,
                }
            )
        )

    assert schedule.submitted == []


def test_runtime_applies_defaults_and_override_to_the_submitted_task_dag() -> None:
    schedule = _RecordingSchedule()
    service = RuntimeService(schedule, action_catalog=ACTION_CATALOG)

    asyncio.run(
        service.start_run(
            {
                "source": {
                    "format": "canonical_workflow_v2",
                    "payload": _canonical_payload(parameters=_parameter_payload()),
                },
                "parameters": {
                    "plate_no": "P1",
                    "well": "B3",
                    "volume_ml": 7.5,
                },
            }
        )
    )

    assert len(schedule.submitted) == 1
    assert schedule.submitted[0].runtime_parameters == {
        "plate_no": "P1",
        "well": "B3",
        "volume_ml": 7.5,
        "mix_cycles": 2,
        "touch_tip": False,
    }


def test_explicit_zero_parameter_contract_rejects_every_run_parameter() -> None:
    schedule = _RecordingSchedule()
    service = RuntimeService(
        schedule,
        action_catalog={"station.noop": {"inputs": {}, "outputs": {}}},
    )
    payload = {
        "schema_version": "2",
        "revision_id": "draft",
        "workflow_id": "no-parameters",
        "parameters": [],
        "invocations": [{"node_id": "noop-1", "action_ref": "station.noop"}],
    }

    with pytest.raises(DagValidationError, match="UNKNOWN_WORKFLOW_PARAMETER"):
        asyncio.run(
            service.start_run(
                {
                    "source": {
                        "format": "canonical_workflow_v2",
                        "payload": payload,
                    },
                    "parameters": {"unexpected": "value"},
                }
            )
        )

    assert schedule.submitted == []
