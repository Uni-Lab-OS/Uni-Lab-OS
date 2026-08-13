"""Resolve reproducible launch plans for Workspace Host components."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from .model import WorkspaceHostError, WorkspacePaths


@dataclass(frozen=True)
class LaunchPlan:
    component: str
    command: tuple[str, ...]
    cwd: Path
    environment: dict[str, str]
    generation: str
    log_path: Path
    address: str | None
    ready_url: str | None
    metadata: dict[str, object]


def available_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def load_environment_configuration(paths: WorkspacePaths) -> dict[str, object]:
    try:
        payload = json.loads(paths.environment.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkspaceHostError(
            "environment_invalid",
            f"本地环境配置无效：{paths.environment}",
        ) from error
    if not isinstance(payload, dict) or payload.get("schemaVersion") != 1:
        raise WorkspaceHostError(
            "environment_invalid",
            f"本地环境配置 schemaVersion 无效：{paths.environment}",
        )
    return payload


def resolve_backend_launch(
    paths: WorkspacePaths,
    *,
    graph_path: str | None = None,
    runtime_mode: str | None = None,
    backend_port: int | None = None,
    hostlink_port: int | None = None,
) -> LaunchPlan:
    config = load_environment_configuration(paths)
    selected_graph = graph_path or _optional_text(config.get("graphPath"))
    selected_graph = selected_graph or "deployment/graphs/szlab-local-debug.json"
    graph = _workspace_file(paths, selected_graph, code="graph_not_found")
    local_config = _workspace_file(
        paths,
        "deployment/local_config.py",
        code="config_not_found",
    )
    mode = runtime_mode or _optional_text(config.get("runtimeMode")) or "normal"
    if mode in {"simulation", "simulate"}:
        mode = "dry-run"
    if mode not in {"normal", "dry-run"}:
        raise WorkspaceHostError("runtime_mode_invalid", f"无效启动模式：{mode}")
    generation = str(uuid.uuid4())
    runtime_directory = paths.runtime / "backend" / generation
    runtime_directory.mkdir(parents=True, exist_ok=False)
    validated_graph = runtime_directory / "selected-graph.json"
    shutil.copyfile(graph, validated_graph)
    os.chmod(validated_graph, 0o600)
    backend_port = backend_port or available_loopback_port()
    hostlink_port = hostlink_port or available_loopback_port()
    if backend_port == hostlink_port:
        raise WorkspaceHostError("port_conflict", "Backend 与 HostLink 端口不能相同")
    environment = _runtime_environment(paths, generation)
    edge_token = _workspace_host_token(paths)
    edge_key = _workspace_edge_key(paths)
    backend_address = f"http://127.0.0.1:{backend_port}"
    environment.update(
        {
            "UNILABOS_EDGECONTROLCONFIG_API_KEY": edge_token,
            "UNILABOS_EDGECONTROLCONFIG_BACKEND_ADDR": backend_address,
            "UNILABOS_EDGECONTROLCONFIG_EDGE_KEY": edge_key,
            "UNILABOS_EDGECONTROLCONFIG_SCHEDULER_ADDR": backend_address,
            "UNILABOS_HOSTLINKCONFIG_PORT": str(hostlink_port),
            "UNILABOS_WORKBENCH_RUNTIME_MODE": mode,
            "UNILABOS_WORKBENCH_GRAPH_FINGERPRINT": _sha256(graph),
            "ROS_DOMAIN_ID": str(2 + (uuid.uuid4().int % 98)),
        }
    )
    command = (
        sys.executable,
        "-m",
        "unilabos.app.main",
        "--workspace",
        str(paths.workspace),
        "--graph",
        str(validated_graph),
        "--config",
        str(local_config),
        "--working_dir",
        str(runtime_directory),
        "--process_role",
        "workspace_backend",
        "--backend",
        "ros",
        "--app_bridges",
        "fastapi",
        "--port",
        str(backend_port),
        "--disable_browser",
        "--action_mode",
        "real" if mode == "normal" else "simulate",
        "--external_devices_only",
        "--ros_discovery_server",
        "off",
    )
    return LaunchPlan(
        component="backend",
        command=command,
        cwd=paths.workspace,
        environment=environment,
        generation=generation,
        log_path=paths.logs / f"{generation}-backend.log",
        address=backend_address,
        ready_url=f"{backend_address}/api/v1/health",
        metadata={
            "graphPath": str(graph),
            "graphFingerprint": _sha256(graph),
            "runtimeMode": mode,
            "hostLinkPort": hostlink_port,
            "runtimeDirectory": str(runtime_directory),
            "validatedGraphPath": str(validated_graph),
            "localConfigPath": str(local_config),
        },
    )


def resolve_edge_launch(
    paths: WorkspacePaths, backend: dict[str, object]
) -> LaunchPlan:
    metadata = backend.get("metadata")
    if not isinstance(metadata, dict):
        raise WorkspaceHostError("backend_not_ready", "Backend 缺少启动元数据")
    generation = str(uuid.uuid4())
    runtime_directory = paths.runtime / "edge" / generation
    runtime_directory.mkdir(parents=True, exist_ok=False)
    ready_file = runtime_directory / "ready.json"
    mode = str(metadata.get("runtimeMode") or "normal")
    backend_address = str(backend.get("address") or "").strip()
    if not backend_address:
        raise WorkspaceHostError("backend_not_ready", "Backend 缺少服务地址")
    edge_state_directory = paths.runtime / "edge"
    edge_state_directory.mkdir(parents=True, exist_ok=True)
    environment = _runtime_environment(paths, generation)
    environment.update(
        {
            "UNILABOS_EDGECONTROLCONFIG_API_KEY": _workspace_host_token(paths),
            "UNILABOS_EDGECONTROLCONFIG_BACKEND_ADDR": backend_address,
            "UNILABOS_EDGECONTROLCONFIG_EDGE_KEY": _workspace_edge_key(paths),
            "UNILABOS_EDGECONTROLCONFIG_SCHEDULER_ADDR": backend_address,
            "UNILABOS_EDGECONTROLCONFIG_STATE_DB": str(
                edge_state_directory / "edge_control.db"
            ),
            "UNILABOS_WORKBENCH_RUNTIME_MODE": mode,
            "UNILABOS_WORKBENCH_PROCESS_ROLE": "edge_runtime",
            "UNILABOS_EDGE_READY_FILE": str(ready_file),
        }
    )
    command = (
        sys.executable,
        "-m",
        "unilabos.app.main",
        "--workspace",
        str(paths.workspace),
        "--graph",
        str(metadata["validatedGraphPath"]),
        "--config",
        str(metadata["localConfigPath"]),
        "--working_dir",
        str(runtime_directory),
        "--process_role",
        "edge_runtime",
        "--control_plane",
        "local",
        "--backend",
        "ros",
        "--app_bridges",
        "edge_control",
        "--port",
        "0",
        "--disable_browser",
        "--action_mode",
        "real" if mode == "normal" else "simulate",
        "--external_devices_only",
        "--ros_discovery_server",
        "off",
    )
    return LaunchPlan(
        component="edge",
        command=command,
        cwd=paths.workspace,
        environment=environment,
        generation=generation,
        log_path=paths.logs / f"{generation}-edge.log",
        address=None,
        ready_url=None,
        metadata={
            "graphPath": metadata["graphPath"],
            "runtimeMode": mode,
            "runtimeDirectory": str(runtime_directory),
            "readyFilePath": str(ready_file),
        },
    )


def resolve_plc_launch(paths: WorkspacePaths) -> LaunchPlan:
    """Resolve PLC-Sim plus its variable-table and handshake configuration."""

    config = load_environment_configuration(paths)
    project_value = _optional_text(config.get("plcSimulatorProjectPath"))
    table_value = _optional_text(config.get("plcVariableTablePath"))
    profile = _optional_text(config.get("plcHandshakeProfile")) or "szlab"
    workflow = _optional_text(config.get("plcHandshakeWorkflow")) or "all"
    if not project_value:
        raise WorkspaceHostError("plc_configuration_missing", "未配置 PLC-Sim 项目目录")
    if not table_value:
        raise WorkspaceHostError("plc_configuration_missing", "未配置 PLC-Sim 变量表")
    if profile not in {"szlab", "xuse"}:
        raise WorkspaceHostError("plc_configuration_invalid", f"无效握手器：{profile}")
    project = Path(project_value).expanduser().resolve()
    working_directory = next(
        (
            candidate
            for candidate in (project / "OpcUaSim", project)
            if (candidate / "gui" / "backend.py").is_file()
        ),
        None,
    )
    if working_directory is None:
        raise WorkspaceHostError("plc_project_invalid", f"PLC-Sim 项目无效：{project}")
    table = Path(table_value).expanduser()
    if not table.is_absolute():
        table = paths.workspace / table
    table = table.resolve()
    if not table.is_file():
        raise WorkspaceHostError("plc_table_invalid", f"PLC-Sim 变量表不存在：{table}")
    generation = str(uuid.uuid4())
    runtime_directory = paths.runtime / "plc" / generation
    runtime_directory.mkdir(parents=True, exist_ok=False)
    # The device graph addresses the PLC simulator by its laboratory contract,
    # so unlike the Host and Backend control ports these ports are stable.
    gui_port = _configured_port(config.get("plcSimulatorGuiPort"), 18_765)
    opcua_port = _configured_port(config.get("plcSimulatorOpcUaPort"), 4_855)
    return LaunchPlan(
        component="plc",
        command=(
            sys.executable,
            "-m",
            "gui.backend",
            "--host",
            "127.0.0.1",
            "--port",
            str(gui_port),
        ),
        cwd=working_directory,
        environment=_runtime_environment(paths, generation),
        generation=generation,
        log_path=paths.logs / f"{generation}-plc.log",
        address=f"http://127.0.0.1:{gui_port}",
        ready_url=f"http://127.0.0.1:{gui_port}/api/state",
        metadata={
            "projectPath": str(project),
            "variableTablePath": str(table),
            "handshakeProfile": profile,
            "handshakeWorkflow": workflow,
            "guiUrl": f"http://127.0.0.1:{gui_port}",
            "opcUaUrl": f"opc.tcp://127.0.0.1:{opcua_port}",
            "opcUaPort": opcua_port,
            "runtimeDirectory": str(runtime_directory),
        },
    )


def _runtime_environment(paths: WorkspacePaths, generation: str) -> dict[str, str]:
    environment = dict(os.environ)
    checkout = Path(__file__).resolve().parents[2]
    imports = [str(checkout), str(paths.workspace)]
    inherited = environment.get("PYTHONPATH")
    if inherited:
        imports.append(inherited)
    environment.update(
        {
            "PYTHONPATH": os.pathsep.join(imports),
            "PYTHONUNBUFFERED": "1",
            "UNILABOS_OBSERVABILITYCONFIG_ENABLED": "true",
            "UNILABOS_OBSERVABILITYCONFIG_PROJECT_NAME": "uni-lab-workbench",
            "UNILABOS_WORKBENCH_GENERATION": generation,
            "UNILABOS_WORKBENCH_WORKSPACE": str(paths.workspace),
        }
    )
    return environment


def _workspace_file(paths: WorkspacePaths, value: str, *, code: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = paths.workspace / candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(paths.workspace)
    except ValueError as error:
        raise WorkspaceHostError(code, f"路径越出 Workspace：{candidate}") from error
    if not candidate.is_file():
        raise WorkspaceHostError(code, f"文件不存在：{candidate}")
    return candidate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _workspace_host_token(paths: WorkspacePaths) -> str:
    try:
        token = paths.token.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise WorkspaceHostError(
            "host_token_invalid", "Workspace Host token 不可读"
        ) from error
    if not token:
        raise WorkspaceHostError(
            "host_token_invalid", "Workspace Host token 为空"
        )
    return token


def _workspace_edge_key(paths: WorkspacePaths) -> str:
    return "managed-local-" + hashlib.sha256(
        str(paths.workspace).encode("utf-8")
    ).hexdigest()[:24]


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _configured_port(value: object, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 65_535:
        raise WorkspaceHostError(
            "plc_configuration_invalid", f"无效 PLC-Sim 端口：{value}"
        )
    return value
