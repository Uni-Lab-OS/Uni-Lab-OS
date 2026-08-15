"""Stable inventory source identity for copied physical graphs."""

from __future__ import annotations

import pytest

from unilabos.app.main import parse_args, resolve_resource_graph_source_identity


def test_resource_graph_source_identity_preserves_existing_startup_modes() -> None:
    """Only an explicit caller identity may override existing source semantics."""

    assert resolve_resource_graph_source_identity(
        physical_graph_path="/workspace/graph.json"
    ) == "/workspace/graph.json"
    assert resolve_resource_graph_source_identity(
        physical_graph_path=None
    ) == "remote-startup.json"
    assert resolve_resource_graph_source_identity(
        physical_graph_path="/runtime/selected-graph.json",
        explicit_source_identity="bioyond_day1_virtual.json",
    ) == "bioyond_day1_virtual.json"
    arguments = parse_args().parse_args(
        ["--resource_graph_source_id", "bioyond_day1_virtual.json"]
    )
    assert arguments.resource_graph_source_id == "bioyond_day1_virtual.json"


@pytest.mark.parametrize("invalid", ["", "   ", 7])
def test_resource_graph_source_identity_rejects_invalid_explicit_values(
    invalid: object,
) -> None:
    """An explicit namespace must be a non-empty string instead of falling back."""

    with pytest.raises((TypeError, ValueError)):
        resolve_resource_graph_source_identity(
            physical_graph_path="/runtime/selected-graph.json",
            explicit_source_identity=invalid,
        )
