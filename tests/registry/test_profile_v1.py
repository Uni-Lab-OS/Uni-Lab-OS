"""ProfileV1 schema and public loader acceptance contract."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator

from unilabos.runtime.profile_loader import ProfileLoader, ProfileValidationError


TEMPLATES_ROOT = Path(__file__).resolve().parents[3] / "Uni-Lab-Templates"
PROFILE_SCHEMA = TEMPLATES_ROOT / "schemas" / "profile-v1.schema.json"
REFERENCE_PACKAGE = TEMPLATES_ROOT / "packages" / "ptlc_station" / "package.yaml"
REFERENCE_SPEC = TEMPLATES_ROOT / "specs" / "ptlc_station.yaml"
DRIVER_CATALOG = {"generic_plc_macro": object()}


def _load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _write_profile_copy(
    root: Path,
    *,
    mutate_profile: Callable[[dict[str, Any]], None] | None = None,
    mutate_spec: Callable[[dict[str, Any]], None] | None = None,
) -> Path:
    profile = copy.deepcopy(_load_yaml(REFERENCE_PACKAGE))
    spec = copy.deepcopy(_load_yaml(REFERENCE_SPEC))
    profile["device_spec"] = "device.yaml"
    if mutate_profile is not None:
        mutate_profile(profile)
    if mutate_spec is not None:
        mutate_spec(spec)
    (root / "device.yaml").write_text(
        yaml.safe_dump(spec, sort_keys=False),
        encoding="utf-8",
    )
    profile_path = root / "package.yaml"
    profile_path.write_text(
        yaml.safe_dump(profile, sort_keys=False),
        encoding="utf-8",
    )
    return profile_path


def _load_profile(path: Path):
    return ProfileLoader(driver_catalog=DRIVER_CATALOG).load(path)


def test_profile_v1_schema_exists_and_accepts_reference_package() -> None:
    assert PROFILE_SCHEMA.is_file(), (
        "Uni-Lab-Templates/schemas/profile-v1.schema.json is required"
    )
    schema = json.loads(PROFILE_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(_load_yaml(REFERENCE_PACKAGE))


def test_public_profile_loader_accepts_reference_package() -> None:
    loaded = _load_profile(REFERENCE_PACKAGE)

    assert loaded.profile_id == "ptlc_station"
    assert loaded.driver_binding["driver_key"] == "generic_plc_macro"
    assert "ptlc_station.develop" in loaded.action_catalog


def test_profile_v1_rejects_unknown_top_level_fields(tmp_path: Path) -> None:
    profile_path = _write_profile_copy(
        tmp_path,
        mutate_profile=lambda profile: profile.update(
            {"unexpected_extension": True}
        ),
    )

    with pytest.raises(
        ProfileValidationError,
        match="(?i)unexpected_extension|additional|unknown",
    ):
        _load_profile(profile_path)


@pytest.mark.parametrize("schema_version", [2, "1", None])
def test_profile_v1_rejects_invalid_schema_version(
    tmp_path: Path,
    schema_version: object,
) -> None:
    profile_path = _write_profile_copy(
        tmp_path,
        mutate_profile=lambda profile: profile.update(
            {"schema_version": schema_version}
        ),
    )

    with pytest.raises(ProfileValidationError, match="(?i)schema_version"):
        _load_profile(profile_path)


@pytest.mark.parametrize("profile_id", ["../escape", "profile id", ""])
def test_profile_v1_rejects_invalid_profile_id(
    tmp_path: Path,
    profile_id: str,
) -> None:
    profile_path = _write_profile_copy(
        tmp_path,
        mutate_profile=lambda profile: profile.update({"profile_id": profile_id}),
    )

    with pytest.raises(ProfileValidationError, match="(?i)profile_id"):
        _load_profile(profile_path)


def test_profile_v1_rejects_incomplete_driver_binding(tmp_path: Path) -> None:
    def remove_connection_ref(profile: dict[str, Any]) -> None:
        profile["default_device_binding"].pop("connection_ref")

    profile_path = _write_profile_copy(
        tmp_path,
        mutate_profile=remove_connection_ref,
    )

    with pytest.raises(
        ProfileValidationError,
        match="(?i)connection_ref|device binding",
    ):
        _load_profile(profile_path)


def test_profile_v1_rejects_missing_device_spec(tmp_path: Path) -> None:
    profile_path = _write_profile_copy(
        tmp_path,
        mutate_profile=lambda profile: profile.update(
            {"device_spec": "missing-device.yaml"}
        ),
    )

    with pytest.raises(ProfileValidationError, match="(?i)device|not found"):
        _load_profile(profile_path)


def test_profile_v1_rejects_invalid_device_spec_contract(tmp_path: Path) -> None:
    profile_path = _write_profile_copy(
        tmp_path,
        mutate_spec=lambda spec: spec.update({"schema_version": 1}),
    )

    with pytest.raises(
        ProfileValidationError,
        match="(?i)device spec|schema_version",
    ):
        _load_profile(profile_path)


def test_profile_v1_rejects_action_without_driver_macro(tmp_path: Path) -> None:
    def remove_action_macro(profile: dict[str, Any]) -> None:
        profile["driver_config"]["macros"].pop("spotting")

    profile_path = _write_profile_copy(
        tmp_path,
        mutate_profile=remove_action_macro,
    )

    with pytest.raises(ProfileValidationError, match="(?i)spotting|action|macro"):
        _load_profile(profile_path)


def test_profile_v1_rejects_unknown_resource_reference(tmp_path: Path) -> None:
    def break_resource_reference(spec: dict[str, Any]) -> None:
        spec["actions"][0]["resource_claims"][0]["resource_ref"] = (
            "missing-resource"
        )

    profile_path = _write_profile_copy(
        tmp_path,
        mutate_spec=break_resource_reference,
    )

    with pytest.raises(ProfileValidationError, match="(?i)resource"):
        _load_profile(profile_path)


def test_profile_v1_rejects_stage_mapping_to_unknown_action(
    tmp_path: Path,
) -> None:
    def break_stage_mapping(profile: dict[str, Any]) -> None:
        profile["recipe_import_mapping"]["spotting"] = (
            "ptlc_station.missing_action"
        )

    profile_path = _write_profile_copy(
        tmp_path,
        mutate_profile=break_stage_mapping,
    )

    with pytest.raises(ProfileValidationError, match="(?i)stage|action"):
        _load_profile(profile_path)
