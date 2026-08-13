"""Workflow Agent tools sharing the exact Domain client used by the CLI.

This module deliberately contains no process control, database reads, or Node
RPC.  MCP is an optional transport around :class:`DomainBackendClient`.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Mapping, Sequence

from unilabos.client.domain import DomainBackendClient
from unilabos.client.material_renderer import MaterialRendererClient
from unilabos.client.material_layout import MaterialLayoutClient
from unilabos.client.material_template import MaterialTemplateClient
from unilabos.client.material_visual_regression import (
    approve_material_baseline,
    compare_material_capture,
)


class WorkflowAgentTools:
    """Bounded, JSON-serializable Agent operations for one workspace."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        ak: str = "",
        sk: str = "",
    ) -> None:
        self.workspace = str(Path(workspace).expanduser().resolve())
        self.ak = ak
        self.sk = sk

    def _client(self) -> DomainBackendClient:
        return DomainBackendClient.discover(
            self.workspace,
            ak=self.ak,
            sk=self.sk,
        )

    def list_workflows(
        self, *, page: int = 1, page_size: int = 100, name: str = ""
    ) -> dict[str, Any]:
        with self._client() as client:
            return client.result(
                client.list_workflows(page=page, page_size=page_size, name=name)
            )

    def inspect_workflow(self, workflow_uuid: str) -> dict[str, Any]:
        with self._client() as client:
            return client.inspect_workflow(workflow_uuid)

    def inspect_task(
        self, task_uuid: str, *, event_limit: int = 100
    ) -> dict[str, Any]:
        with self._client() as client:
            return client.inspect_task(task_uuid, event_limit=event_limit)

    def run_workflow(
        self,
        workflow_uuid: str,
        *,
        run_mode: str = "normal",
        target_node_uuid: str | None = None,
        input_value: Mapping[str, Any] | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        with self._client() as client:
            return client.create_task(
                workflow_uuid,
                run_mode=run_mode,
                target_node_uuid=target_node_uuid,
                input_value=input_value,
                operation_id=operation_id,
            )

    def debug_workflow(
        self,
        workflow_uuid: str,
        *,
        start_node_uuid: str,
        breakpoint_node_uuids: Sequence[str] = (),
        input_value: Mapping[str, Any] | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        with self._client() as client:
            return client.create_debug_task(
                workflow_uuid,
                start_node_uuid=start_node_uuid,
                breakpoint_node_uuids=breakpoint_node_uuids,
                input_value=input_value,
                operation_id=operation_id,
            )

    def watch_task(
        self,
        task_uuid: str,
        *,
        after: int = 0,
        timeout: float = 300.0,
        max_events: int = 500,
    ) -> dict[str, Any]:
        with self._client() as client:
            events = list(
                client.watch_task(
                    task_uuid,
                    after=after,
                    timeout=timeout,
                    max_events=max_events,
                )
            )
            cursor = events[-1].get("cursor", after) if events else after
            return client.result(
                {"items": events, "nextCursor": cursor},
                task_uuid=task_uuid,
                cursor=int(cursor),
            )

    def command_task(
        self,
        task_uuid: str,
        command_type: str,
        *,
        target_node_uuid: str | None = None,
        hold_uuid: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        with self._client() as client:
            if hold_uuid:
                return client.command_debug_task(
                    task_uuid,
                    command_type,
                    hold_uuid=hold_uuid,
                    idempotency_key=idempotency_key,
                )
            return client.command_task(
                task_uuid,
                command_type,
                target_node_uuid=target_node_uuid,
                idempotency_key=idempotency_key,
            )

    def wait_authoring(
        self,
        workflow_uuid: str,
        *,
        after_revision: int,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        with self._client() as client:
            return client.wait_authoring(
                workflow_uuid,
                after_revision=after_revision,
                timeout=timeout,
            )

    def inspect_material_scene(
        self,
        *,
        view: str | None = None,
        show_sites: bool | None = None,
        show_material_transfers: bool | None = None,
        selected_material_ids: Sequence[str] = (),
        hidden_material_ids: Sequence[str] = (),
        layout_overrides: Sequence[Mapping[str, object]] = (),
        timeout: float = 30.0,
        headless: bool = False,
    ) -> dict[str, Any]:
        """检查当前已打开的真实物料 renderer，不读取 DOM 或另建场景。"""

        with MaterialRendererClient.discover(
            self.workspace, headless=headless, timeout=timeout
        ) as client:
            return client.inspect_scene(
                view=view,
                show_sites=show_sites,
                show_material_transfers=show_material_transfers,
                selected_material_ids=selected_material_ids,
                hidden_material_ids=hidden_material_ids,
                layout_overrides=layout_overrides,
                timeout=timeout,
            )

    def capture_material_scene(
        self,
        output: str,
        *,
        view: str | None = None,
        camera_preset: str = "default",
        viewport_width: int = 1440,
        viewport_height: int = 960,
        pixel_ratio: float = 1.0,
        show_sites: bool | None = None,
        show_material_transfers: bool | None = None,
        selected_material_ids: Sequence[str] = (),
        hidden_material_ids: Sequence[str] = (),
        layout_overrides: Sequence[Mapping[str, object]] = (),
        timeout: float = 30.0,
        headless: bool = False,
    ) -> dict[str, Any]:
        """截图当前已附着物料 renderer，并把 PNG 原子写入指定路径。"""

        with MaterialRendererClient.discover(
            self.workspace, headless=headless, timeout=timeout
        ) as client:
            return client.capture_scene(
                output,
                view=view,
                camera_preset=camera_preset,
                viewport=(viewport_width, viewport_height),
                pixel_ratio=pixel_ratio,
                show_sites=show_sites,
                show_material_transfers=show_material_transfers,
                selected_material_ids=selected_material_ids,
                hidden_material_ids=hidden_material_ids,
                layout_overrides=layout_overrides,
                timeout=timeout,
            )

    def inspect_material_layout(self, *, timeout: float = 120.0) -> dict[str, Any]:
        """Return source graph layout facts and its CAS revision."""

        return MaterialLayoutClient.discover(
            self.workspace, timeout=timeout
        ).inspect()

    def preview_material_layout(
        self,
        change_set: Mapping[str, object],
        *,
        expected_revision: str,
        timeout: float = 120.0,
        output: str | None = None,
        headless: bool = False,
    ) -> dict[str, Any]:
        """Compile a non-mutating layout candidate and disposable preview artifact."""

        result = MaterialLayoutClient.discover(
            self.workspace, timeout=timeout
        ).preview(change_set, expected_revision=expected_revision)
        if output:
            view = result.get("changeSet", {}).get("view", {})
            layout_overrides = result.get("changeSet", {}).get("nodes", [])
            with MaterialRendererClient.discover(
                self.workspace, headless=headless, timeout=timeout
            ) as renderer:
                capture = renderer.capture_scene(
                    output,
                    view=str(view.get("mode") or "2.5d"),
                    camera_preset=str(view.get("cameraPreset") or "default"),
                    viewport=(
                        int(view.get("viewport", {}).get("width") or 1440),
                        int(view.get("viewport", {}).get("height") or 960),
                    ),
                    layout_overrides=layout_overrides,
                    timeout=timeout,
                )
            result = {**result, "previewImage": capture.get("data")}
        return result

    def apply_material_layout(
        self,
        preview_id: str,
        *,
        expected_revision: str,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        """CAS-apply exactly one previously compiled layout preview."""

        return MaterialLayoutClient.discover(
            self.workspace, timeout=timeout
        ).apply(preview_id, expected_revision=expected_revision)

    def compare_material_scene(
        self,
        candidate: str,
        baseline: str,
        *,
        threshold: float,
    ) -> dict[str, Any]:
        """Compare captured pixels and stable structural facts."""

        return compare_material_capture(candidate, baseline, threshold=threshold)

    def approve_material_scene_baseline(
        self, candidate: str, baseline: str
    ) -> dict[str, Any]:
        """Explicitly approve and replace one visual baseline."""

        return approve_material_baseline(candidate, baseline)

    def validate_material_templates(
        self, *, timeout: float = 120.0
    ) -> dict[str, Any]:
        """Statically compile templates in an isolated process without publishing."""

        return MaterialTemplateClient(self.workspace, timeout=timeout).validate()


def build_mcp_server(
    workspace: str | Path,
    *,
    ak: str = "",
    sk: str = "",
) -> Any:
    """Build the optional MCP transport without making it a core dependency."""

    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as error:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            "MCP transport is not installed; install UniLab OS with the 'mcp' extra"
        ) from error

    tools = WorkflowAgentTools(workspace, ak=ak, sk=sk)
    server = FastMCP("UniLab Workspace")
    server.tool()(tools.list_workflows)
    server.tool()(tools.inspect_workflow)
    server.tool()(tools.inspect_task)
    server.tool()(tools.run_workflow)
    server.tool()(tools.debug_workflow)
    server.tool()(tools.watch_task)
    server.tool()(tools.command_task)
    server.tool()(tools.wait_authoring)
    server.tool()(tools.inspect_material_scene)
    server.tool()(tools.capture_material_scene)
    server.tool()(tools.inspect_material_layout)
    server.tool()(tools.preview_material_layout)
    server.tool()(tools.apply_material_layout)
    server.tool()(tools.compare_material_scene)
    server.tool()(tools.approve_material_scene_baseline)
    server.tool()(tools.validate_material_templates)
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve UniLab workflow MCP tools")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--ak", default="")
    parser.add_argument("--sk", default="")
    arguments = parser.parse_args()
    build_mcp_server(
        arguments.workspace,
        ak=arguments.ak,
        sk=arguments.sk,
    ).run()


__all__ = ["WorkflowAgentTools", "build_mcp_server", "main"]
