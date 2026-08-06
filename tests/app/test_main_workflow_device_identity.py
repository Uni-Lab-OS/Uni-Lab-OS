from __future__ import annotations

from types import SimpleNamespace

from unilabos.app.main import (
    _build_workflow_device_identity_map,
    _workflow_ros_execution_mode,
    parse_args,
)


def test_workflow_device_identity_map_includes_host_and_graph_devices() -> None:
    resource_tree_set = SimpleNamespace(
        all_nodes=[
            SimpleNamespace(
                res_content=SimpleNamespace(
                    type="device",
                    uuid="robot-uuid",
                    id="robot",
                )
            ),
            SimpleNamespace(
                res_content=SimpleNamespace(
                    type="container",
                    uuid="beaker-uuid",
                    id="beaker",
                )
            ),
        ]
    )

    identities = _build_workflow_device_identity_map(resource_tree_set)

    assert identities == {
        "host_node": "host_node",
        "robot-uuid": "robot",
        "robot": "robot",
    }


def test_workflow_ros_execution_requires_workspace_ros_and_explicit_mode() -> None:
    assert (
        _workflow_ros_execution_mode(
            workspace_attached=True,
            backend="ros",
            test_mode=True,
            physical_execution_enabled=False,
        )
        == "simulated"
    )
    assert (
        _workflow_ros_execution_mode(
            workspace_attached=True,
            backend="ros",
            test_mode=False,
            physical_execution_enabled=True,
        )
        == "physical"
    )
    assert (
        _workflow_ros_execution_mode(
            workspace_attached=True,
            backend="ros",
            test_mode=False,
            physical_execution_enabled=False,
        )
        is None
    )
    assert (
        _workflow_ros_execution_mode(
            workspace_attached=False,
            backend="ros",
            test_mode=True,
            physical_execution_enabled=True,
        )
        is None
    )


def test_cli_parses_explicit_workflow_physical_execution_opt_in() -> None:
    args = parse_args().parse_args(["--enable_workflow_physical_execution"])

    assert args.enable_workflow_physical_execution is True
