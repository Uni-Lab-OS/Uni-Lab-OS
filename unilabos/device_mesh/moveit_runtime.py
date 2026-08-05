"""MoveIt runtime independent from RViz/web presentation.

The runtime consumes audited package model assets, owns its generated files and
LaunchService lifecycle, and only adds RViz when an explicit view is requested.
ROS/MoveIt imports are intentionally lazy so PLC-only deployments do not need
those packages at import time.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET

import yaml


@dataclass(frozen=True)
class MoveItModelBundle:
    device_id: str
    models_root: Path
    manifest: Mapping[str, Any]
    manifest_path: Path
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
        self._launch_service: Any | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._startup_error: BaseException | None = None
        self._started_event = threading.Event()

    @property
    def enabled(self) -> bool:
        return bool(self._moveit_device_ids)

    @property
    def enable_rviz(self) -> bool:
        return self._enable_rviz

    @property
    def healthy(self) -> bool:
        thread = self._thread
        return (
            self.enabled
            and self._startup_error is None
            and thread is not None
            and thread.is_alive()
        )

    @property
    def startup_error(self) -> BaseException | None:
        return self._startup_error

    def start(self, timeout: float = 10.0) -> None:
        """Start the ROS launch tree once; explicit MoveIt failure is fatal."""

        with self._lock:
            if not self.enabled or (self._thread is not None and self._thread.is_alive()):
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
                launch_description = self._create_launch_description(self._runtime_root)
            except BaseException:
                shutil.rmtree(self._runtime_root, ignore_errors=True)
                self._runtime_root = None
                raise
            self._startup_error = None
            self._started_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                args=(launch_description,),
                name="moveit_runtime",
                daemon=True,
            )
            self._thread.start()
        if not self._started_event.wait(timeout=max(0.1, float(timeout))):
            self.stop(timeout=timeout)
            raise TimeoutError("MoveIt LaunchService 初始化超时")
        thread = self._thread
        if thread is not None:
            # Catch immediate run-loop failures before publishing the runtime.
            # Controller/action readiness remains enforced by the execution
            # Adapter because rclpy is owned by the backend started next.
            thread.join(timeout=min(0.25, max(0.0, float(timeout))))
        if self._startup_error is not None:
            error = self._startup_error
            self.stop(timeout=timeout)
            raise RuntimeError(f"MoveIt LaunchService 启动失败: {error}") from error
        if thread is None or not thread.is_alive():
            self.stop(timeout=timeout)
            raise RuntimeError("MoveIt LaunchService 在启动阶段提前退出")

    def _run(self, launch_description: Any) -> None:
        service = None
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            from launch import LaunchService

            service = LaunchService(noninteractive=True)
            service.include_launch_description(launch_description)
            with self._lock:
                self._launch_service = service
            self._started_event.set()
            exit_code = service.run()
            if exit_code not in (None, 0):
                raise RuntimeError(f"LaunchService exit_code={exit_code}")
        except BaseException as exc:
            self._startup_error = exc
            self._started_event.set()
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    def stop(self, timeout: float = 10.0) -> None:
        with self._lock:
            service = self._launch_service
            thread = self._thread
            runtime_root = self._runtime_root
        if service is not None:
            shutdown = getattr(service, "shutdown", None)
            if callable(shutdown):
                shutdown()
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, float(timeout)))
        if thread is not None and thread.is_alive():
            raise TimeoutError(
                "MoveIt LaunchService 未在期限内退出；为避免子进程读取失效文件，保留 runtime 目录"
            )
        with self._lock:
            self._launch_service = None
            self._thread = None
            self._runtime_root = None
        if runtime_root is not None:
            shutil.rmtree(runtime_root, ignore_errors=True)

    def _create_launch_description(self, runtime_root: Path):
        from ament_index_python.packages import get_package_share_directory
        from launch import LaunchDescription
        from launch_ros.actions import Node
        from launch_ros.parameter_descriptions import ParameterFile

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

        urdf, srdf = _assemble_robot_descriptions(bundles)
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

        launch = LaunchDescription()
        controller_params = ParameterFile(str(controller_path), allow_substs=True)
        launch.add_action(
            Node(
                package="controller_manager",
                executable="ros2_control_node",
                output="screen",
                parameters=[{"robot_description": urdf}, controller_params],
                env=dict(os.environ),
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
                    env=dict(os.environ),
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
                env=dict(os.environ),
            )
        )
        launch.add_action(
            Node(
                package="robot_state_publisher",
                executable="robot_state_publisher",
                name="robot_state_publisher",
                output="screen",
                parameters=[{"robot_description": urdf, "use_sim_time": False}],
                env=dict(os.environ),
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
                env=dict(os.environ),
            )
        )
        if self._enable_rviz:
            planning_group = next(iter(kinematics), "")
            rviz_config = _write_rviz_config(runtime_root, planning_group)
            launch.add_action(
                Node(
                    package="rviz2",
                    executable="rviz2",
                    name="rviz2",
                    arguments=[
                        "-d",
                        str(rviz_config),
                    ],
                    output="screen",
                    parameters=[
                        {"robot_description_kinematics": kinematics},
                        {"robot_description_planning": joint_limits},
                        planning,
                    ],
                    env=dict(os.environ),
                )
            )
        return launch


def _assemble_robot_descriptions(
    bundles: Sequence[MoveItModelBundle],
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

    urdf_doc = xacro.parse(ET.tostring(robot, encoding="unicode"))
    xacro.process_doc(urdf_doc)
    srdf_doc = xacro.parse(ET.tostring(semantic, encoding="unicode"))
    xacro.process_doc(srdf_doc)
    return urdf_doc.toxml(), srdf_doc.toxml()


def _assemble_moveit_configs(
    bundles: Sequence[MoveItModelBundle],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    ros2: dict[str, Any] = {
        "controller_manager": {
            "ros__parameters": {
                "update_rate": 100,
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


def _node_pose_arguments(node: Mapping[str, Any]) -> dict[str, str]:
    position = node.get("position") or {}
    if isinstance(position, Mapping) and isinstance(position.get("position"), Mapping):
        position = position["position"]
    if not isinstance(position, Mapping):
        position = {}
    pose = node.get("pose")
    if isinstance(pose, Mapping) and isinstance(pose.get("position"), Mapping):
        position = pose["position"]
    config = node.get("config")
    rotation: Mapping[str, Any] = {}
    if isinstance(config, Mapping) and isinstance(config.get("rotation"), Mapping):
        rotation = config["rotation"]
    if isinstance(pose, Mapping) and isinstance(pose.get("rotation"), Mapping):
        rotation = pose["rotation"]
    return {
        "x": str(float(position.get("x", 0.0)) / 1000.0),
        "y": str(float(position.get("y", 0.0)) / 1000.0),
        "z": str(float(position.get("z", 0.0)) / 1000.0),
        "rx": str(float(rotation.get("x", 0.0))),
        "ry": str(float(rotation.get("y", 0.0))),
        "r": str(float(rotation.get("z", 0.0))),
    }


__all__ = [
    "MoveItModelBundle",
    "MoveItRuntime",
    "PackageMoveItModelResolver",
]
