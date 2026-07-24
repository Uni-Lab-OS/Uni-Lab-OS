"""pTLC remains Profile data while importing generic Canonical resource holds."""

from __future__ import annotations

from pathlib import Path

from unilabos.runtime.profile_loader import ProfileLoader


TEMPLATES_ROOT = Path(__file__).resolve().parents[3] / "Uni-Lab-Templates"
PTLC_PROFILE = TEMPLATES_ROOT / "packages" / "ptlc_station" / "package.yaml"


def test_ptlc_profile_imports_develop_and_scrape_hold_boundaries() -> None:
    profile = ProfileLoader(
        driver_catalog={"generic_plc_macro": object()}
    ).load(PTLC_PROFILE)

    revision = profile.import_legacy_source(
        {
            "name": "multi-band-ptlc",
            "stages": [
                {"name": "develop", "params": {"tank_id": 1}},
                {"name": "scrape", "params": {"band": 1}},
                {"name": "scrape", "params": {"band": 2}},
                {"name": "scrape", "params": {"band": 3}},
            ],
        }
    )

    assert [
        (
            hold.hold_id,
            hold.resource_ref,
            hold.scope,
            hold.acquire_node_id,
            hold.release_node_id,
        )
        for hold in revision.resource_holds
    ] == [
        (
            "develop-to-first-scrape",
            "develop-tank-pool",
            "until_handoff",
            "develop-1",
            "scrape-2",
        ),
        (
            "first-to-last-scrape",
            "photo-scrape-station",
            "workflow_block",
            "scrape-2",
            "scrape-4",
        ),
    ]
