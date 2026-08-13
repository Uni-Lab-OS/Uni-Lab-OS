"""`unilab workspace` adapter over the Workspace Host client SDK."""

from __future__ import annotations

import json
import os
from typing import Any

from .client import WorkspaceHostClient, ensure_workspace_host
from .model import COMPONENT_NAMES, WorkspaceHostError


def register_workspace_subcommands(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "workspace",
        help="Control the per-workspace Local Backend, OS, PLC-Sim, and renderer",
    )
    actions = parser.add_subparsers(dest="workspace_action", required=True)
    for action in ("status", "start", "stop", "restart", "reset-local"):
        leaf = actions.add_parser(action)
        leaf.add_argument("--workspace", dest="workspace_cli_path", default=None)
        leaf.add_argument("--json", action="store_true", dest="workspace_json")
        if action not in {"status", "reset-local"}:
            leaf.add_argument(
                "--component",
                choices=["backend", "os", "plc"],
                default="backend" if action == "start" else "os",
            )
        if action != "status":
            leaf.add_argument("--operation-id", default=None)
            leaf.add_argument("--wait", type=float, default=120.0)
        if action in {"start", "restart", "reset-local"}:
            leaf.add_argument("--graph", default=None)
            leaf.add_argument(
                "--runtime-mode", choices=["normal", "dry-run"], default=None
            )
        if action == "reset-local":
            leaf.add_argument(
                "--yes",
                action="store_true",
                help="Confirm deletion of rebuildable Local Domain and Edge state",
            )
    logs = actions.add_parser("logs")
    logs.add_argument("--workspace", dest="workspace_cli_path", default=None)
    logs.add_argument("--component", choices=COMPONENT_NAMES, default="backend")
    logs.add_argument("--max-bytes", type=int, default=64 * 1024)
    logs.add_argument("--json", action="store_true", dest="workspace_json")
    operation = actions.add_parser("operation")
    operation.add_argument("operation_id")
    operation.add_argument("--workspace", dest="workspace_cli_path", default=None)
    operation.add_argument("--json", action="store_true", dest="workspace_json")
    authority = actions.add_parser("authority")
    authority.add_argument("mode", choices=["local", "backend"])
    authority.add_argument("--backend-url", default=None)
    authority.add_argument("--workspace", dest="workspace_cli_path", default=None)
    authority.add_argument("--operation-id", default=None)
    authority.add_argument("--wait", type=float, default=120.0)
    authority.add_argument("--json", action="store_true", dest="workspace_json")


def dispatch_workspace_command(args: dict[str, Any]) -> bool:
    if args.get("command") != "workspace":
        return False
    workspace = args.get("workspace_cli_path") or args.get("workspace") or os.getcwd()
    action = str(args.get("workspace_action") or "")
    output_json = bool(args.get("workspace_json"))
    try:
        if action == "status":
            result = WorkspaceHostClient.status(workspace)
        elif action == "logs":
            client = WorkspaceHostClient.discover(workspace)
            result = client.logs(
                str(args["component"]), max_bytes=int(args["max_bytes"])
            )
        elif action == "operation":
            client = WorkspaceHostClient.discover(workspace)
            result = client.operation(str(args["operation_id"]))
        elif action == "authority":
            client = ensure_workspace_host(workspace)
            result = client.execute(
                "authority.switch",
                parameters={
                    "mode": args.get("mode"),
                    "backendUrl": args.get("backend_url"),
                },
                operation_id=args.get("operation_id"),
                timeout=float(args.get("wait") or 120.0),
            )
        elif action == "reset-local":
            if not args.get("yes"):
                raise WorkspaceHostError(
                    "confirmation_required",
                    "重建本地数据会删除可重建的 Local Domain 与 Edge 状态；请显式传入 --yes",
                )
            client = ensure_workspace_host(workspace)
            parameters = {
                key: value
                for key, value in {
                    "graphPath": args.get("graph"),
                    "runtimeMode": args.get("runtime_mode"),
                }.items()
                if value is not None
            }
            result = client.execute(
                "local.reset-state",
                parameters=parameters,
                operation_id=args.get("operation_id"),
                timeout=float(args.get("wait") or 120.0),
            )
        else:
            client = ensure_workspace_host(workspace)
            parameters = {
                key: value
                for key, value in {
                    "graphPath": args.get("graph"),
                    "runtimeMode": args.get("runtime_mode"),
                }.items()
                if value is not None
            }
            command = _command(action, str(args.get("component")))
            result = client.execute(
                command,
                parameters=parameters,
                operation_id=args.get("operation_id"),
                timeout=float(args.get("wait") or 120.0),
            )
    except WorkspaceHostError as error:
        _print({"ok": False, "error": error.as_dict()}, output_json=True)
        raise SystemExit(1) from error
    _print(result, output_json=output_json)
    return True


def _command(action: str, component: str) -> str:
    names = {"backend": "backend", "os": "os", "plc": "plc"}
    if action not in {"start", "stop", "restart"} or component not in names:
        raise WorkspaceHostError("invalid_request", "无效 workspace 操作")
    return f"{names[component]}.{action}"


def _print(payload: object, *, output_json: bool) -> None:
    if output_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if isinstance(payload, dict) and "content" in payload:
        print(str(payload.get("content") or ""), end="")
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
