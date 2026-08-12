from dataclasses import dataclass, field

from unilabos.resources.resource_tracker import DeviceNodeResourceTracker


@dataclass
class FakeResource:
    name: str
    unilabos_uuid: str
    children: list["FakeResource"] = field(default_factory=list)


def test_figure_resource_uses_authoritative_uuid_index_for_non_root_resource():
    tracker = DeviceNodeResourceTracker()
    resource = FakeResource("beaker", "stable-beaker-uuid")
    tracker.uuid_to_resources[resource.unilabos_uuid] = resource

    assert tracker.figure_resource(
        {"uuid": resource.unilabos_uuid}, try_mode=True
    ) == [resource]
    assert tracker.figure_resource({"uuid": resource.unilabos_uuid}) is resource


def test_figure_resource_does_not_return_unknown_uuid_from_index():
    tracker = DeviceNodeResourceTracker()

    assert tracker.figure_resource({"uuid": "missing"}, try_mode=True) == []
