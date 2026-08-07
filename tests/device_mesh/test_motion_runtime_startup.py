from unilabos.device_mesh.motion_runtime_plan import plan_motion_runtime
from unilabos.device_mesh.motion_runtime_startup import (
    build_motion_runtime_plan,
    should_use_legacy_resource_visualization,
)


def _robot(backend: str) -> dict:
    return {
        "robot": {
            "id": "robot",
            "class": "community.szlab_poly_studio.szlab_mixer_robot",
            "config": {"standard_execution_backend": backend},
        }
    }


def test_moveit_sim_skips_legacy_visualization_even_with_rviz() -> None:
    plan = build_motion_runtime_plan(_robot("moveit_sim"), visual="rviz")

    assert plan.moveit_enabled is True
    assert plan.enable_rviz_view is True
    assert should_use_legacy_resource_visualization(plan) is False


def test_rviz_without_moveit_uses_legacy_visualization() -> None:
    plan = build_motion_runtime_plan(_robot("plc_program"), visual="rviz")

    assert plan.moveit_enabled is False
    assert plan.enable_rviz_view is True
    assert should_use_legacy_resource_visualization(plan) is True


def test_moveit_sim_without_visual_still_skips_legacy() -> None:
    plan = build_motion_runtime_plan(_robot("moveit_sim"), visual="disable")

    assert plan.moveit_enabled is True
    assert plan.enable_rviz_view is False
    assert should_use_legacy_resource_visualization(plan) is False


def test_build_plan_matches_plan_motion_runtime() -> None:
    devices = _robot("moveit_sim")
    assert build_motion_runtime_plan(devices, visual="web") == plan_motion_runtime(
        devices, visual="web"
    )
