from typing import Optional

from unilabos.config.config import BasicConfig
from unilabos.package_manager.consumers import resolve_registry_definition
from unilabos.package_manager.device_secrets import resolve_device_configuration
from unilabos.registry.init_enforce import merge_init_param_enforce
from unilabos.registry.registry import lab_registry
from unilabos.resources.resource_tracker import ResourceDictInstance
from unilabos.ros.device_node_wrapper import ros2_device_node
from unilabos.ros.nodes.base_device_node import DeviceInitError, ROS2DeviceNode
from unilabos.utils import logger
from unilabos.utils.exception import DeviceClassInvalid
from unilabos.utils.import_manager import default_manager


def initialize_device_from_dict(
    device_id,
    device_config: ResourceDictInstance,
) -> Optional[ROS2DeviceNode]:
    """按设备图声明加载驱动，并在构造边界解析本地秘密引用。

    参数 ``device_id`` 是当前设备图中的稳定实例 ID，``device_config`` 是保留
    引用形态配置的 Resource 投影。返回完成包装的 ROS2 设备节点；驱动初始化
    失败时沿用既有 ``None`` 语义。秘密只进入短生命周期 ``driver_params``，不会
    回写设备图或 Resource 投影；引用不可解析时失败关闭。
    """
    d = None
    device_class_config = device_config.res_content.klass
    registry_entry = {}
    uid = device_config.res_content.uuid
    if isinstance(device_class_config, str):  # 如果是字符串，则直接去lab_registry中查找，获取class
        if len(device_class_config) == 0:
            raise DeviceClassInvalid(f"Device [{device_id}] class cannot be an empty string. {device_config}")
        try:
            canonical_class, registry_entry = resolve_registry_definition(
                lab_registry.device_type_registry,
                device_class_config,
            )
        except KeyError:
            raise DeviceClassInvalid(
                f"Device [{device_id}] class {device_class_config} not found. {device_config}"
            ) from None
        # Runtime graph state carries the resolved identity after activation;
        # Registry itself remains canonical and does not gain alias records.
        device_config.res_content.klass = canonical_class
        device_class_config = registry_entry["class"]
    elif isinstance(device_class_config, dict):
        raise DeviceClassInvalid(
            f"Device [{device_id}] class config should be type 'str' but 'dict' got. {device_config}"
        )
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
            runtime_config = resolve_device_configuration(
                runtime_config,
                working_dir=BasicConfig.working_dir,
            )
            driver_params = merge_init_param_enforce(
                runtime_config,
                registry_entry.get("init_param_enforce"),
            )
            d = DEVICE(
                device_id=device_id,
                device_uuid=uid,
                driver_is_ros=device_class_config["type"] == "ros2",
                driver_params=driver_params,
            )
        except DeviceInitError:
            return d
    else:
        logger.warning(f"initialize device {device_id} failed, provided device_config: {device_config}")
    return d
