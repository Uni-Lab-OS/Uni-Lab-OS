from unittest.mock import Mock, patch

from unilabos.resources.resource_tracker import ResourceDictInstance
from unilabos.ros.utils.driver_creator import DeviceClassCreator


def _child(*, logical_mount: bool) -> ResourceDictInstance:
    return ResourceDictInstance.get_resource_instance_from_dict(
        {
            "id": "logical-warehouse",
            "uuid": "logical-warehouse-uuid",
            "name": "Logical warehouse",
            "type": "warehouse",
            "class": "community.example.logical_warehouse",
            "position": {"x": 0, "y": 0, "z": 0},
            "config": {"logical_mount": logical_mount},
            "data": {},
            "extra": {},
        }
    )


def test_device_creator_does_not_convert_logical_inventory_mount_to_plr() -> None:
    tracker = Mock()
    creator = DeviceClassCreator(object, [_child(logical_mount=True)], tracker)
    creator.device_instance = object()

    with patch(
        "unilabos.ros.utils.driver_creator.ResourceTreeSet.to_plr_resources"
    ) as to_plr_resources:
        creator.attach_resource()

    to_plr_resources.assert_not_called()
    tracker.add_resource.assert_not_called()


def test_device_creator_still_attaches_physical_child_resources() -> None:
    tracker = Mock()
    creator = DeviceClassCreator(object, [_child(logical_mount=False)], tracker)
    creator.device_instance = object()
    physical_resource = object()

    with patch(
        "unilabos.ros.utils.driver_creator.ResourceTreeSet.to_plr_resources",
        return_value=[physical_resource],
    ) as to_plr_resources:
        creator.attach_resource()

    to_plr_resources.assert_called_once()
    tracker.add_resource.assert_called_once_with(physical_resource)
