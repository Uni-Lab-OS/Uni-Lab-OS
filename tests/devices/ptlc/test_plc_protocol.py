"""Golden PLC trace produced by a generic declarative macro driver."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
from pathlib import Path
from types import ModuleType

import pytest


FIXTURE = Path(__file__).parent / "fixtures" / "single_sample_golden.json"
OS_ROOT = Path(__file__).resolve().parents[3]
PROFILE_PATH = (
    OS_ROOT.parent
    / "Uni-Lab-Templates"
    / "packages"
    / "ptlc_station"
    / "package.yaml"
)


class FakePLC:
    """Protocol fake; performs no socket, OPC-UA, DDS, or wall-clock operation."""

    def __init__(self) -> None:
        self.trace: list[list[object]] = []

    async def write(self, tag: str, value: object) -> None:
        self.trace.append(["write", tag, value])

    async def wait_for(self, tag: str, value: object) -> None:
        self.trace.append(["wait", tag, value])

    async def start_stage(self, stage: str, channels: object = None) -> None:
        self.trace.append(["start_stage", stage])

    async def await_stage_done(self, stage: str, **_kwargs: object) -> None:
        self.trace.append(["await_stage_done", stage])

    async def send_recipe_params(self, params: dict[str, object]) -> None:
        mode = params.get("scrape_PhotoMode")
        if mode is not None:
            self.trace.append(["send_recipe_params", {"scrape_PhotoMode": mode}])

    async def await_stage_step(self, stage: str, step: int, **_kwargs: object) -> None:
        self.trace.append(["await_stage_step", stage, step])

    async def confirm_stage(self, stage: str) -> None:
        self.trace.append(["confirm_stage", stage])

    async def trigger_drain(self, tank_id: int) -> None:
        self.trace.append(["trigger_drain", tank_id])

    async def await_drain_done(self, tank_id: int, **_kwargs: object) -> None:
        self.trace.append(["await_drain_done", tank_id])


def _driver_api() -> ModuleType:
    try:
        return importlib.import_module("unilabos.devices.generic_plc_macro")
    except ModuleNotFoundError as exc:
        if exc.name != "unilabos.devices.generic_plc_macro":
            raise
        pytest.fail("generic declarative PLC macro driver is missing", pytrace=False)


def _profile_api() -> ModuleType:
    try:
        return importlib.import_module("unilabos.runtime.profile_loader")
    except ModuleNotFoundError as exc:
        if exc.name != "unilabos.runtime.profile_loader":
            raise
        pytest.fail("generic ProfileLoader capability is missing", pytrace=False)


def _loaded_profile():
    driver_api = _driver_api()
    return _profile_api().ProfileLoader(
        driver_catalog={
            "generic_plc_macro": driver_api.DeclarativePLCMacroDriver,
        }
    ).load(PROFILE_PATH)


def test_profile_macros_drive_complete_single_sample_golden_trace() -> None:
    golden = json.loads(FIXTURE.read_text(encoding="utf-8"))
    driver_api = _driver_api()
    profile = _loaded_profile()
    fake = FakePLC()
    driver = driver_api.DeclarativePLCMacroDriver(
        plc=fake,
        driver_config=profile.driver_config,
    )

    assert set(profile.driver_config["macros"]) >= {
        step["macro"] for step in golden["recipe"]
    }

    async def scenario() -> list[object]:
        results = []
        for step in golden["recipe"]:
            results.append(
                await driver.run_macro(
                    step["macro"],
                    inputs={"sample_id": golden["sample_id"], **step["params"]},
                )
            )
        return results

    results = asyncio.run(scenario())
    assert fake.trace == golden["plc_trace"]
    assert [result.macro for result in results] == [
        step["macro"] for step in golden["recipe"]
    ]
    assert all(result.terminal == "succeeded" for result in results)


def test_loaded_profile_owns_action_contract_and_driver_configuration() -> None:
    profile = _loaded_profile()
    develop = profile.action_catalog["ptlc_station.develop"]

    assert profile.driver_binding["driver_key"] == "generic_plc_macro"
    assert profile.driver_config["macros"]
    assert {
        (claim["resource_type"], claim["scope"])
        for claim in develop["resource_claims"]
    } >= {
        ("develop_tank", "action"),
        ("develop_prep_line", "action"),
    }
    for action_ref, action in profile.action_catalog.items():
        for claim in action["resource_claims"]:
            assert claim.get("mode", "exclusive") == "exclusive", action_ref
            assert int(claim.get("quantity", 1)) == 1, action_ref
            assert claim.get("scope", "action") == "action", action_ref
    assert develop["recovery"]["disconnect"] == "reconcile_required"
    assert develop["recovery"]["timeout"] == "reconcile_required"


def test_generic_driver_is_injected_and_contains_no_profile_specific_logic() -> None:
    driver_api = _driver_api()
    source = inspect.getsource(driver_api).lower()
    class_name = driver_api.DeclarativePLCMacroDriver.__name__.lower()
    golden = json.loads(FIXTURE.read_text(encoding="utf-8"))

    assert "ptlc" not in driver_api.__name__.lower()
    assert "ptlc" not in class_name
    assert "ptlc" not in source
    for step in golden["recipe"]:
        macro = step["macro"].lower()
        assert f'"{macro}"' not in source
        assert f"'{macro}'" not in source

    fake = FakePLC()
    profile = _loaded_profile()
    driver = driver_api.DeclarativePLCMacroDriver(
        plc=fake,
        driver_config=profile.driver_config,
    )
    assert driver.plc is fake
