"""Workflow Agent tools sharing the exact Domain client used by the CLI.

This module deliberately contains no process control, database reads, or Node
RPC.  MCP is an optional transport around :class:`DomainBackendClient`.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Mapping, Sequence

from unilabos.client.domain import DomainBackendClient


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
