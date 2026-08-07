"""Wire graph motion plan to MoveItRuntime without coupling to --visual."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from unilabos.device_mesh.motion_runtime_plan import (
    MotionRuntimePlan,
    plan_motion_runtime,
)
from unilabos.device_mesh.moveit_runtime import MoveItRuntime


def build_motion_runtime_plan(
    devices: Mapping[str, Mapping[str, Any]] | None,
    *,
    visual: str,
) -> MotionRuntimePlan:
    """Project graph devices onto the orthogonal MoveIt / RViz plan."""

    return plan_motion_runtime(devices or {}, visual=visual)


def compile_workspace_package_observations(
    workspace_root: str,
) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    """Compile one workspace into the PackageSource / PackageCatalog pair MoveIt needs."""

    from unilabos.package_manager import WorkspaceSource, compile_package_source

    source = WorkspaceSource(workspace_root)
    catalog = compile_package_source(source)
    return (source,), (catalog,)


def create_moveit_runtime(
    devices: Mapping[str, Mapping[str, Any]],
    plan: MotionRuntimePlan,
    *,
    package_sources: Sequence[Any],
    package_catalogs: Sequence[Any],
    runtime_parent: str | None,
) -> MoveItRuntime:
    """Build MoveItRuntime; RViz is taken only from ``plan.enable_rviz_view``."""

    return MoveItRuntime(
        devices,
        moveit_device_ids=plan.moveit_device_ids,
        package_sources=package_sources,
        package_catalogs=package_catalogs,
        enable_rviz=plan.enable_rviz_view,
        runtime_parent=runtime_parent,
    )


def should_use_legacy_resource_visualization(plan: MotionRuntimePlan) -> bool:
    """Legacy Adapter only when MoveIt does not own the motion / RViz stack."""

    return not plan.moveit_enabled and (
        plan.enable_rviz_view or plan.enable_web_view
    )


__all__ = [
    "build_motion_runtime_plan",
    "compile_workspace_package_observations",
    "create_moveit_runtime",
    "should_use_legacy_resource_visualization",
]
