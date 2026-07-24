"""Quick Debug Alpha ActionContract v2 acceptance tests.

Imports happen inside tests so an absent v2 capability is reported as an
intentional RED assertion rather than as a collection/import accident.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import ValidationError

from unilabos.registry.decorators import (
    ActionInputHandle,
    ActionOutputHandle,
    action,
    get_action_meta,
)


TEMPLATES_RESOURCE_SCHEMA = (
    Path(__file__).resolve().parents[3]
    / "Uni-Lab-Templates"
    / "schemas"
    / "device-template-v2.schema.json"
)


def _contract_api() -> ModuleType:
    try:
        return importlib.import_module("unilabos.registry.action_contract")
    except ModuleNotFoundError as exc:
        if exc.name != "unilabos.registry.action_contract":
            raise
        pytest.fail(
            "ActionContract v2 capability is missing: add "
            "unilabos.registry.action_contract",
            pytrace=False,
        )


def _valid_recovery(api: ModuleType):
    return api.RecoveryContract(
        idempotency="reconcile_before_retry",
        cancel="reconcile_required",
        timeout="reconcile_required",
        disconnect="reconcile_required",
        estop="needs_drain",
    )


def _valid_macro_contract(api: ModuleType):
    return api.ActionContract(
        schema_version="2",
        execution_kind=api.ExecutionKind.DEVICE_MACRO,
        material_mode=api.MaterialMode.PASS_THROUGH,
        effects=(
            api.MaterialEffectTemplate(
                port="plate",
                op="move",
                location_from="input_slot",
                location_to="develop_tank",
            ),
        ),
        resource_claims=(
            api.ResourceClaimTemplate(
                resource_type="develop_tank",
                selector="planner",
                quantity=1,
                scope="until_handoff",
                mode="exclusive",
            ),
        ),
        timing=api.TimingContract(
            estimated_duration_s=420.0,
            timeout_s=900.0,
            setup_s=5.0,
            cleanup_s=10.0,
        ),
        recovery=_valid_recovery(api),
    )


def _assert_typed_validation_error(error: BaseException, *needles: str) -> None:
    assert isinstance(error, ValidationError) or error.__class__.__name__ == (
        "ActionContractValidationError"
    ), f"expected a typed validation error, got {type(error).__name__}: {error}"
    message = str(error).lower()
    assert all(needle.lower() in message for needle in needles), message


def _templates_claim_enum(field: str) -> tuple[str, ...]:
    schema = json.loads(TEMPLATES_RESOURCE_SCHEMA.read_text(encoding="utf-8"))
    values = schema["$defs"]["resourceClaim"]["properties"][field]["enum"]
    return tuple(str(value) for value in values)


def _model_property_enum(model: type, field: str) -> tuple[str, ...]:
    schema = model.model_json_schema()
    property_schema = schema["properties"][field]
    if "$ref" in property_schema:
        target = schema
        for part in property_schema["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        property_schema = target
    return tuple(str(value) for value in property_schema.get("enum", ()))


def test_v2_contract_ports_claims_timing_and_recovery_round_trip() -> None:
    api = _contract_api()
    contract = _valid_macro_contract(api)
    handles = [
        ActionInputHandle(
            key="recipe",
            domain="data",
            data_type="object",
            label="Recipe",
            required=True,
            schema_ref="#/$defs/Recipe",
            binding="argument",
        ),
        ActionInputHandle(
            key="plate",
            domain="material",
            data_type="ptlc_plate",
            label="Input plate",
            required=True,
            binding="context",
        ),
        ActionOutputHandle(
            key="plate",
            domain="material",
            data_type="ptlc_plate",
            label="Developed plate",
            required=True,
            binding="result",
            same_identity_as="plate",
        ),
    ]

    @action(handles=handles, contract=contract)
    def develop(recipe: dict[str, object]) -> dict[str, object]:
        return recipe

    metadata = get_action_meta(develop)
    assert metadata is not None
    assert metadata["contract"] == contract.model_dump(mode="json", exclude_none=True)
    assert metadata["handles"]["input"][0]["domain"] == "data"
    assert metadata["handles"]["input"][1]["domain"] == "material"
    assert metadata["handles"]["output"][0]["binding"] == "result"
    assert metadata["handles"]["output"][0]["same_identity_as"] == "plate"

    restored = api.ActionContract.model_validate_json(contract.model_dump_json())
    assert restored == contract
    assert restored.timing.estimated_duration_s == 420.0
    assert restored.resource_claims[0].scope == "until_handoff"


@pytest.mark.parametrize("field", ["scope", "mode"])
def test_resource_claim_enum_schema_matches_templates_exactly(field: str) -> None:
    api = _contract_api()

    assert set(_model_property_enum(api.ResourceClaimTemplate, field)) == set(
        _templates_claim_enum(field)
    )


@pytest.mark.parametrize("field", ["scope", "mode"])
def test_resource_claim_accepts_every_templates_enum_value(field: str) -> None:
    api = _contract_api()
    for value in _templates_claim_enum(field):
        values = {
            "resource_type": "process_cell",
            "selector": "planner",
            field: value,
        }

        claim = api.ResourceClaimTemplate(**values)

        assert getattr(claim, field) == value


@pytest.mark.parametrize("field", ["scope", "mode"])
def test_resource_claim_rejects_values_outside_templates_enum(field: str) -> None:
    api = _contract_api()
    values = {
        "resource_type": "process_cell",
        "selector": "planner",
        field: "not-a-templates-value",
    }

    with pytest.raises(ValidationError):
        api.ResourceClaimTemplate(**values)


def test_duplicate_port_is_a_typed_validation_error() -> None:
    api = _contract_api()
    duplicate_handles = [
        ActionInputHandle(key="plate", domain="material", data_type="plate", label="in"),
        ActionInputHandle(key="plate", domain="material", data_type="plate", label="again"),
    ]

    with pytest.raises(Exception) as caught:

        @action(handles=duplicate_handles, contract=_valid_macro_contract(api))
        def invalid_duplicate() -> None:
            return None

    _assert_typed_validation_error(caught.value, "duplicate", "plate")


def test_effect_with_material_mode_none_is_a_typed_validation_error() -> None:
    api = _contract_api()
    with pytest.raises(Exception) as caught:
        api.ActionContract(
            schema_version="2",
            execution_kind=api.ExecutionKind.ATOMIC,
            material_mode=api.MaterialMode.NONE,
            effects=(api.MaterialEffectTemplate(port="plate", op="consume"),),
            resource_claims=(),
            timing=api.TimingContract(estimated_duration_s=1.0),
            recovery=None,
        )
    _assert_typed_validation_error(caught.value, "material", "effect")


def test_device_macro_without_recovery_is_a_typed_validation_error() -> None:
    api = _contract_api()
    with pytest.raises(Exception) as caught:
        api.ActionContract(
            schema_version="2",
            execution_kind=api.ExecutionKind.DEVICE_MACRO,
            material_mode=api.MaterialMode.NONE,
            effects=(),
            resource_claims=(),
            timing=api.TimingContract(estimated_duration_s=1.0),
            recovery=None,
        )
    _assert_typed_validation_error(caught.value, "device_macro", "recovery")
