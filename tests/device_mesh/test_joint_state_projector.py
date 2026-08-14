"""Graph Device 关节反馈投影的时序、隔离和失效合同。"""

from __future__ import annotations

import pytest

from unilabos.device_mesh.joint_state_projector import (
    JointStateOwner,
    JointStateProjector,
)


def _owner(device_id: str) -> JointStateOwner:
    return JointStateOwner(
        device_id=device_id,
        topology_digest=("a" if device_id == "robot_a" else "b") * 64,
        qualified_joint_names=tuple(
            f"{device_id}_cr5_joint_{index}" for index in range(1, 7)
        ),
        stale_after_s=1.0,
    )


def test_two_same_model_instances_never_cross_streams() -> None:
    """同型号两实例必须由完整关节名精确隔离。"""

    projector = JointStateProjector(
        (_owner("robot_a"), _owner("robot_b")),
        boot_id="edge-test",
    )
    names = _owner("robot_a").qualified_joint_names

    assert projector.publish_joint_state(names, (0.1,) * 6, observed_at=10.0)
    frames = projector.drain(now=10.01)

    assert [frame["data"]["device_id"] for frame in frames] == ["robot_a"]
    assert set(frames[0]["data"]["joint_states"]) == set(names)
    assert all(not name.startswith("robot_b_") for name in names)


def test_projector_rejects_duplicate_ownership_and_incomplete_or_stale_frames() -> None:
    """重名归属关闭失败，不完整/过期反馈不对外发布。"""

    owner = _owner("robot_a")
    with pytest.raises(ValueError, match="device_id 完全限定"):
        JointStateOwner(
            device_id="robot_b",
            topology_digest="b" * 64,
            qualified_joint_names=owner.qualified_joint_names,
        )

    projector = JointStateProjector((owner,), boot_id="edge-test")
    assert projector.publish_joint_state(
        owner.qualified_joint_names[:2],
        (0.0, 0.0),
        observed_at=10.0,
    )
    assert projector.drain(now=10.1) == []
    assert projector.publish_joint_state(
        owner.qualified_joint_names[2:],
        (0.0,) * 4,
        observed_at=10.0,
    )
    assert projector.drain(now=11.1) == []


def test_projector_is_monotonic_and_rate_limited() -> None:
    """boot/sequence 可识别重连代际，旧帧和过频帧不外发。"""

    owner = _owner("robot_a")
    projector = JointStateProjector((owner,), boot_id="edge-test")
    assert projector.publish_joint_state(
        owner.qualified_joint_names,
        (0.0,) * 6,
        observed_at=20.0,
    )
    first = projector.drain(now=20.0)[0]
    assert first["data"]["boot_id"] == "edge-test"
    assert first["data"]["sequence"] == 1

    assert projector.publish_joint_state(
        owner.qualified_joint_names,
        (1.0,) * 6,
        observed_at=20.01,
    )
    assert projector.drain(now=20.02) == []
    second = projector.drain(now=20.06)[0]
    assert second["data"]["sequence"] == 2
    assert set(second["data"]["joint_states"].values()) == {1.0}

    assert not projector.publish_joint_state(
        (owner.qualified_joint_names[0],) * 2,
        (2.0, 2.0),
        observed_at=21.0,
    )
