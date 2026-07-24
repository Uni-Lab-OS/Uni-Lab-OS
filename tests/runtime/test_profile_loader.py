"""Generic declarative profile loading and reference-preflight contract."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml


def _api() -> ModuleType:
    try:
        return importlib.import_module("unilabos.runtime.profile_loader")
    except ModuleNotFoundError as exc:
        if exc.name != "unilabos.runtime.profile_loader":
            raise
        pytest.fail("generic ProfileLoader capability is missing", pytrace=False)


def _write_profile(
    root: Path,
    *,
    driver_key: str = "generic_driver",
    mapped_action: str = "generic_station.measure",
    resource_ref: str = "measurement-cell-1",
) -> Path:
    spec = {
        "schema_version": 2,
        "device": {"id": "generic_station", "display_name": "Generic Station"},
        "actions": [
            {
                "id": "measure",
                "execution_kind": "device_macro",
                "params": [{"name": "sample", "type": "string", "required": True}],
                "results": [{"name": "reading", "type": "number"}],
                "resource_claims": [
                    {
                        "resource_ref": resource_ref,
                        "resource_type": "measurement_cell",
                        "scope": "action",
                        "mode": "exclusive",
                    }
                ],
                "effects": [{"op": "observe", "resource_ref": resource_ref}],
                "timing": {"estimated_duration_s": 3, "timeout_s": 30},
                "recovery": {
                    "idempotency": "reconcile_before_retry",
                    "cancel": "reconcile_required",
                    "timeout": "reconcile_required",
                    "disconnect": "reconcile_required",
                    "estop": "needs_drain",
                },
            }
        ],
    }
    profile = {
        "schema_version": 1,
        "profile_id": "generic_profile",
        "device_spec": "generic_station.yaml",
        "default_device_binding": {
            "device_id": "generic_station",
            "driver_key": driver_key,
            "connection_ref": "GENERIC_CONNECTION",
        },
        "resource_topology": {
            "resources": [
                {
                    "id": "measurement-cell-1",
                    "resource_type": "measurement_cell",
                }
            ]
        },
        "driver_config": {
            "macros": {"measure": [{"call": "measure"}]},
        },
        "recipe_import_mapping": {"measure_stage": mapped_action},
    }
    (root / "generic_station.yaml").write_text(
        yaml.safe_dump(spec, sort_keys=False),
        encoding="utf-8",
    )
    profile_path = root / "profile.yaml"
    profile_path.write_text(
        yaml.safe_dump(profile, sort_keys=False),
        encoding="utf-8",
    )
    return profile_path


def test_profile_loader_builds_catalog_binding_resources_and_stage_map(
    tmp_path: Path,
) -> None:
    api = _api()
    profile_path = _write_profile(tmp_path)

    loaded = api.ProfileLoader(
        driver_catalog={"generic_driver": object()},
    ).load(profile_path)

    assert loaded.profile_id == "generic_profile"
    assert loaded.driver_binding == {
        "device_id": "generic_station",
        "driver_key": "generic_driver",
        "connection_ref": "GENERIC_CONNECTION",
    }
    assert loaded.resources == {
        "measurement-cell-1": {"resource_type": "measurement_cell"}
    }
    assert loaded.legacy_stage_map == {
        "measure_stage": "generic_station.measure"
    }
    action = loaded.action_catalog["generic_station.measure"]
    assert action["inputs"] == {
        "sample": {"type": "string", "required": True}
    }
    assert action["outputs"] == {"reading": {"type": "number"}}
    assert action["resource_claims"][0]["resource_ref"] == "measurement-cell-1"
    assert action["effects"] == [
        {"op": "observe", "resource_ref": "measurement-cell-1"}
    ]


@pytest.mark.parametrize(
    ("profile_kwargs", "driver_catalog", "message"),
    [
        ({"driver_key": "missing_driver"}, {}, "driver"),
        (
            {"mapped_action": "generic_station.missing_action"},
            {"generic_driver": object()},
            "action",
        ),
        (
            {"resource_ref": "missing-resource"},
            {"generic_driver": object()},
            "resource",
        ),
    ],
)
def test_profile_loader_rejects_unresolved_references_before_runtime(
    tmp_path: Path,
    profile_kwargs: dict[str, str],
    driver_catalog: dict[str, Any],
    message: str,
) -> None:
    api = _api()
    profile_path = _write_profile(tmp_path, **profile_kwargs)

    with pytest.raises(api.ProfileValidationError, match=f"(?i){message}"):
        api.ProfileLoader(driver_catalog=driver_catalog).load(profile_path)


def test_generic_profile_loader_source_has_no_device_family_dependency() -> None:
    api = _api()
    source = Path(api.__file__).read_text(encoding="utf-8").lower()
    assert "ptlc" not in source
