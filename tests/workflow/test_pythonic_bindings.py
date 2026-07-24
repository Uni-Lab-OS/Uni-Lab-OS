"""Quick Debug Alpha Python authoring and runtime-binding acceptance tests."""

from __future__ import annotations

import importlib
from types import ModuleType

import pytest


def _require(module_name: str, capability: str) -> ModuleType:
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise
        pytest.fail(f"{capability} is missing: add {module_name}", pytrace=False)


def _python_compiler():
    authoring = _require(
        "unilabos.workflow.from_python_script", "Canonical Python compiler capability"
    )
    compiler = getattr(authoring, "compile_python_script", None)
    if compiler is None:
        pytest.fail(
            "Canonical Python compiler capability is missing: "
            "unilabos.workflow.from_python_script.compile_python_script",
            pytrace=False,
        )
    return compiler


def _action_catalog() -> dict[str, dict[str, object]]:
    return {
        "balance.measure": {
            "inputs": {"sample": {"type": "string"}},
            "outputs": {"mass": {"type": "number"}},
        },
        "pump.dose": {
            "inputs": {"amount": {"type": "number"}},
            "outputs": {},
        },
        "pump.label": {
            "inputs": {"label": {"type": "string"}},
            "outputs": {},
        },
    }


def test_two_line_python_named_output_compiles_to_canonical_binding() -> None:
    source = "mass = balance.measure(sample='sample-1')\npump.dose(amount=mass)\n"

    revision = _python_compiler()(source, action_catalog=_action_catalog())

    dose = next(node for node in revision.invocations if node.node_id == "dose-2")
    binding = dose.input_bindings["amount"]
    assert binding.kind == "node_output"
    assert binding.node_id == "measure-1"
    assert binding.output == "mass"


def test_literal_and_runtime_parameter_compile_as_tagged_bindings() -> None:
    source = (
        "mass = balance.measure(sample='sample-1')\n"
        "pump.dose(amount=RuntimeParameter('target_amount', default=5.0))\n"
    )

    revision = _python_compiler()(source, action_catalog=_action_catalog())

    measure, dose = revision.invocations
    assert measure.input_bindings["sample"].kind == "literal"
    assert measure.input_bindings["sample"].value == "sample-1"
    assert dose.input_bindings["amount"].kind == "runtime_parameter"
    assert dose.input_bindings["amount"].parameter == "target_amount"
    assert dose.input_bindings["amount"].default == 5.0


@pytest.mark.parametrize(
    ("outputs", "expected_code"),
    [
        ({"measure-1": {"other": 1.25}}, "MISSING_NODE_OUTPUT"),
        ({"measure-1": {"mass": "not-a-number"}}, "BINDING_TYPE_MISMATCH"),
    ],
)
def test_preflight_rejects_bad_result_binding_with_zero_dispatch(
    outputs: dict[str, dict[str, object]], expected_code: str
) -> None:
    bindings = _require("unilabos.workflow.bindings", "Runtime binding preflight capability")
    dispatched: list[dict[str, object]] = []

    with pytest.raises(bindings.BindingPreflightError) as caught:
        bindings.preflight_and_dispatch(
            input_bindings={
                "amount": bindings.NodeOutputRef(node_id="measure-1", output="mass")
            },
            input_schema={"amount": {"type": "number"}},
            node_outputs=outputs,
            runtime_parameters={},
            dispatch=dispatched.append,
        )

    assert caught.value.code == expected_code
    assert dispatched == []
