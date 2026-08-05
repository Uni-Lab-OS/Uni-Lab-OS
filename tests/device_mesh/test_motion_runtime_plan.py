from unilabos.device_mesh.motion_runtime_plan import plan_motion_runtime


def graph(backend: str) -> dict:
    return {
        "robot": {
            "id": "robot",
            "class": "community.szlab_poly_studio.szlab_mixer_robot",
            "config": {"standard_execution_backend": backend},
        }
    }


def test_moveit_execution_does_not_depend_on_visual_mode() -> None:
    disabled = plan_motion_runtime(graph("moveit_sim"), visual="disable")
    web = plan_motion_runtime(graph("moveit_sim"), visual="web")
    rviz = plan_motion_runtime(graph("moveit_sim"), visual="rviz")

    assert disabled.moveit_enabled is True
    assert web.moveit_device_ids == disabled.moveit_device_ids
    assert rviz.moveit_device_ids == disabled.moveit_device_ids
    assert disabled.enable_rviz_view is False
    assert web.enable_rviz_view is False
    assert rviz.enable_rviz_view is True


def test_plc_execution_never_starts_moveit_merely_for_rviz() -> None:
    plan = plan_motion_runtime(graph("plc_program"), visual="rviz")

    assert plan.moveit_enabled is False
    assert plan.enable_rviz_view is True
    assert plan.planning_scene_required is False


def test_explicit_backend_overrides_legacy_class_name_heuristic() -> None:
    devices = graph("plc_program")
    devices["robot"]["class"] = "legacy.moveit.robot"

    assert plan_motion_runtime(devices, visual="disable").moveit_enabled is False


def test_physical_moveit_is_reported_as_unsupported_not_started_as_mock() -> None:
    plan = plan_motion_runtime(graph("moveit"), visual="disable")

    assert plan.moveit_enabled is False
    assert plan.unsupported_physical_device_ids == ("robot",)


def test_legacy_class_name_is_not_sent_to_package_runtime() -> None:
    devices = graph("")
    devices["robot"]["class"] = "legacy.moveit.robot"

    assert plan_motion_runtime(devices, visual="rviz").moveit_enabled is False
