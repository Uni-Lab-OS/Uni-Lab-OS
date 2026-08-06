from typing import Optional

from unilabos.registry.init_enforce import merge_init_param_enforce
from unilabos.registry.registry import lab_registry
from unilabos.ros.device_node_wrapper import ros2_device_node
from unilabos.ros.nodes.base_device_node import ROS2DeviceNode, DeviceInitError
from unilabos.resources.resource_tracker import ResourceDictInstance
from unilabos.utils import logger
from unilabos.utils.exception import DeviceClassInvalid
from unilabos.utils.import_manager import default_manager


def initialize_device_from_dict(device_id, device_config: ResourceDictInstance) -> Optional[ROS2DeviceNode]:
    """根据物理图设备配置解析并初始化唯一选中的设备执行器。

    参数：``device_id`` 是运行时设备实例身份；``device_config`` 是携带设备
    定义身份、稳定 UUID 和初始化配置的资源实例。
    返回：成功包装并初始化的 ROS2 设备节点；驱动抛出 ``DeviceInitError`` 时
    返回当前空结果，保留既有启动兼容语义。
    异常：设备定义为空、缺失、歧义或不是字符串时抛出 ``DeviceClassInvalid``；
    驱动模块导入与其他初始化异常原样传播。设备定义统一经实时注册表
    （Registry）解析，发布目录本身不会提前导入未选中的作者模块。
    """
    initialized_device = None
    # ``device_definition_identity`` 是物理图选择的规范 FQID 或唯一兼容短身份。
    device_definition_identity = device_config.res_content.klass
    registry_entry = {}
    # ``runtime_device_uuid`` 是设备实例的稳定身份，不是设备模板身份。
    runtime_device_uuid = device_config.res_content.uuid
    if isinstance(device_definition_identity, str):
        if len(device_definition_identity) == 0:
            raise DeviceClassInvalid(f"Device [{device_id}] class cannot be an empty string. {device_config}")
        try:
            registry_entry = lab_registry.resolve_definition(
                "device",
                device_definition_identity,
            )
        except (KeyError, ValueError, RuntimeError) as error:
            raise DeviceClassInvalid(
                f"Device [{device_id}] class {device_definition_identity} invalid: {error}. "
                f"{device_config}"
            ) from error
        device_class_config = registry_entry["class"]
    elif isinstance(device_definition_identity, dict):
        raise DeviceClassInvalid(
            f"Device [{device_id}] class config should be type 'str' but 'dict' got. {device_config}"
        )
    else:
        device_class_config = device_definition_identity
    if isinstance(device_class_config, dict):
        DEVICE = default_manager.get_class(device_class_config["module"])
        # 不管是ros2的实例，还是python的，都必须包一次，除了HostNode
        DEVICE = ros2_device_node(
            DEVICE,
            status_types=device_class_config.get("status_types", {}),
            device_config=device_config,
            action_value_mappings=device_class_config.get("action_value_mappings", {}),
            hardware_interface=device_class_config.get(
                "hardware_interface",
                {"name": "hardware_interface", "write": "send_command", "read": "read_data", "extra_info": []},
            )
        )
        try:
            runtime_config = device_config.res_content.config
            if not isinstance(runtime_config, dict):
                runtime_config = {}
            driver_params = merge_init_param_enforce(
                runtime_config,
                registry_entry.get("init_param_enforce"),
            )
            initialized_device = DEVICE(
                device_id=device_id,
                device_uuid=runtime_device_uuid,
                driver_is_ros=device_class_config["type"] == "ros2",
                driver_params=driver_params,
            )
        except DeviceInitError:
            return initialized_device
    else:
        logger.warning(f"initialize device {device_id} failed, provided device_config: {device_config}")
    return initialized_device
