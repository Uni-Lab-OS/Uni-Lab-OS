"""Legacy source import through a generic LoadedProfile."""

from __future__ import annotations

import importlib
import json
from pathlib import Path


FIXTURE = Path(__file__).parent / "fixtures" / "single_sample_golden.json"
OS_ROOT = Path(__file__).resolve().parents[3]
PROFILE_PATH = (
    OS_ROOT.parent
    / "Uni-Lab-Templates"
    / "packages"
    / "ptlc_station"
    / "package.yaml"
)


def _profile():
    driver_api = importlib.import_module("unilabos.devices.generic_plc_macro")
    profile_api = importlib.import_module("unilabos.runtime.profile_loader")
    return profile_api.ProfileLoader(
        driver_catalog={
            "generic_plc_macro": driver_api.DeclarativePLCMacroDriver,
        }
    ).load(PROFILE_PATH)


def _legacy_payload() -> dict[str, object]:
    golden = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return {
        "name": "single-sample",
        "stages": [
            {
                "name": step["macro"],
                "enabled": True,
                "params": step["params"],
            }
            for step in golden["recipe"]
        ],
    }


def test_loaded_profile_import_is_idempotent_and_preserves_stage_parameters() -> None:
    profile = _profile()
    payload = _legacy_payload()

    first = profile.import_legacy_source(
        payload,
        parameters={"sample_id": "sample-001"},
    )
    second = profile.import_legacy_source(
        payload,
        parameters={"sample_id": "sample-002"},
    )

    assert first.content_hash == second.content_hash
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert [node.action_ref for node in first.invocations] == [
        "ptlc_station.spotting",
        "ptlc_station.before_photo",
        "ptlc_station.develop",
        "ptlc_station.scrape",
        "ptlc_station.collect",
    ]
    assert first.invocations[0].input_bindings["band"].value == 1
    assert first.invocations[2].input_bindings["target_tank"].value == 2
    assert first.invocations[2].input_bindings["group_id"].value == 1
    assert all(
        "sample_id" not in invocation.input_bindings
        for invocation in first.invocations
    )


def test_loaded_profile_import_source_map_points_to_each_legacy_stage() -> None:
    profile = _profile()
    payload = _legacy_payload()
    revision = profile.import_legacy_source(
        payload,
        parameters={"sample_id": "sample-001"},
    )
    stages = payload["stages"]

    assert len(revision.source_map.entries) == len(stages)
    assert [entry.source_step_index for entry in revision.source_map.entries] == list(
        range(len(stages))
    )
    assert all(entry.compiled_node_ids for entry in revision.source_map.entries)
