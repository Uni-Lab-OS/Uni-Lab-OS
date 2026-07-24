"""Runtime ProfileV1 validator must agree with the shared JSON-Schema corpus."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator

from unilabos.runtime.profile_loader import ProfileLoader, ProfileValidationError


TEMPLATES_ROOT = Path(__file__).resolve().parents[3] / "Uni-Lab-Templates"
SCHEMA_PATH = TEMPLATES_ROOT / "schemas" / "profile-v1.schema.json"
FIXTURE_PATH = (
    TEMPLATES_ROOT / "tests" / "fixtures" / "profile_v1_conformance.yaml"
)


def _load_mapping(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


FIXTURES = _load_mapping(FIXTURE_PATH)
CASES = FIXTURES["cases"]


def _deep_merge(target: dict[str, Any], patch: dict[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def _profile_for(case: dict[str, Any]) -> dict[str, Any]:
    profile = copy.deepcopy(FIXTURES["base_profile"])
    _deep_merge(profile, case.get("profile_patch", {}))
    for path in case.get("remove_paths", []):
        cursor = profile
        parts = path.split(".")
        for part in parts[:-1]:
            cursor = cursor[part]
        cursor.pop(parts[-1])
    return profile


def _runtime_accepts(
    *,
    root: Path,
    profile: dict[str, Any],
) -> bool:
    (root / "device.yaml").write_text(
        yaml.safe_dump(FIXTURES["device_spec"], sort_keys=False),
        encoding="utf-8",
    )
    profile_path = root / "profile.yaml"
    profile_path.write_text(
        yaml.safe_dump(profile, sort_keys=False),
        encoding="utf-8",
    )
    try:
        ProfileLoader(driver_catalog={"generic_driver": object()}).load(
            profile_path
        )
    except ProfileValidationError:
        return False
    return True


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_runtime_and_schema_agree_on_shared_profile_v1_fixture(
    case: dict[str, Any],
    tmp_path: Path,
) -> None:
    profile = _profile_for(case)
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    schema_accepts = not list(
        Draft202012Validator(schema).iter_errors(profile)
    )
    runtime_accepts = _runtime_accepts(root=tmp_path, profile=profile)

    assert schema_accepts is case["expected_valid"]
    assert runtime_accepts is case["expected_valid"]
    assert runtime_accepts is schema_accepts
