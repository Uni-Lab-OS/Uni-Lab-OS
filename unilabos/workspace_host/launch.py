"""Resolve reproducible launch plans for Workspace Host components."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from unilabos.app.edge_control.addressing import resolve_scheduler_address

from .model import WorkspaceHostError, WorkspacePaths, atomic_write_json


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
    """解析 Workspace Backend 的可复现启动计划。

    Args:
        paths: 当前工作区的标准路径集合。
        graph_path: 可选的显式工作流图路径。
        runtime_mode: 可选的显式 OS 运行模式。
        backend_port: 可选的 Backend 监听端口。
        hostlink_port: 可选的 HostLink 监听端口。

    Returns:
        包含命令、环境、身份代次与配置元数据的 Backend 启动计划。

    Raises:
        WorkspaceHostError: 工作区文件、端口、模式或设备包范围配置无效。
    """

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
    domain_mode = _optional_text(config.get("domainMode")) or "local"
    if domain_mode not in {"local", "backend"}:
        raise WorkspaceHostError(
            "domain_mode_invalid", f"无效 Domain Authority：{domain_mode}"
        )
    external_devices_only = config.get("externalDevicesOnly", True)
    if not isinstance(external_devices_only, bool):
        raise WorkspaceHostError(
            "environment_invalid", "externalDevicesOnly 必须是布尔值"
        )
    generation = str(uuid.uuid4())
    runtime_directory = paths.runtime / "backend" / generation
    runtime_directory.mkdir(parents=True, exist_ok=False)
    state_directory = paths.runtime / "backend" / (
        "local-domain" if domain_mode == "local" else "authoring"
    )
    state_directory.mkdir(parents=True, exist_ok=True)
    legacy_state = (
        _migrate_legacy_backend_state(paths, state_directory)
        if domain_mode == "local"
        else None
    )
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
        str(state_directory),
        *(
            ("--preserve_runtime_databases",)
            if domain_mode == "local"
            else ()
        ),
        "--process_role",
        "workspace_backend",
        "--control_plane",
        domain_mode,
        "--backend",
        "ros",
        "--app_bridges",
        "fastapi",
        "--port",
        str(backend_port),
        "--disable_browser",
        "--action_mode",
        "real" if mode == "normal" else "simulate",
        *(("--external_devices_only",) if external_devices_only else ()),
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
        ready_url=f"{backend_address}/api/v1/readiness",
        metadata={
            "graphPath": str(graph),
            "graphFingerprint": _sha256(graph),
            "runtimeMode": mode,
            "externalDevicesOnly": external_devices_only,
            "domainMode": domain_mode,
            "backendUrl": _optional_text(config.get("backendUrl")),
            "schedulerUrl": _optional_text(config.get("schedulerUrl")),
            "hostLinkPort": hostlink_port,
            "runtimeDirectory": str(runtime_directory),
            "stateDirectory": str(state_directory),
            **(
                {"legacyStateMigratedFrom": legacy_state}
                if legacy_state
                else {}
            ),
            "validatedGraphPath": str(validated_graph),
            "localConfigPath": str(local_config),
        },
    )


def resolve_edge_launch(
    paths: WorkspacePaths, backend: dict[str, object]
) -> LaunchPlan:
    """从 Backend 元数据解析共享配置的 Edge 启动计划。

    Args:
        paths: 当前工作区的标准路径集合。
        backend: 已就绪 Backend 的地址与启动元数据投影。

    Returns:
        与 Backend 使用相同设备包范围和领域权威的 Edge 启动计划。

    Raises:
        WorkspaceHostError: Backend 地址、元数据、领域权威或设备包范围无效。
    """

    metadata = backend.get("metadata")
    if not isinstance(metadata, dict):
        raise WorkspaceHostError("backend_not_ready", "Backend 缺少启动元数据")
    generation = str(uuid.uuid4())
    runtime_directory = paths.runtime / "edge" / generation
    runtime_directory.mkdir(parents=True, exist_ok=False)
    ready_file = runtime_directory / "ready.json"
    mode = str(metadata.get("runtimeMode") or "normal")
    external_devices_only = metadata.get("externalDevicesOnly", True)
    if not isinstance(external_devices_only, bool):
        raise WorkspaceHostError(
            "backend_not_ready", "Backend 外部设备包范围元数据无效"
        )
    local_backend_address = str(backend.get("address") or "").strip()
    if not local_backend_address:
        raise WorkspaceHostError("backend_not_ready", "Backend 缺少服务地址")
    domain_mode = str(metadata.get("domainMode") or "local")
    if domain_mode not in {"local", "backend"}:
        raise WorkspaceHostError(
            "domain_mode_invalid", f"无效 Domain Authority：{domain_mode}"
        )
    authority_address = (
        str(metadata.get("backendUrl") or "").rstrip("/")
        if domain_mode == "backend"
        else local_backend_address
    )
    if not authority_address:
        raise WorkspaceHostError(
            "backend_url_missing", "Backend Authority 未配置服务地址"
        )
    try:
        scheduler_address = (
            resolve_scheduler_address(
                authority_address,
                _optional_text(metadata.get("schedulerUrl")),
            )
            if domain_mode == "backend"
            else authority_address
        )
    except ValueError as error:
        raise WorkspaceHostError("scheduler_url_invalid", str(error)) from error
    authority_token = (
        os.environ.get("UNILAB_BACKEND_API_KEY") or _workspace_host_token(paths)
        if domain_mode == "backend"
        else _workspace_host_token(paths)
    )
    edge_state_directory = paths.runtime / "edge"
    edge_state_directory.mkdir(parents=True, exist_ok=True)
    # Command sequence numbers and pending outcomes are scoped to the
    # scheduler authority that issued them.  Reusing Local Authority state
    # against Backend Authority makes a legitimate local acknowledgement look
    # like an impossible future acknowledgement to Backend.  Keep the existing
    # local filename for upgrade/crash recovery and isolate every remote
    # authority by a stable origin digest.
    state_db = edge_state_directory / "edge_control.db"
    if domain_mode == "backend":
        scheduler_override = _optional_text(metadata.get("schedulerUrl"))
        state_scope = (
            f"{authority_address}\0{scheduler_address}"
            if scheduler_override
            else authority_address
        )
        authority_digest = hashlib.sha256(state_scope.encode("utf-8")).hexdigest()[:16]
        state_db = edge_state_directory / f"edge_control-backend-{authority_digest}.db"
    environment = _runtime_environment(paths, generation)
    environment.update(
        {
            "UNILABOS_EDGECONTROLCONFIG_API_KEY": authority_token,
            "UNILABOS_EDGECONTROLCONFIG_BACKEND_ADDR": authority_address,
            "UNILABOS_EDGECONTROLCONFIG_EDGE_KEY": _workspace_edge_key(paths),
            "UNILABOS_EDGECONTROLCONFIG_SCHEDULER_ADDR": scheduler_address,
            "UNILABOS_EDGECONTROLCONFIG_STATE_DB": str(state_db),
            "UNILABOS_WORKBENCH_RUNTIME_MODE": mode,
            "UNILABOS_WORKBENCH_PROCESS_ROLE": "edge_runtime",
            "UNILABOS_EDGE_READY_FILE": str(ready_file),
            # Isolate DDS discovery per Workspace.  Leaving Edge on domain 0
            # makes HostNode discover devices from unrelated laboratories
            # running on the same machine and then attempt to register their
            # barcodes with this Backend.
            "ROS_DOMAIN_ID": str(
                2
                + int.from_bytes(
                    hashlib.sha256(str(paths.workspace).encode("utf-8")).digest()[:4],
                    "big",
                )
                % 98
            ),
        }
    )
    edge_graph = Path(str(metadata["validatedGraphPath"]))
    if mode == "dry-run":
        edge_graph = runtime_directory / "selected-graph.json"
        _write_dry_run_edge_graph(
            Path(str(metadata["validatedGraphPath"])), edge_graph
        )
    command = (
        sys.executable,
        "-m",
        "unilabos.app.main",
        "--workspace",
        str(paths.workspace),
        "--graph",
        str(edge_graph),
        "--config",
        str(metadata["localConfigPath"]),
        "--working_dir",
        str(runtime_directory),
        "--process_role",
        "edge_runtime",
        "--control_plane",
        domain_mode,
        "--backend",
        "ros",
        "--app_bridges",
        "edge_control",
        "--port",
        "0",
        "--disable_browser",
        "--action_mode",
        "real" if mode == "normal" else "simulate",
        *(("--external_devices_only",) if external_devices_only else ()),
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
            "externalDevicesOnly": external_devices_only,
            "domainMode": domain_mode,
            "authorityAddress": authority_address,
            "schedulerAddress": scheduler_address,
            "protocolStatePath": str(state_db),
            "runtimeDirectory": str(runtime_directory),
            "readyFilePath": str(ready_file),
        },
    )


def _write_dry_run_edge_graph(source: Path, target: Path) -> None:
    """Detach dry-run Edge drivers from real endpoints without changing authority data."""

    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkspaceHostError(
            "graph_invalid", "无法生成 Dry-run Edge 设备图"
        ) from error
    nodes = payload.get("nodes") if isinstance(payload, dict) else None
    if isinstance(nodes, list):
        for node in nodes:
            if not isinstance(node, dict):
                continue
            config = node.get("config")
            if isinstance(config, dict) and "auto_connect" in config:
                config["auto_connect"] = False
    atomic_write_json(target, payload)
    os.chmod(target, 0o600)


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
    environment = _with_conda_ros_environment(dict(os.environ))
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


def _with_conda_ros_environment(
    environment: dict[str, str],
    *,
    platform: str = sys.platform,
    prefix: Path | None = None,
    executable: str | None = None,
) -> dict[str, str]:
    """Restore RoboStack activation variables for detached Windows children.

    Workbench starts its Workspace Host with the selected environment's Python
    executable, but a GUI process has no activated Conda shell.  RoboStack's
    ``rclpy`` then imports successfully while RMW initialization fails because
    ``AMENT_PREFIX_PATH`` was never populated.  Reconstruct the stable subset
    of the environment's activation contract directly from that interpreter.
    Explicit caller overrides remain authoritative.
    """

    if platform != "win32":
        return environment
    conda_prefix = (prefix or Path(sys.prefix)).resolve()
    library = conda_prefix / "Library"
    if not (conda_prefix / "conda-meta").is_dir():
        return environment
    if not (library / "local_setup.bat").is_file():
        return environment

    python_executable = executable or sys.executable
    environment.setdefault("CONDA_PREFIX", str(conda_prefix))
    environment.setdefault("CONDA_DEFAULT_ENV", conda_prefix.name)
    environment.setdefault("AMENT_PREFIX_PATH", str(library))
    environment.setdefault("AMENT_PYTHON_EXECUTABLE", python_executable)
    environment.setdefault("QT_PLUGIN_PATH", str(library / "plugins"))
    environment.setdefault("ROS_DISTRO", "humble")
    environment.setdefault("ROS_ETC_DIR", str(library / "etc" / "ros"))
    environment.setdefault("ROS_LOCALHOST_ONLY", "0")
    environment.setdefault("ROS_OS_OVERRIDE", "conda:win64")
    environment.setdefault("ROS_PYTHON_VERSION", str(sys.version_info.major))
    environment.setdefault("ROS_VERSION", "2")
    environment["PYTHONHOME"] = ""

    existing_path = environment.get("PATH", "")
    path_entries = [
        library / "bin",
        conda_prefix,
        library / "mingw-w64" / "bin",
        library / "usr" / "bin",
        conda_prefix / "Scripts",
        conda_prefix / "bin",
    ]
    merged = [str(entry) for entry in path_entries]
    merged.extend(part for part in existing_path.split(os.pathsep) if part)
    seen: set[str] = set()
    preserved: list[str] = []
    for part in merged:
        key = part.casefold()
        if key in seen:
            continue
        seen.add(key)
        preserved.append(part)
    environment["PATH"] = os.pathsep.join(preserved)
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


def _migrate_legacy_backend_state(
    paths: WorkspacePaths,
    state_directory: Path,
) -> str | None:
    """Move closed generation-local SQLite facts into the stable Local Domain."""

    try:
        session = json.loads(paths.session.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    components = session.get("components") if isinstance(session, dict) else None
    backend = components.get("backend") if isinstance(components, dict) else None
    metadata = backend.get("metadata") if isinstance(backend, dict) else None
    value = metadata.get("runtimeDirectory") if isinstance(metadata, dict) else None
    if not isinstance(value, str) or not value:
        return None
    legacy_directory = Path(value).expanduser().resolve()
    backend_root = (paths.runtime / "backend").resolve()
    try:
        legacy_directory.relative_to(backend_root)
    except ValueError:
        return None
    if legacy_directory == state_directory.resolve():
        return None
    migrated = False
    for database_name in (
        "inventory.db",
        "device_state.db",
        "workflow_history.db",
        "edge_authority.db",
    ):
        source = legacy_directory / database_name
        destination = state_directory / database_name
        if destination.exists() or not source.is_file():
            continue
        try:
            source_connection = sqlite3.connect(source)
            destination_connection = sqlite3.connect(destination)
            try:
                source_connection.backup(destination_connection)
            finally:
                destination_connection.close()
                source_connection.close()
            os.chmod(destination, 0o600)
        except (OSError, sqlite3.Error) as error:
            raise WorkspaceHostError(
                "backend_state_migration_failed",
                f"迁移旧 Local Domain 数据失败：{source}：{error}",
            ) from error
        migrated = True
    return str(legacy_directory) if migrated else None


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
