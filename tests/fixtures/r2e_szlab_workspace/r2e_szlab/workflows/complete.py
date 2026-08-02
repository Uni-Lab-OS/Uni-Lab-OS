"""Stable PackageCatalog identity used by the ROS CLI execution tracer."""

from unilabos.workflow.authoring import workflow_definition


@workflow_definition(
    workflow_uuid="65000000-0000-4000-8000-000000000001",
    displayname="R2E SZLab complete workflow",
)
def complete_workflow() -> None:
    """The HTTP graph save below materializes this declared draft for execution."""
