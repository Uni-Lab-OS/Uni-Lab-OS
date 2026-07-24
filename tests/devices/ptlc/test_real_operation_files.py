"""Regression locks for pTLC's real Operation sources.

These fixtures are byte-for-byte git blobs, not workflows invented for tests.
The manifest records their authoritative repository paths and blob hashes.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "real_operations"


def _git_blob_hash(raw: bytes) -> str:
    header = f"blob {len(raw)}\0".encode()
    return hashlib.sha1(header + raw).hexdigest()  # noqa: S324 - git object identity


def _load(relative_path: str) -> dict[str, Any]:
    raw = (FIXTURE_ROOT / relative_path).read_text(encoding="utf-8")
    document = yaml.safe_load(raw)
    assert isinstance(document, dict)
    return document


def _non_comment_ops(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for step in steps:
        if step.get("op") != "comment":
            result.append(step)
        for child_key in ("then", "else", "body", "finally"):
            children = step.get(child_key)
            if isinstance(children, list):
                result.extend(_non_comment_ops(children))
    return result


def test_snapshots_match_the_recorded_authoritative_git_blobs() -> None:
    manifest = _load("PROVENANCE.yaml")

    assert manifest["source_repository"] == "pTLC_platformUI"
    assert manifest["source_ref"] == "origin/codex/ui-upper-next"
    assert [entry["source_path"] for entry in manifest["files"]] == [
        "eit_ptlc/config/operation/02_develop/develop_execute.yaml",
        "eit_ptlc/config/operation/02_develop/develop_prepare.yaml",
        "eit_ptlc/config/operation/02_develop/develop_standby.yaml",
        "eit_ptlc/config/operation/08_rail/rail_move_safe.yaml",
    ]
    for entry in manifest["files"]:
        fixture = FIXTURE_ROOT / entry["fixture_path"]
        assert _git_blob_hash(fixture.read_bytes()) == entry["git_blob"]


def test_real_develop_execute_is_the_14_operation_workflow() -> None:
    workflow = _load("02_develop/develop_execute.yaml")
    operations = _non_comment_ops(workflow["body"])

    assert workflow["schema"] == "ptlc.script/v1"
    assert workflow["kind"] == "operation"
    assert workflow["name"] == "develop_execute"
    assert len(operations) == 14
    assert {step["op"] for step in operations} == {
        "call",
        "human",
        "if",
        "run_script",
    }
    assert sum(step["op"] == "call" for step in operations) == 6
    assert sum(step["op"] == "human" for step in operations) == 3
    assert sum(step["op"] == "if" for step in operations) == 4
    assert sum(step["op"] == "run_script" for step in operations) == 1
    assert next(step for step in operations if step["op"] == "run_script")[
        "script"
    ] == "develop_standby"


def test_real_develop_prepare_is_the_12_operation_two_finally_workflow() -> None:
    workflow = _load("02_develop/develop_prepare.yaml")
    operations = _non_comment_ops(workflow["body"])

    assert workflow["schema"] == "ptlc.script/v1"
    assert workflow["kind"] == "operation"
    assert workflow["name"] == "develop_prepare"
    assert len(operations) == 12
    assert sum(step["op"] == "call" for step in operations) == 10
    assert sum(step["op"] == "try" for step in operations) == 2
    try_steps = [step for step in operations if step["op"] == "try"]
    assert [step["finally"][0]["action"] for step in try_steps] == [
        "pump.vacuum_off",
        "pump.vacuum_off",
    ]


def test_checked_in_nested_scripts_are_real_and_resolve_transitively() -> None:
    standby = _load("02_develop/develop_standby.yaml")
    rail_move = _load("08_rail/rail_move_safe.yaml")

    assert standby["body"][1] == {
        "op": "run_script",
        "script": "rail_move_safe",
        "inputs": {"target": {"lit": 5}},
        "outputs": {},
    }
    assert [step.get("action") for step in rail_move["body"] if step["op"] == "call"] == [
        "robot.require_anchor",
        "rail.move",
    ]
