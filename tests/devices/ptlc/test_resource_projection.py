"""Resource references resolve entirely through a generic LoadedProfile."""

from __future__ import annotations

import importlib
from pathlib import Path


OS_ROOT = Path(__file__).resolve().parents[3]
PROFILE_PATH = (
    OS_ROOT.parent
    / "Uni-Lab-Templates"
    / "packages"
    / "ptlc_station"
    / "package.yaml"
)


def _load_profile():
    driver_api = importlib.import_module("unilabos.devices.generic_plc_macro")
    profile_api = importlib.import_module("unilabos.runtime.profile_loader")
    return profile_api.ProfileLoader(
        driver_catalog={
            "generic_plc_macro": driver_api.DeclarativePLCMacroDriver,
        }
    ).load(PROFILE_PATH)


def test_loaded_profile_resolves_action_claims_to_declared_resources() -> None:
    profile = _load_profile()
    develop = profile.action_catalog["ptlc_station.develop"]
    resolved = {
        (
            claim["resource_ref"],
            profile.resources[claim["resource_ref"]]["resource_type"],
            claim["scope"],
            claim["mode"],
        )
        for claim in develop["resource_claims"]
    }

    assert (
        "develop-tank-pool",
        "develop_tank",
        "action",
        "exclusive",
    ) in resolved
    assert (
        "develop-prep-line",
        "develop_prep_line",
        "action",
        "exclusive",
    ) in resolved
    assert profile.resources["develop-tank-2"]["group_id"] == "develop-group-1"
    assert profile.resources["develop-tank-6"]["group_id"] == "develop-group-2"


def test_loaded_profile_resource_resolution_is_deterministic_without_guessing() -> None:
    first = _load_profile()
    second = _load_profile()

    assert first.resources == second.resources
    assert first.action_catalog == second.action_catalog
    assert not hasattr(first, "available_inventory")
    assert not hasattr(first, "selected_alternative_resource")
