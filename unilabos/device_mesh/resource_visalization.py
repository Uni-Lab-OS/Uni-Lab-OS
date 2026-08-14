import json
import os
import re
import shutil
import sys
import tempfile
import weakref
from pathlib import Path

import xacro
import yaml
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription, LaunchService
from launch.actions import ExecuteProcess, RegisterEventHandler, Shutdown
from launch.event_handlers import OnProcessExit
from launch_param_builder import load_yaml
from launch_ros.actions import Node as nd
from launch_ros.parameter_descriptions import ParameterFile
from lxml import etree

from unilabos.device_mesh.motion_runtime_plan import node_requests_moveit
from unilabos.device_mesh.package_moveit_model import (
    get_ros_model_type,
    load_package_moveit_model,
    merge_package_moveit_parameters,
)
from unilabos.registry.registry import lab_registry


def controller_spawn_order(controller_names) -> tuple[str, ...]:
    """Return the deterministic ros2_control activation order.

    The state broadcaster owns the first activation slot.  Motion controllers
    follow in the exact model-declared order, with duplicates removed.  Keeping
    activation serialized also makes failure ownership deterministic and avoids
    overlapping ``switch_controller`` requests during startup.
    """

    ordered = ["joint_state_broadcaster", *controller_names]
    return tuple(dict.fromkeys(str(name) for name in ordered if str(name)))


def should_use_builtin_simulation_controller(
    *,
    platform_name: str,
    moveit_device_ids: tuple[str, ...],
    simulated_moveit_device_ids: tuple[str, ...],
) -> bool:
    """仅为 macOS 上的纯仿真 MoveIt 图选择内置轨迹 Action 后端。

    ros2_control 2.51 的 ControllerManager 在 libc++ 上激活控制器时会把未持锁的
    ``unique_lock`` 交给 ``condition_variable::wait_for`` 并终止进程。该兼容
    后端只替换无硬件 simulation 的控制器传输；Live、混合图和非 macOS 平台仍
    使用标准 ros2_control。
    """

    moveit = tuple(dict.fromkeys(str(value) for value in moveit_device_ids))
    simulated = set(str(value) for value in simulated_moveit_device_ids)
    return bool(moveit) and platform_name == "darwin" and set(moveit) == simulated


def simulation_controller_specs(moveit_controllers: dict) -> tuple[dict, ...]:
    """从 MoveItSimpleControllerManager 配置派生仿真 Action 合同。"""

    manager = moveit_controllers.get("moveit_simple_controller_manager")
    if not isinstance(manager, dict):
        raise TypeError("MoveIt 仿真缺少 moveit_simple_controller_manager")
    specs: list[dict] = []
    for raw_name in manager.get("controller_names", ()):
        name = str(raw_name).strip().strip("/")
        config = manager.get(raw_name)
        if not name or not isinstance(config, dict):
            raise ValueError("MoveIt 仿真 controller 声明无效")
        if str(config.get("type") or "") != "FollowJointTrajectory":
            raise ValueError(f"MoveIt 仿真不支持 controller 类型: {name}")
        action_ns = str(config.get("action_ns") or "").strip().strip("/")
        joints = [str(value).strip() for value in config.get("joints", ())]
        if not action_ns or not joints or any(not value for value in joints):
            raise ValueError(f"MoveIt 仿真 controller 缺少 Action 或关节: {name}")
        specs.append(
            {
                "name": name,
                "action": f"/{name}/{action_ns}",
                "joints": joints,
            }
        )
    if not specs:
        raise ValueError("MoveIt 仿真没有可执行 controller")
    return tuple(specs)


def _start_next_controller_on_success(next_spawner, controller_name: str):
    """Create an OnProcessExit callback that fails the MoveIt launch closed."""

    def _on_exit(event, _context):
        if event.returncode == 0:
            return [next_spawner]
        return [
            Shutdown(
                reason=(
                    "ros2_control controller activation failed before "
                    f"{controller_name} (exit={event.returncode})"
                )
            )
        ]

    return _on_exit


def get_pattern_matches(folder, pattern):
    """Given all the files in the folder, find those that match the pattern.

    If there are groups defined, the groups are returned. Otherwise the path to the matches are returned.
    """
    matches = []
    if not folder.exists():
        return matches
    for child in folder.iterdir():
        if not child.is_file():
            continue
        m = pattern.search(child.name)
        if m:
            groups = m.groups()
            if groups:
                matches.append(groups[0])
            else:
                matches.append(child)
    return matches

class ResourceVisualization:
    def __init__(
        self,
        device: dict,
        resource: dict,
        enable_rviz: bool = True,
        required_moveit_device_ids: tuple[str, ...] = (),
        simulated_moveit_device_ids: tuple[str, ...] = (),
    ):
        """初始化资源 ROS Launch 组合器。
        
        该类用于将设备和资源的3D模型可视化展示。通过解析设备和资源的配置信息,
        从注册表中获取对应的3D模型文件,并使用ROS2和RViz进行可视化。
        
        Args:
            device (dict): 设备配置字典,包含设备的类型、位置等信息
            resource (dict): 资源配置字典,包含资源的类型、位置等信息 
            registry (dict): 注册表字典,包含设备和资源类型的注册信息
            enable_rviz (bool, optional): 是否启用RViz可视化. Defaults to True.
            required_moveit_device_ids: Graph 明确要求由本 Launch owner 启动的
                MoveIt Device id；缺少可运行模型时关闭失败。

        Returns:
            None.

        Safety:
            ``required_moveit_device_ids`` 只声明运动基础设施，不证明设备许可或
            硬件就绪；任何声明设备未解析为 MoveIt 模型都会拒绝启动。
        """
        self.launch_service = LaunchService()
        self.launch_description = LaunchDescription()
        self.resource_dict = resource
        self.resource_model = {}
        self.resource_type = ['deck', 'plate', 'container', 'tip_rack']
        self.mesh_path = Path(__file__).parent.absolute()
        self.enable_rviz = enable_rviz
        self.required_moveit_device_ids = tuple(required_moveit_device_ids)
        self.simulated_moveit_device_ids = tuple(simulated_moveit_device_ids)
        self.runtime_dir = Path(tempfile.mkdtemp(prefix="unilab-resource-runtime-"))
        self._runtime_finalizer = weakref.finalize(
            self,
            shutil.rmtree,
            self.runtime_dir,
            ignore_errors=True,
        )
        self._launch_prepared = False
        registry = lab_registry

        self.srdf_str = '''<?xml version="1.0" ?>
        <robot xmlns:xacro="http://ros.org/wiki/xacro" name="full_dev">

        </robot>
        '''
        self.robot_state_str= '''<?xml version="1.0" ?>
        <robot xmlns:xacro="http://ros.org/wiki/xacro" name="full_dev">
        <link name="world"/>
        </robot>
        '''
        self.root = etree.fromstring(self.robot_state_str)
        self.root_srdf = etree.fromstring(self.srdf_str)
                
        xacro_uri = self.root.nsmap["xacro"]

        self.moveit_nodes = {}
        self.legacy_moveit_nodes = {}
        self.moveit_nodes_kinematics = {}
        self.moveit_joint_limits = {"joint_limits": {}}
        self.moveit_controllers_yaml = {
            "moveit_controller_manager": "moveit_simple_controller_manager/MoveItSimpleControllerManager",
            "moveit_simple_controller_manager": {
                "controller_names": []
            }
        }
        self.ros2_controllers_yaml = {
            "controller_manager": {
                "ros__parameters": {
                    "update_rate": 100,
                    "joint_state_broadcaster": {
                        "type": "joint_state_broadcaster/JointStateBroadcaster",
                    }
                }
            }
        }

        # 遍历设备节点
        for node in device.values():
            if (node['type'] in self.resource_type and node['class'] != '') or (node['type'] == 'device' and node['class'] != ''):
                model_config = {}
                if node['type'] in self.resource_type:
                    resource_class = node['class']
                    if resource_class not in registry.resource_type_registry.keys():
                        raise ValueError(f"{node['id']}资源类型 {resource_class} 未在注册表中注册")
                    elif "model" in registry.resource_type_registry[resource_class].keys():
                        model_config = registry.resource_type_registry[resource_class]['model']
                elif node['type'] == 'device' and node['class'] != '':
                    device_class = node['class']
                    if device_class not in registry.device_type_registry.keys():
                        raise ValueError(f"{node['id']}设备类型 {device_class} 未在注册表中注册")
                    elif "model" in registry.device_type_registry[device_class].keys():
                        model_config = registry.device_type_registry[device_class]['model']
                if model_config:
                    model_type = get_ros_model_type(model_config)
                    if model_type == 'package_moveit':
                        self._add_package_moveit_model(
                            node,
                            model_config,
                            enable_motion=node_requests_moveit(node),
                        )
                    elif model_type == 'resource':
                        self.resource_model[node['id']] = {
                            'mesh': f"{str(self.mesh_path)}/resources/{model_config['mesh']}",
                            'mesh_tf': model_config['mesh_tf']}
                        if 'children_mesh' in model_config:
                            if model_config['children_mesh'] is not None:
                                self.resource_model[f"{node['id']}_"] = {
                                    'mesh': f"{str(self.mesh_path)}/resources/{model_config['children_mesh']}",
                                    'mesh_tf': model_config['children_mesh_tf']
                                }
                    elif model_type == 'device':

                        new_include = etree.SubElement(self.root, f"{{{xacro_uri}}}include")
                        new_include.set("filename", f"{str(self.mesh_path)}/devices/{model_config['mesh']}/macro_device.xacro")
                        new_dev = etree.SubElement(self.root, f"{{{xacro_uri}}}{model_config['mesh']}")
                        new_dev.set("parent_link", "world")
                        new_dev.set("mesh_path", str(self.mesh_path))
                        new_dev.set("device_name", node["id"]+"_")
                        # if node["parent"] is not None:
                        #     new_dev.set("station_name", node["parent"]+'_')
                        if "position" in node:
                            new_dev.set("x",str(float(node["position"]["position"]["x"])/1000))
                            new_dev.set("y",str(float(node["position"]["position"]["y"])/1000))
                            new_dev.set("z",str(float(node["position"]["position"]["z"])/1000))
                        if "rotation" in node["config"]:
                            new_dev.set("rx",str(float(node["config"]["rotation"]["x"])))
                            new_dev.set("ry",str(float(node["config"]["rotation"]["y"])))
                            new_dev.set("r",str(float(node["config"]["rotation"]["z"])))
                        if "pose" in node:
                            new_dev.set("x",str(float(node["pose"]["position"]["x"])/1000))
                            new_dev.set("y",str(float(node["pose"]["position"]["y"])/1000))
                            new_dev.set("z",str(float(node["pose"]["position"]["z"])/1000))
                            new_dev.set("rx",str(float(node["pose"]["rotation"]["x"])))
                            new_dev.set("ry",str(float(node["pose"]["rotation"]["y"])))
                            new_dev.set("r",str(float(node["pose"]["rotation"]["z"])))
                        if "device_config" in node["config"]:
                            for key, value in node["config"]["device_config"].items():
                                new_dev.set(key, str(value))

                        # 添加ros2_controller
                        if node_requests_moveit(node):
                            new_include_controller = etree.SubElement(self.root, f"{{{xacro_uri}}}include")
                            new_include_controller.set("filename", f"{str(self.mesh_path)}/devices/{model_config['mesh']}/config/macro.ros2_control.xacro")
                            new_controller = etree.SubElement(self.root, f"{{{xacro_uri}}}{model_config['mesh']}_ros2_control")
                            new_controller.set("device_name", node["id"]+"_")
                            new_controller.set("mesh_path", str(self.mesh_path))

                            # 添加moveit的srdf
                            new_include_srdf = etree.SubElement(self.root_srdf, f"{{{xacro_uri}}}include")
                            new_include_srdf.set("filename", f"{str(self.mesh_path)}/devices/{model_config['mesh']}/config/macro.srdf.xacro")
                            new_srdf = etree.SubElement(self.root_srdf, f"{{{xacro_uri}}}{model_config['mesh']}_srdf")
                            new_srdf.set("device_name", node["id"]+"_")
                            self.moveit_nodes[node["id"]] = model_config['mesh']
                            self.legacy_moveit_nodes[node["id"]] = model_config['mesh']
                    elif model_type is not None:
                        print("错误的注册表类型！")
        re = etree.tostring(self.root, encoding="unicode")
        doc = xacro.parse(re)
        xacro.process_doc(doc)
        self.urdf_str = doc.toxml()


        re_srdf = etree.tostring(self.root_srdf, encoding="unicode")
        doc_srdf = xacro.parse(re_srdf)
        xacro.process_doc(doc_srdf)
        self.urdf_str_srdf = doc_srdf.toxml()


        if self.moveit_nodes:
            self.moveit_init()
        missing_moveit_models = sorted(
            set(self.required_moveit_device_ids) - set(self.moveit_nodes)
        )
        if missing_moveit_models:
            self.stop()
            raise ValueError(
                "Graph 要求 MoveIt 运动运行时，但以下 Device 没有可运行的 "
                "URDF/SRDF/controller 模型资产: " + ", ".join(missing_moveit_models)
            )

    def moveit_init(self):

        for name, config in self.legacy_moveit_nodes.items():
            controller_dict = yaml.safe_load(open(f"{str(self.mesh_path)}/devices/{config}/config/ros2_controllers.yaml", "r"))
            moveit_dict = yaml.safe_load(open(f"{str(self.mesh_path)}/devices/{config}/config/moveit_controllers.yaml", "r"))
            kinematics_dict = yaml.safe_load(open(f"{str(self.mesh_path)}/devices/{config}/config/kinematics.yaml", "r"))
            
            for key_kinematics, value_kinematics in kinematics_dict.items():
                self.moveit_nodes_kinematics[f'{name}_{key_kinematics}'] = value_kinematics
            
            for key, value in controller_dict['controller_manager']['ros__parameters'].items():
                if key == 'update_rate' or key == 'joint_state_broadcaster':
                    continue
                self.ros2_controllers_yaml['controller_manager']['ros__parameters'][f"{name}_{key}"] = value
                controller_dict[key]['ros__parameters']['joints'] = [f"{name}_{joint}" for joint in controller_dict[key]['ros__parameters']['joints']]
                self.ros2_controllers_yaml[f"{name}_{key}"] = controller_dict[key]

            for controller_name in moveit_dict['moveit_simple_controller_manager']['controller_names']:
                self.moveit_controllers_yaml['moveit_simple_controller_manager']['controller_names'].append(f"{name}_{controller_name}")
                moveit_dict['moveit_simple_controller_manager'][controller_name]['joints'] = [f"{name}_{joint}" for joint in moveit_dict['moveit_simple_controller_manager'][controller_name]['joints']]
                self.moveit_controllers_yaml['moveit_simple_controller_manager'][f"{name}_{controller_name}"] = moveit_dict['moveit_simple_controller_manager'][controller_name]

    def _add_package_moveit_model(
        self,
        node: dict,
        model_config: dict,
        *,
        enable_motion: bool,
    ) -> None:
        """把 distribution Provider 的六轴模型并入本进程唯一 Launch 描述。

        参数：``node`` 是 Graph Device；``model_config`` 是已发布 Catalog 模型
        声明。返回：无。异常：Provider 摘要、XML 或控制器形状无效时传播。
        安全：Provider 只能贡献型号模型和执行参数；RViz 仍由 OS 独立开关拥有。
        """

        bundle = load_package_moveit_model(model_config, node)
        robot = etree.fromstring(bundle.urdf.encode("utf-8"))
        for child in robot:
            self.root.append(child)
        if not enable_motion:
            return

        semantic = etree.fromstring(bundle.srdf.encode("utf-8"))
        for child in semantic:
            self.root_srdf.append(child)

        merge_package_moveit_parameters(
            bundle,
            ros2_controllers=self.ros2_controllers_yaml,
            moveit_controllers=self.moveit_controllers_yaml,
            kinematics=self.moveit_nodes_kinematics,
            joint_limits=self.moveit_joint_limits,
        )
        self.moveit_nodes[str(node["id"])] = "package_moveit"


    def create_launch_description(self) -> LaunchDescription:
        """
        创建launch描述，包含robot_state_publisher和move_group节点

        Args:
            urdf_str: URDF文本

        Returns:
            LaunchDescription: launch描述对象
        """
        # 检查ROS 2环境变量
        if "AMENT_PREFIX_PATH" not in os.environ:
            raise OSError(
                "ROS 2环境未正确设置。需要设置 AMENT_PREFIX_PATH 环境变量。\n"
                "请确保：\n"
                "1. 已安装ROS 2 (推荐使用 ros-humble-desktop-full)\n"
                "2. 已激活Conda环境: conda activate unilab\n"
                "3. 或手动source ROS 2 setup文件: source /opt/ros/humble/setup.bash\n"
                "4. 或者使用 --backend simple 参数跳过ROS依赖"
            )

        try:
            moveit_configs_utils_path = Path(get_package_share_directory("moveit_configs_utils"))
        except Exception as e:
            raise OSError(
                f"无法找到moveit_configs_utils包。请确保ROS 2和MoveIt 2已正确安装。\n"
                f"原始错误: {e}"
            )
        default_folder = moveit_configs_utils_path / "default_configs"
        planning_pattern = re.compile("^(.*)_planning.yaml$")
        pipelines = []

        for pipeline in get_pattern_matches(default_folder, planning_pattern):
            if pipeline not in pipelines:
                pipelines.append(pipeline)

        if "ompl" in pipelines:
            default_planning_pipeline = "ompl"
        else:
            default_planning_pipeline = pipelines[0]

        planning_pipelines = {
            "planning_pipelines": pipelines,
            "default_planning_pipeline": default_planning_pipeline,
        }

        for pipeline in pipelines:
            planning_pipelines[pipeline] = load_yaml(
                default_folder /  f"{pipeline}_planning.yaml"
            )

        if "ompl" in planning_pipelines:
            ompl_config = planning_pipelines["ompl"]
            if "planner_configs" not in ompl_config:
                ompl_config.update(load_yaml(default_folder / "ompl_defaults.yaml"))

        controllers_path = self.runtime_dir / "ros2_controllers.yaml"
        staged_controllers_path = self.runtime_dir / "ros2_controllers.yaml.tmp"
        staged_controllers_path.write_text(
            yaml.safe_dump(self.ros2_controllers_yaml, sort_keys=False),
            encoding="utf-8",
        )
        staged_controllers_path.replace(controllers_path)

        robot_description_planning = {
            "default_velocity_scaling_factor": 0.1,
            "default_acceleration_scaling_factor": 0.1,
            "joint_limits": self.moveit_joint_limits["joint_limits"],
            "cartesian_limits": {
            "max_trans_vel": 1.0,
            "max_trans_acc": 2.25,
            "max_trans_dec": -5.0,
            "max_rot_vel": 1.57
            }
        }
        # 解析URDF文件
        robot_description = self.urdf_str
        urdf_str_srdf = self.urdf_str_srdf

        kinematics_dict = self.moveit_nodes_kinematics


        if self.moveit_nodes:

            controllers = []
            use_builtin_simulation = should_use_builtin_simulation_controller(
                platform_name=sys.platform,
                moveit_device_ids=tuple(self.moveit_nodes),
                simulated_moveit_device_ids=self.simulated_moveit_device_ids,
            )
            if use_builtin_simulation:
                specs_path = self.runtime_dir / "simulation_controllers.json"
                specs_path.write_text(
                    json.dumps(
                        {
                            "controllers": simulation_controller_specs(
                                self.moveit_controllers_yaml
                            )
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                controllers.append(
                    ExecuteProcess(
                        cmd=[
                            sys.executable,
                            str(
                                Path(__file__).with_name(
                                    "simulated_trajectory_controller.py"
                                )
                            ),
                            "--config",
                            str(specs_path),
                        ],
                        output="screen",
                        additional_env=dict(os.environ),
                    )
                )
            else:
                ros2_controllers = ParameterFile(
                    str(controllers_path), allow_substs=True
                )
                controllers.append(
                    nd(
                        package="controller_manager",
                        executable="ros2_control_node",
                        output='screen',
                        parameters=[
                            {"robot_description": robot_description},
                            ros2_controllers,
                        ],
                        env=dict(os.environ)
                    )
                )
                spawn_order = controller_spawn_order(
                    self.moveit_controllers_yaml[
                        "moveit_simple_controller_manager"
                    ]["controller_names"]
                )
                spawners = [
                    nd(
                        package="controller_manager",
                        executable="spawner",
                        arguments=[
                            name,
                            "--controller-manager",
                            "controller_manager",
                        ],
                        output="screen",
                        env=dict(os.environ),
                    )
                    for name in spawn_order
                ]
                if spawners:
                    controllers.append(spawners[0])
                    for current, following, following_name in zip(
                        spawners[:-1],
                        spawners[1:],
                        spawn_order[1:],
                        strict=True,
                    ):
                        controllers.append(
                            RegisterEventHandler(
                                OnProcessExit(
                                    target_action=current,
                                    on_exit=_start_next_controller_on_success(
                                        following,
                                        following_name,
                                    ),
                                )
                            )
                        )
            for i in controllers:
                self.launch_description.add_action(i)
        else:
            ros2_controllers = None

        # 创建robot_state_publisher节点
        robot_state_publisher = nd(
            package='robot_state_publisher',
            executable='robot_state_publisher',
            name='robot_state_publisher',
            output='screen',
            parameters=[{
                'robot_description': robot_description,
                'use_sim_time': False
            },
            # kinematics_dict
            ],
            env=dict(os.environ)
        )


        # 创建move_group节点
        moveit_params =[{
            'allow_trajectory_execution': True,
            'robot_description': robot_description,
            'robot_description_semantic': urdf_str_srdf,
            'robot_description_kinematics': kinematics_dict,
            'capabilities': '',
            'disable_capabilities': '',
            'monitor_dynamics': False,
            'publish_monitored_planning_scene': True,
            'publish_robot_description_semantic': True,
            'publish_planning_scene': True,
            'publish_geometry_updates': True,
            'publish_state_updates': True,
            'publish_transforms_updates': True,
            # 'robot_description_planning': robot_description_planning,
            },
            robot_description_planning,
            planning_pipelines,
            ]
        if self.moveit_controllers_yaml['moveit_simple_controller_manager']['controller_names']:
            moveit_params.append(self.moveit_controllers_yaml)

        # 将节点添加到launch描述中
        self.launch_description.add_action(robot_state_publisher)
        # self.launch_description.add_action(joint_state_publisher_node)
        if self.moveit_nodes:
            move_group = nd(
                package='moveit_ros_move_group',
                executable='move_group',
                output='screen',
                parameters=moveit_params,
                env=dict(os.environ)
            )
            self.launch_description.add_action(move_group)

        # 如果启用RViz,添加RViz节点
        if self.enable_rviz:
            rviz_node = nd(
                package='rviz2',
                executable='rviz2',
                name='rviz2',
                arguments=['-d', f"{str(self.mesh_path)}/view_robot.rviz"],
                output='screen',
                parameters=[
                    {'robot_description_kinematics': kinematics_dict,
                     },
                    robot_description_planning,
                    planning_pipelines,

                ],
                env=dict(os.environ)
            )
            self.launch_description.add_action(rviz_node)

        return self.launch_description

    def prepare(self) -> LaunchDescription:
        """在启动设备前完整构造并校验 ROS Launch 描述。

        参数：无。返回：可交给本实例 ``LaunchService`` 的唯一描述。异常：ROS、
        MoveIt 或模型资产不完整时传播原异常。安全：准备失败不会启动任何设备或
        ROS 子进程，调用方可据此对必需运动运行时执行关闭失败。
        """

        if not self._launch_prepared:
            self.create_launch_description()
            self._launch_prepared = True
        return self.launch_description

    def start(self) -> None:
        """
        启动资源 ROS Launch 服务；RViz 是否存在由独立开关决定。

        Args:
            urdf_str: URDF文件路径
        """
        launch_description = self.prepare()
        # print('--------------------------------')
        # print(self.moveit_controllers_yaml)
        # print('--------------------------------')
        # print(self.urdf_str)
        # print('--------------------------------')
        self.launch_service.include_launch_description(launch_description)
        self.launch_service.run()

    def stop(self) -> None:
        """停止本实例拥有的 LaunchService 并清理临时控制器配置。

        参数：无。返回：无。异常：LaunchService 的关闭异常向上传播。安全：只
        删除本实例由 ``tempfile`` 创建的精确目录，不触碰仓库内模型资产。
        """

        shutdown = getattr(self.launch_service, "shutdown", None)
        if callable(shutdown):
            shutdown()
        if self._runtime_finalizer.alive:
            self._runtime_finalizer()
