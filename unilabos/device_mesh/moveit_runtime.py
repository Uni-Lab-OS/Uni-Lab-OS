"""MoveIt runtime independent from RViz/web presentation.

The runtime consumes audited package model assets, owns its generated files and
LaunchService lifecycle, and only adds RViz when an explicit view is requested.
ROS/MoveIt imports are intentionally lazy so PLC-only deployments do not need
those packages at import time.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET

import yaml

from unilabos.device_mesh.motion_runtime_plan import node_requests_moveit


@dataclass(frozen=True)
class MoveItModelBundle:
    device_id: str
    models_root: Path
    manifest: Mapping[str, Any]
    manifest_path: Path
    node: Mapping[str, Any]


@dataclass(frozen=True)
class StaticModelBundle:
    """A non-MoveIt station node contributing fixed geometry only.

    The device or resource declares ``model.format == "xacro"``; the runtime
    includes its macro under ``world`` so RViz renders the station from the same
    package assets the frontend projects. No ros2_control or SRDF is derived
    from it. ``node`` carries a world-space position, since every static macro
    is anchored to ``world`` regardless of graph nesting.
    """

    device_id: str
    models_root: Path
    entry_path: Path
    macro: str
    node: Mapping[str, Any]


class PackageMoveItModelResolver:
    """RobotModelResolver Adapter backed by PackageSource + PackageCatalog."""

    def __init__(
        self,
        package_sources: Sequence[Any],
        package_catalogs: Sequence[Any],
        runtime_root: Path,
    ) -> None:
        if len(package_sources) != len(package_catalogs):
            raise ValueError("PackageSource 与 PackageCatalog 数量不一致")
        self._observations = tuple(zip(package_sources, package_catalogs))
        self._runtime_root = runtime_root

    def resolve(
        self,
        *,
        device_fqid: str,
        device_id: str,
        node: Mapping[str, Any],
    ) -> MoveItModelBundle:
        matches: list[tuple[Any, Any, Any]] = []
        for source, catalog in self._observations:
            for record in catalog.definitions.devices:
                if record.fqid == device_fqid:
                    matches.append((source, catalog, record))
        if len(matches) != 1:
            raise ValueError(
                f"MoveIt device {device_fqid} 必须恰好来自一个已授权 Package observation，"
                f"实际 {len(matches)}"
            )
        source, catalog, record = matches[0]
        model = record.details.get("model")
        if not isinstance(model, Mapping):
            raise ValueError(f"MoveIt device {device_fqid} 没有 model metadata")
        moveit = model.get("moveit")
        if not isinstance(moveit, Mapping) or moveit.get("format") != "unilab.moveit/v1":
            raise ValueError(
                f"MoveIt device {device_fqid} 缺少 model.moveit unilab.moveit/v1 manifest"
            )
        logical_manifest = _safe_logical_path(str(moveit.get("entry") or ""))
        parts = logical_manifest.parts
        if "models" not in parts:
            raise ValueError("MoveIt manifest 必须位于 models/ 资产闭包")
        models_index = max(index for index, part in enumerate(parts) if part == "models")
        models_prefix = PurePosixPath(*parts[: models_index + 1])

        from unilabos.package_manager import PackageAssetResolver

        resolver = PackageAssetResolver(source, catalog)
        destination = self._runtime_root / _safe_segment(device_id)
        destination.mkdir(parents=True, exist_ok=False)
        for asset in catalog.assets:
            logical = _safe_logical_path(asset.logical_path)
            if not logical.is_relative_to(models_prefix):
                continue
            relative = logical.relative_to(models_prefix)
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with resolver.open_binary(asset.logical_path) as stream:
                target.write_bytes(stream.read())

        relative_manifest = logical_manifest.relative_to(models_prefix)
        manifest_path = destination.joinpath(*relative_manifest.parts)
        if not manifest_path.is_file():
            raise ValueError(f"MoveIt manifest 未物化: {logical_manifest}")
        raw_manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(raw_manifest, Mapping):
            raise ValueError("MoveIt manifest 必须是对象")
        if raw_manifest.get("schema") != "unilab.moveit/v1":
            raise ValueError("MoveIt manifest schema 必须是 unilab.moveit/v1")
        _validate_manifest_files(raw_manifest, manifest_path, destination)
        return MoveItModelBundle(
            device_id=device_id,
            models_root=destination,
            manifest=dict(raw_manifest),
            manifest_path=manifest_path,
            node=node,
        )

    def resolve_static(
        self,
        *,
        device_fqid: str,
        device_id: str,
        node: Mapping[str, Any],
    ) -> StaticModelBundle | None:
        """Materialize a xacro-only station node, or ``None`` when absent."""

        match = self._single_record(device_fqid)
        if match is None:
            return None
        source, catalog, record = match
        model = record.details.get("model")
        if not isinstance(model, Mapping):
            return None
        if str(model.get("format") or "") != "xacro":
            return None
        macro = str(model.get("macro") or "")
        entry = str(model.get("entry") or "")
        if not macro or not entry:
            return None

        destination, entry_path = self._materialize_models(
            source,
            catalog,
            device_id=device_id,
            logical_entry=entry,
        )
        return StaticModelBundle(
            device_id=device_id,
            models_root=destination,
            entry_path=entry_path,
            macro=macro,
            node=node,
        )

    def _single_record(self, device_fqid: str) -> tuple[Any, Any, Any] | None:
        """Look a graph class up as a device first, then as a resource.

        Station scenery may be declared either way: a rack that only carries
        geometry is a resource, while a machine is a device.
        """

        matches = [
            (source, catalog, record)
            for source, catalog in self._observations
            for collection in (catalog.definitions.devices, catalog.definitions.resources)
            for record in collection
            if record.fqid == device_fqid
        ]
        if len(matches) != 1:
            return None
        return matches[0]

    def _materialize_models(
        self,
        source: Any,
        catalog: Any,
        *,
        device_id: str,
        logical_entry: str,
    ) -> tuple[Path, Path]:
        logical = _safe_logical_path(logical_entry)
        parts = logical.parts
        if "models" not in parts:
            raise ValueError(f"模型 entry 必须位于 models/ 资产闭包: {logical_entry}")
        models_index = max(index for index, part in enumerate(parts) if part == "models")
        models_prefix = PurePosixPath(*parts[: models_index + 1])

        from unilabos.package_manager import PackageAssetResolver

        resolver = PackageAssetResolver(source, catalog)
        destination = self._runtime_root / _safe_segment(device_id)
        destination.mkdir(parents=True, exist_ok=True)
        for asset in catalog.assets:
            asset_logical = _safe_logical_path(asset.logical_path)
            if not asset_logical.is_relative_to(models_prefix):
                continue
            relative = asset_logical.relative_to(models_prefix)
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with resolver.open_binary(asset.logical_path) as stream:
                target.write_bytes(stream.read())

        entry_path = destination.joinpath(*logical.relative_to(models_prefix).parts)
        if not entry_path.is_file():
            raise ValueError(f"模型 entry 未物化: {logical_entry}")
        return destination, entry_path


class MoveItRuntime:
    """Deep Module owning model assembly, launch children and temporary files."""

    def __init__(
        self,
        devices: Mapping[str, Mapping[str, Any]],
        *,
        moveit_device_ids: Sequence[str],
        package_sources: Sequence[Any],
        package_catalogs: Sequence[Any],
        enable_rviz: bool = False,
        runtime_parent: str | Path | None = None,
    ) -> None:
        self._devices = devices
        self._moveit_device_ids = tuple(moveit_device_ids)
        self._package_sources = tuple(package_sources)
        self._package_catalogs = tuple(package_catalogs)
        self._enable_rviz = bool(enable_rviz)
        self._runtime_parent = Path(runtime_parent) if runtime_parent else None
        self._runtime_root: Path | None = None
        self._process: Any | None = None
        self._lock = threading.RLock()
        self._startup_error: BaseException | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._moveit_device_ids)

    @property
    def enable_rviz(self) -> bool:
        return self._enable_rviz

    @property
    def healthy(self) -> bool:
        process = self._process
        return (
            self.enabled
            and self._startup_error is None
            and process is not None
            and process.is_alive()
        )

    @property
    def startup_error(self) -> BaseException | None:
        return self._startup_error

    def start(self, timeout: float = 30.0) -> None:
        """Start the ROS launch tree once; explicit MoveIt failure is fatal."""

        with self._lock:
            if not self.enabled or (self._process is not None and self._process.is_alive()):
                return
            if "AMENT_PREFIX_PATH" not in os.environ:
                raise OSError(
                    "显式 MoveIt backend 需要 ROS 2 环境；AMENT_PREFIX_PATH 未设置"
                )
            parent = str(self._runtime_parent) if self._runtime_parent else None
            self._runtime_root = Path(
                tempfile.mkdtemp(prefix="unilab-moveit-", dir=parent)
            )
            try:
                self._materialize_launch_bundle(self._runtime_root)
            except BaseException:
                shutil.rmtree(self._runtime_root, ignore_errors=True)
                self._runtime_root = None
                raise
            self._startup_error = None
            ready_path = self._runtime_root / ".launch_ready"
            error_path = self._runtime_root / ".launch_error"
            for marker in (ready_path, error_path):
                marker.unlink(missing_ok=True)
            # LaunchService insists on the process main thread (signal wakeup
            # fd). Keep FastAPI on this process and own MoveIt in a child whose
            # main thread can run the launch loop.
            import multiprocessing as mp

            context = mp.get_context("spawn")
            self._process = context.Process(
                target=_launch_service_process_main,
                args=(str(self._runtime_root),),
                name="moveit_runtime",
                daemon=True,
            )
            self._process.start()

        deadline = time.monotonic() + max(0.1, float(timeout))
        while time.monotonic() < deadline:
            process = self._process
            if process is None:
                break
            if error_path.is_file():
                message = error_path.read_text(encoding="utf-8").strip() or "unknown"
                self._startup_error = RuntimeError(message)
                break
            if ready_path.is_file():
                # Catch immediate child death after ready (e.g. node spawn fail).
                process.join(timeout=0.25)
                if process.is_alive():
                    return
                message = (
                    error_path.read_text(encoding="utf-8").strip()
                    if error_path.is_file()
                    else f"exit_code={process.exitcode}"
                )
                self._startup_error = RuntimeError(
                    f"MoveIt LaunchService 在启动阶段提前退出: {message}"
                )
                break
            if not process.is_alive():
                message = (
                    error_path.read_text(encoding="utf-8").strip()
                    if error_path.is_file()
                    else f"exit_code={process.exitcode}"
                )
                self._startup_error = RuntimeError(
                    f"MoveIt LaunchService 启动失败: {message}"
                )
                break
            time.sleep(0.05)

        if self._startup_error is None and (
            self._process is None or not self._process.is_alive() or not ready_path.is_file()
        ):
            self._startup_error = TimeoutError("MoveIt LaunchService 初始化超时")
        if self._startup_error is not None:
            error = self._startup_error
            self.stop(timeout=timeout)
            raise RuntimeError(f"MoveIt LaunchService 启动失败: {error}") from error

    def stop(self, timeout: float = 10.0) -> None:
        with self._lock:
            process = self._process
            runtime_root = self._runtime_root
        if process is not None and process.is_alive():
            process.terminate()
            process.join(timeout=max(0.0, float(timeout)))
        if process is not None and process.is_alive():
            process.kill()
            process.join(timeout=max(0.0, float(timeout)))
        if process is not None and process.is_alive():
            raise TimeoutError(
                "MoveIt LaunchService 未在期限内退出；为避免子进程读取失效文件，保留 runtime 目录"
            )
        with self._lock:
            self._process = None
            self._runtime_root = None
        if runtime_root is not None:
            shutil.rmtree(runtime_root, ignore_errors=True)

    def _materialize_launch_bundle(self, runtime_root: Path) -> None:
        resolver = PackageMoveItModelResolver(
            self._package_sources,
            self._package_catalogs,
            runtime_root,
        )
        bundles = []
        nodes_by_id = {
            str(node.get("id") or key): node for key, node in self._devices.items()
        }
        for device_id in self._moveit_device_ids:
            node = nodes_by_id.get(device_id)
            if node is None:
                raise ValueError(f"MoveIt graph device 不存在: {device_id}")
            bundles.append(
                resolver.resolve(
                    device_fqid=str(node.get("class") or ""),
                    device_id=device_id,
                    node=node,
                )
            )
        for bundle in bundles:
            _validate_sim_runtime_profile(bundle)

        static_bundles = self._resolve_static_bundles(resolver, nodes_by_id)
        urdf, srdf = _assemble_robot_descriptions(bundles, static_bundles)
        _validate_mock_hardware_urdf(urdf)
        ros2_controllers, moveit_controllers, kinematics, joint_limits = (
            _assemble_moveit_configs(bundles)
        )
        _validate_move_group_specs(bundles, kinematics, joint_limits)
        controller_path = runtime_root / "ros2_controllers.yaml"
        staged = runtime_root / "ros2_controllers.yaml.tmp"
        staged.write_text(
            yaml.safe_dump(ros2_controllers, sort_keys=False),
            encoding="utf-8",
        )
        staged.replace(controller_path)

        rviz_config = ""
        if self._enable_rviz:
            planning_group = next(iter(kinematics), "")
            rviz_config = str(_write_rviz_config(runtime_root, planning_group).name)

        payload = {
            "enable_rviz": self._enable_rviz,
            "urdf": urdf,
            "srdf": srdf,
            "controller_path": controller_path.name,
            "moveit_controllers": moveit_controllers,
            "kinematics": kinematics,
            "joint_limits": joint_limits,
            "rviz_config": rviz_config,
        }
        bundle_path = runtime_root / "launch_bundle.yaml"
        bundle_tmp = runtime_root / "launch_bundle.yaml.tmp"
        bundle_tmp.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        bundle_tmp.replace(bundle_path)

    def _resolve_static_bundles(
        self,
        resolver: PackageMoveItModelResolver,
        nodes_by_id: Mapping[str, Mapping[str, Any]],
    ) -> tuple[StaticModelBundle, ...]:
        """Collect xacro-only station geometry so RViz shows the whole station.

        Devices and resources are both eligible — a rack that only carries
        geometry is modelled as a resource. A node that fails to resolve is
        skipped rather than fatal: station scenery must never block the motion
        runtime.
        """

        moveit_ids = set(self._moveit_device_ids)
        parent_of = _parent_index(nodes_by_id)
        static: list[StaticModelBundle] = []
        for device_id, node in sorted(nodes_by_id.items()):
            if device_id in moveit_ids:
                continue
            if node_requests_moveit(node):
                continue
            placed = _flatten_node_to_world(node, nodes_by_id, parent_of)
            if placed is None:
                continue
            try:
                bundle = resolver.resolve_static(
                    device_fqid=str(node.get("class") or ""),
                    device_id=device_id,
                    node=placed,
                )
            except Exception:
                continue
            if bundle is not None:
                static.append(bundle)
        return tuple(static)

    def _create_launch_description(self, runtime_root: Path):
        """Compatibility helper used by tests; materialize then build."""

        self._materialize_launch_bundle(runtime_root)
        return _launch_description_from_bundle(runtime_root)


def _assemble_robot_descriptions(
    bundles: Sequence[MoveItModelBundle],
    static_bundles: Sequence[StaticModelBundle] = (),
) -> tuple[str, str]:
    import xacro

    xacro_namespace = "http://www.ros.org/wiki/xacro"
    ET.register_namespace("xacro", xacro_namespace)
    robot = ET.Element("robot", {"name": "unilab_motion_runtime"})
    ET.SubElement(robot, "link", {"name": "world"})
    semantic = ET.Element("robot", {"name": "unilab_motion_runtime"})

    for bundle in bundles:
        manifest = bundle.manifest
        robot_spec = _mapping(manifest, "robot")
        control_spec = _mapping(manifest, "ros2_control")
        semantic_spec = _mapping(manifest, "semantic")
        robot_path = _manifest_file(bundle, robot_spec, "entry")
        control_path = _manifest_file(bundle, control_spec, "entry")
        semantic_path = _manifest_file(bundle, semantic_spec, "entry")

        ET.SubElement(
            robot,
            f"{{{xacro_namespace}}}include",
            {"filename": str(robot_path)},
        )
        ET.SubElement(
            robot,
            f"{{{xacro_namespace}}}include",
            {"filename": str(control_path)},
        )
        model_args = {
            "parent_link": "world",
            "station_name": "",
            "device_name": f"{bundle.device_id}_",
            "mesh_path": str(bundle.models_root),
            **_node_pose_arguments(bundle.node),
        }
        declared_args = robot_spec.get("arguments")
        if isinstance(declared_args, Mapping):
            model_args.update({str(key): str(value) for key, value in declared_args.items()})
        ET.SubElement(
            robot,
            f"{{{xacro_namespace}}}{str(robot_spec['macro'])}",
            model_args,
        )
        ET.SubElement(
            robot,
            f"{{{xacro_namespace}}}{str(control_spec['macro'])}",
            {
                "device_name": f"{bundle.device_id}_",
                "mesh_path": str(bundle.models_root),
            },
        )

        ET.SubElement(
            semantic,
            f"{{{xacro_namespace}}}include",
            {"filename": str(semantic_path)},
        )
        ET.SubElement(
            semantic,
            f"{{{xacro_namespace}}}{str(semantic_spec['macro'])}",
            {"device_name": f"{bundle.device_id}_"},
        )

    for static in static_bundles:
        ET.SubElement(
            robot,
            f"{{{xacro_namespace}}}include",
            {"filename": str(static.entry_path)},
        )
        ET.SubElement(
            robot,
            f"{{{xacro_namespace}}}{static.macro}",
            {
                "parent_link": "world",
                "station_name": "",
                "device_name": f"{static.device_id}_",
                "mesh_path": str(static.models_root),
                **_node_pose_arguments(static.node),
            },
        )

    urdf_doc = xacro.parse(ET.tostring(robot, encoding="unicode"))
    xacro.process_doc(urdf_doc)
    srdf_doc = xacro.parse(ET.tostring(semantic, encoding="unicode"))
    xacro.process_doc(srdf_doc)
    urdf = _freeze_static_joints(
        urdf_doc.toxml(),
        tuple(f"{item.device_id}_" for item in static_bundles),
    )
    return urdf, srdf_doc.toxml()


def _freeze_static_joints(urdf: str, static_prefixes: Sequence[str]) -> str:
    """Turn station scenery joints into fixed ones.

    Static devices contribute no ros2_control interface, so their articulated
    joints would never receive a ``joint_states`` update; robot_state_publisher
    then cannot complete the TF tree. Freezing keeps the geometry visible at its
    declared origin without inventing state.
    """

    if not static_prefixes:
        return urdf
    root = ET.fromstring(urdf)
    for joint in root.findall("joint"):
        name = str(joint.get("name") or "")
        if joint.get("type") == "fixed":
            continue
        if not any(name.startswith(prefix) for prefix in static_prefixes):
            continue
        joint.set("type", "fixed")
        for tag in ("axis", "limit", "dynamics", "safety_controller", "mimic"):
            for child in joint.findall(tag):
                joint.remove(child)
    return ET.tostring(root, encoding="unicode")


def _assemble_moveit_configs(
    bundles: Sequence[MoveItModelBundle],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    ros2: dict[str, Any] = {
        "controller_manager": {
            "ros__parameters": {
                "update_rate": 100,
                # Windows cannot honor realtime thread priorities; leaving the
                # default (50) has been observed to abort ros2_control_node
                # after controllers load (exit 0xC0000409).
                **({"thread_priority": 0} if sys.platform == "win32" else {}),
                "joint_state_broadcaster": {
                    "type": "joint_state_broadcaster/JointStateBroadcaster"
                },
            }
        }
    }
    moveit: dict[str, Any] = {
        "moveit_controller_manager": (
            "moveit_simple_controller_manager/MoveItSimpleControllerManager"
        ),
        "moveit_simple_controller_manager": {"controller_names": []},
    }
    kinematics: dict[str, Any] = {}
    combined_limits: dict[str, Any] = {
        "default_velocity_scaling_factor": 0.1,
        "default_acceleration_scaling_factor": 0.1,
        "joint_limits": {},
    }

    for bundle in bundles:
        configs = _mapping(bundle.manifest, "configs")
        controller_dict = _load_manifest_yaml(bundle, configs, "ros2_controllers")
        moveit_dict = _load_manifest_yaml(bundle, configs, "moveit_controllers")
        kinematics_dict = _load_manifest_yaml(bundle, configs, "kinematics")
        limits_dict = _load_manifest_yaml(bundle, configs, "joint_limits")
        prefix = f"{bundle.device_id}_"

        for group, value in kinematics_dict.items():
            kinematics[f"{prefix}{group}"] = value
        controller_params = controller_dict["controller_manager"]["ros__parameters"]
        for name, value in controller_params.items():
            if name in {"update_rate", "joint_state_broadcaster"}:
                continue
            prefixed_name = f"{prefix}{name}"
            ros2["controller_manager"]["ros__parameters"][prefixed_name] = value
            body = _deep_dict(controller_dict[name])
            body["ros__parameters"]["joints"] = [
                f"{prefix}{joint}" for joint in body["ros__parameters"]["joints"]
            ]
            ros2[prefixed_name] = body

        manager = moveit_dict["moveit_simple_controller_manager"]
        for name in manager["controller_names"]:
            prefixed_name = f"{prefix}{name}"
            moveit["moveit_simple_controller_manager"]["controller_names"].append(
                prefixed_name
            )
            body = _deep_dict(manager[name])
            body["joints"] = [f"{prefix}{joint}" for joint in body["joints"]]
            moveit["moveit_simple_controller_manager"][prefixed_name] = body

        for joint, value in dict(limits_dict.get("joint_limits", {})).items():
            combined_limits["joint_limits"][f"{prefix}{joint}"] = value
    return ros2, moveit, kinematics, combined_limits


def _planning_pipeline_parameters(get_package_share_directory) -> dict[str, Any]:
    from launch_param_builder import load_yaml

    default_folder = (
        Path(get_package_share_directory("moveit_configs_utils")) / "default_configs"
    )
    pattern = re.compile(r"^(.*)_planning.yaml$")
    pipelines = sorted(
        match.group(1)
        for path in default_folder.iterdir()
        if path.is_file() and (match := pattern.search(path.name))
    )
    if not pipelines:
        raise OSError("moveit_configs_utils 没有 planning pipeline")
    parameters: dict[str, Any] = {
        "planning_pipelines": pipelines,
        "default_planning_pipeline": "ompl" if "ompl" in pipelines else pipelines[0],
    }
    for pipeline in pipelines:
        parameters[pipeline] = load_yaml(default_folder / f"{pipeline}_planning.yaml")
    if "ompl" in parameters and "planner_configs" not in parameters["ompl"]:
        parameters["ompl"].update(load_yaml(default_folder / "ompl_defaults.yaml"))
    return parameters


def _validate_move_group_specs(
    bundles: Sequence[MoveItModelBundle],
    kinematics: Mapping[str, Any],
    joint_limits: Mapping[str, Any],
) -> None:
    all_limits = joint_limits.get("joint_limits", {})
    if not isinstance(all_limits, Mapping):
        raise ValueError("MoveIt joint_limits 必须是对象")
    for bundle in bundles:
        configs = _mapping(bundle.manifest, "configs")
        groups = _load_manifest_yaml(bundle, configs, "move_group")
        if not groups:
            raise ValueError("MoveIt move_group spec 不能为空")
        prefix = f"{bundle.device_id}_"
        for group_name, raw_spec in groups.items():
            if not isinstance(raw_spec, Mapping):
                raise ValueError(f"MoveIt group {group_name} 必须是对象")
            prefixed_group = f"{prefix}{group_name}"
            if prefixed_group not in kinematics:
                raise ValueError(
                    f"MoveIt group {prefixed_group} 缺少 kinematics 配置"
                )
            joints = raw_spec.get("joint_names")
            if not isinstance(joints, Sequence) or isinstance(joints, (str, bytes)):
                raise ValueError(f"MoveIt group {group_name}.joint_names 必须是数组")
            missing = [
                str(joint)
                for joint in joints
                if f"{prefix}{joint}" not in all_limits
            ]
            if missing:
                raise ValueError(
                    f"MoveIt group {group_name} 的 joints 缺少 limit: {missing}"
                )
            for field in ("base_link_name", "end_effector_name"):
                if not str(raw_spec.get(field) or "").strip():
                    raise ValueError(f"MoveIt group {group_name}.{field} 不能为空")


def _validate_manifest_files(
    manifest: Mapping[str, Any],
    manifest_path: Path,
    models_root: Path,
) -> None:
    for section in ("robot", "semantic", "ros2_control"):
        spec = _mapping(manifest, section)
        if not str(spec.get("macro") or "").strip():
            raise ValueError(f"MoveIt manifest {section}.macro 不能为空")
        _safe_runtime_path(manifest_path.parent, str(spec.get("entry") or ""), models_root)
    configs = _mapping(manifest, "configs")
    for name in (
        "move_group",
        "kinematics",
        "joint_limits",
        "moveit_controllers",
        "ros2_controllers",
    ):
        _safe_runtime_path(manifest_path.parent, str(configs.get(name) or ""), models_root)


def _validate_sim_runtime_profile(bundle: MoveItModelBundle) -> None:
    runtime = _mapping(bundle.manifest, "runtime")
    config = bundle.node.get("config")
    if not isinstance(config, Mapping):
        config = {}
    selected_ref = str(config.get("standard_moveit_hardware_profile_ref") or "")
    manifest_ref = str(runtime.get("hardware_profile_ref") or "")
    if not selected_ref or selected_ref != manifest_ref:
        raise ValueError(
            f"MoveIt graph HardwareProfile={selected_ref or '-'} 与 package={manifest_ref or '-'} 不一致"
        )
    if runtime.get("environment") != "simulation":
        raise ValueError("当前 MoveItRuntime 只接受 environment=simulation")
    if runtime.get("ros2_control_plugin") != "mock_components/GenericSystem":
        raise ValueError("moveit_sim 必须显式使用 mock_components/GenericSystem")


def _validate_mock_hardware_urdf(urdf: str) -> None:
    root = ET.fromstring(urdf)
    plugins = [
        str(node.text or "").strip()
        for node in root.findall(".//ros2_control/hardware/plugin")
    ]
    if not plugins or any(plugin != "mock_components/GenericSystem" for plugin in plugins):
        raise ValueError(
            "moveit_sim 展开的 URDF 必须且只能声明 mock_components/GenericSystem"
        )


def _write_rviz_config(runtime_root: Path, planning_group: str) -> Path:
    source = Path(__file__).with_name("view_robot.rviz")
    content = source.read_text(encoding="utf-8")
    content = re.sub(
        r"Planning Group:.*",
        f"Planning Group: {planning_group}",
        content,
    )
    target = runtime_root / "moveit.rviz"
    target.write_text(content, encoding="utf-8")
    return target


def _manifest_file(
    bundle: MoveItModelBundle,
    spec: Mapping[str, Any],
    name: str,
) -> Path:
    return _safe_runtime_path(
        bundle.manifest_path.parent,
        str(spec.get(name) or ""),
        bundle.models_root,
    )


def _load_manifest_yaml(
    bundle: MoveItModelBundle,
    configs: Mapping[str, Any],
    name: str,
) -> dict[str, Any]:
    path = _safe_runtime_path(
        bundle.manifest_path.parent,
        str(configs.get(name) or ""),
        bundle.models_root,
    )
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"MoveIt config {name} 必须是对象")
    return _deep_dict(value)


def _safe_runtime_path(base: Path, relative: str, root: Path) -> Path:
    logical = PurePosixPath(relative)
    if not relative or logical.is_absolute() or "\\" in relative:
        raise ValueError(f"MoveIt manifest 路径非法: {relative}")
    path = base.joinpath(*logical.parts).resolve(strict=True)
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f"MoveIt manifest 路径逃逸或非文件: {relative}")
    return path


def _safe_logical_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError(f"Package logical path 非法: {value}")
    return path


def _safe_segment(value: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError(f"device id 不能作为 runtime 目录: {value}")
    return value


def _mapping(value: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    result = value.get(name)
    if not isinstance(result, Mapping):
        raise ValueError(f"MoveIt manifest {name} 必须是对象")
    return result


def _deep_dict(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _deep_dict(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_deep_dict(item) for item in value]
    return value


def _parent_index(nodes_by_id: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    """Derive child -> parent, whichever nesting form the graph arrived in.

    A raw graph carries ``children`` id lists, while a graph rebuilt from a
    ResourceTreeSet carries ``parent_uuid`` instead. Both are honoured so the
    parent chain survives either conversion.
    """

    parent_of: dict[str, str] = {}
    for node_id, node in nodes_by_id.items():
        children = node.get("children") or []
        if isinstance(children, Mapping):
            children = list(children)
        if not isinstance(children, (list, tuple, set)):
            continue
        for child in children:
            child_id = child.get("id") if isinstance(child, Mapping) else child
            if child_id:
                parent_of[str(child_id)] = str(node_id)

    id_by_uuid = {
        str(node["uuid"]): str(node_id)
        for node_id, node in nodes_by_id.items()
        if node.get("uuid")
    }
    for node_id, node in nodes_by_id.items():
        parent_uuid = node.get("parent_uuid")
        parent_id = id_by_uuid.get(str(parent_uuid)) if parent_uuid else None
        if parent_id and parent_id != str(node_id):
            parent_of.setdefault(str(node_id), parent_id)
    return parent_of


def _flatten_node_to_world(
    node: Mapping[str, Any],
    nodes_by_id: Mapping[str, Mapping[str, Any]],
    parent_of: Mapping[str, str],
) -> Mapping[str, Any] | None:
    """Accumulate a nested node's position up to the graph root.

    Static macros are all anchored to ``world``, so a child node's local
    position has to be summed along its parent chain. Returns ``None`` when an
    ancestor is rotated, because composing that rotation here would silently
    guess a pose the graph never declared.
    """

    x = y = z = 0.0
    current: Mapping[str, Any] | None = node
    visited: set[str] = set()
    while current is not None:
        node_id = str(current.get("id") or "")
        if node_id in visited:
            return None
        visited.add(node_id)
        position = _node_position(current)
        x += float(position.get("x", 0.0) or 0.0)
        y += float(position.get("y", 0.0) or 0.0)
        z += float(position.get("z", 0.0) or 0.0)
        parent_id = parent_of.get(node_id) or current.get("parent")
        if not parent_id:
            break
        parent = nodes_by_id.get(str(parent_id))
        if parent is None:
            break
        if any(abs(float(value or 0.0)) > 0.0 for value in _node_rotation(parent).values()):
            return None
        current = parent

    placed = dict(node)
    placed["position"] = {"x": x, "y": y, "z": z}
    placed.pop("pose", None)
    return placed


def _node_position(node: Mapping[str, Any]) -> Mapping[str, Any]:
    position = node.get("position") or {}
    if isinstance(position, Mapping) and isinstance(position.get("position"), Mapping):
        position = position["position"]
    pose = node.get("pose")
    if isinstance(pose, Mapping) and isinstance(pose.get("position"), Mapping):
        position = pose["position"]
    return position if isinstance(position, Mapping) else {}


def _node_rotation(node: Mapping[str, Any]) -> Mapping[str, Any]:
    config = node.get("config")
    rotation: Mapping[str, Any] = {}
    if isinstance(config, Mapping) and isinstance(config.get("rotation"), Mapping):
        rotation = config["rotation"]
    pose = node.get("pose")
    if isinstance(pose, Mapping) and isinstance(pose.get("rotation"), Mapping):
        rotation = pose["rotation"]
    return rotation


def _node_pose_arguments(node: Mapping[str, Any]) -> dict[str, str]:
    position = _node_position(node)
    rotation = _node_rotation(node)
    return {
        "x": str(float(position.get("x", 0.0)) / 1000.0),
        "y": str(float(position.get("y", 0.0)) / 1000.0),
        "z": str(float(position.get("z", 0.0)) / 1000.0),
        "rx": str(float(rotation.get("x", 0.0))),
        "ry": str(float(rotation.get("y", 0.0))),
        "r": str(float(rotation.get("z", 0.0))),
    }


def _ros_launch_node_env() -> dict[str, str]:
    """Environment for MoveIt/RViz launch nodes.

    On Windows RoboStack, ``rviz2`` needs ``Library/opt/rviz_ogre_vendor/bin``
    on PATH (Ogre DLLs). Conda's activate hook calls ``local_setup.ps1``, which
    is often missing while only ``local_setup.bat`` exists — so Edge-spawned
    RViz dies with ``0xC0000135`` unless we prepend those dirs here.
    """
    env = {str(k): str(v) for k, v in os.environ.items()}
    if sys.platform != "win32":
        return env
    prefix = (
        Path(os.environ.get("CONDA_PREFIX") or os.environ.get("AMENT_PREFIX_PATH") or "")
        .expanduser()
    )
    if not prefix.is_dir():
        # Fall back to the Python env root (…/envs/unilab).
        prefix = Path(sys.prefix)
    candidates = [
        prefix / "Library" / "opt" / "rviz_ogre_vendor" / "bin",
        prefix / "Library" / "bin",
        prefix / "Scripts",
        prefix,
    ]
    prepend = [str(p) for p in candidates if p.is_dir()]
    if prepend:
        current = env.get("PATH", "")
        env["PATH"] = os.pathsep.join(prepend + ([current] if current else []))
    plugins = prefix / "Library" / "plugins"
    if plugins.is_dir():
        env.setdefault("QT_PLUGIN_PATH", str(plugins))
    library = prefix / "Library"
    if library.is_dir():
        env.setdefault("AMENT_PREFIX_PATH", str(library))
    env.setdefault("RMW_IMPLEMENTATION", "rmw_fastrtps_cpp")
    return env


def _launch_description_from_bundle(runtime_root: Path):
    from ament_index_python.packages import get_package_share_directory
    from launch import LaunchDescription
    from launch_ros.actions import Node
    from launch_ros.parameter_descriptions import ParameterFile

    payload = yaml.safe_load((runtime_root / "launch_bundle.yaml").read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("launch_bundle.yaml 必须是对象")
    urdf = str(payload.get("urdf") or "")
    srdf = str(payload.get("srdf") or "")
    controller_path = runtime_root / str(payload.get("controller_path") or "ros2_controllers.yaml")
    moveit_controllers = payload.get("moveit_controllers")
    kinematics = payload.get("kinematics")
    joint_limits = payload.get("joint_limits")
    if not isinstance(moveit_controllers, Mapping):
        raise ValueError("launch_bundle.moveit_controllers 必须是对象")
    if not isinstance(kinematics, Mapping):
        raise ValueError("launch_bundle.kinematics 必须是对象")
    if not isinstance(joint_limits, Mapping):
        raise ValueError("launch_bundle.joint_limits 必须是对象")

    node_env = _ros_launch_node_env()
    launch = LaunchDescription()
    controller_params = ParameterFile(str(controller_path), allow_substs=True)
    launch.add_action(
        Node(
            package="controller_manager",
            executable="ros2_control_node",
            output="screen",
            parameters=[{"robot_description": urdf}, controller_params],
            env=node_env,
        )
    )
    for controller in moveit_controllers[
        "moveit_simple_controller_manager"
    ]["controller_names"]:
        launch.add_action(
            Node(
                package="controller_manager",
                executable="spawner",
                arguments=[controller, "--controller-manager", "controller_manager"],
                output="screen",
                env=node_env,
            )
        )
    launch.add_action(
        Node(
            package="controller_manager",
            executable="spawner",
            arguments=[
                "joint_state_broadcaster",
                "--controller-manager",
                "controller_manager",
            ],
            output="screen",
            env=node_env,
        )
    )
    launch.add_action(
        Node(
            package="robot_state_publisher",
            executable="robot_state_publisher",
            name="robot_state_publisher",
            output="screen",
            parameters=[{"robot_description": urdf, "use_sim_time": False}],
            env=node_env,
        )
    )

    planning = _planning_pipeline_parameters(get_package_share_directory)
    moveit_params: list[Mapping[str, Any]] = [
        {
            "allow_trajectory_execution": True,
            "robot_description": urdf,
            "robot_description_semantic": srdf,
            "robot_description_kinematics": kinematics,
            "publish_monitored_planning_scene": True,
            "publish_robot_description_semantic": True,
            "publish_planning_scene": True,
            "publish_geometry_updates": True,
            "publish_state_updates": True,
            "publish_transforms_updates": True,
        },
        {"robot_description_planning": joint_limits},
        planning,
        moveit_controllers,
    ]
    launch.add_action(
        Node(
            package="moveit_ros_move_group",
            executable="move_group",
            output="screen",
            parameters=moveit_params,
            env=node_env,
        )
    )
    if bool(payload.get("enable_rviz")):
        rviz_name = str(payload.get("rviz_config") or "moveit.rviz")
        rviz_config = runtime_root / rviz_name
        launch.add_action(
            Node(
                package="rviz2",
                executable="rviz2",
                name="rviz2",
                arguments=["-d", str(rviz_config)],
                output="screen",
                parameters=[
                    {
                        "robot_description": urdf,
                        "robot_description_semantic": srdf,
                    },
                    {"robot_description_kinematics": kinematics},
                    {"robot_description_planning": joint_limits},
                    planning,
                ],
                env=node_env,
            )
        )
    return launch


def _launch_service_process_main(runtime_root: str) -> None:
    root = Path(runtime_root)
    ready_path = root / ".launch_ready"
    error_path = root / ".launch_error"
    try:
        from launch import LaunchService

        launch_description = _launch_description_from_bundle(root)
        service = LaunchService(noninteractive=True)
        service.include_launch_description(launch_description)
        ready_path.write_text("1", encoding="utf-8")
        exit_code = service.run()
        if exit_code not in (None, 0):
            error_path.write_text(f"LaunchService exit_code={exit_code}", encoding="utf-8")
            raise SystemExit(int(exit_code or 1))
    except BaseException as exc:
        if not error_path.is_file():
            error_path.write_text(f"{type(exc).__name__}: {exc}", encoding="utf-8")
        raise


__all__ = [
    "MoveItModelBundle",
    "MoveItRuntime",
    "PackageMoveItModelResolver",
]
