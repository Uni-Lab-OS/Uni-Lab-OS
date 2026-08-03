from pylabrobot.resources import Resource

from unilabos.resources.resource_tracker import ResourceTreeSet


def test_logical_warehouse_deserializes_as_generic_resource():
    tree = ResourceTreeSet.from_raw_dict_list(
        [
            {
                "id": "powder_container_warehouse",
                "uuid": "warehouse-uuid",
                "name": "固体粉桶堆栈",
                "parent_uuid": None,
                "type": "warehouse",
                "class": "community.example.powder_warehouse",
                "position": {"x": 10, "y": 20, "z": 30},
                "config": {
                    "size_x": 100,
                    "size_y": 370,
                    "size_z": 531,
                    "category": "powder_stack",
                    "sites": [{"name": "L1C1"}],
                    "num_items_x": 1,
                },
                "data": {},
            }
        ]
    )

    resource = tree.to_plr_resources()[0]

    assert type(resource) is Resource
    assert resource.name == "固体粉桶堆栈"
    assert resource.category == "powder_stack"
    assert resource.unilabos_uuid == "warehouse-uuid"
