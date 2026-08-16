"""关节状态投影器（JointStateProjector）的 exact 归属测试。"""

from __future__ import annotations

import uuid

import pytest

from unilabos.device_mesh.joint_state_projector import (
    JointStateOwner,
    JointStateProjector,
)


def _owner(device_id: str, *joints: str) -> JointStateOwner:
    return JointStateOwner(
        device_id=device_id,
        topology_digest=("a" if device_id == "arm_a" else "b") * 64,
        qualified_joint_names=tuple(joints),
        stale_after_s=1.0,
    )


def test_two_instances_do_not_cross_project_joint_values() -> None:
    """相同型号的两个实例只能接收自己完全限定的关节。"""

    projector = JointStateProjector(
        (
            _owner("arm_a", "arm_a_joint_1", "arm_a_joint_2"),
            _owner("arm_b", "arm_b_joint_1", "arm_b_joint_2"),
        ),
        boot_id=str(uuid.uuid4()),
    )
    assert projector.ingest(
        ["arm_b_joint_1", "arm_a_joint_1", "unknown"],
        [1.0, 0.1, 99.0],
        observed_epoch_s=100.0,
    )
    assert projector.drain(now_epoch_s=100.0) == ()
    assert projector.ingest(
        ["arm_a_joint_2", "arm_b_joint_2"],
        [0.2, 2.0],
        observed_epoch_s=100.01,
    )

    frames = projector.drain(now_epoch_s=100.01)

    assert [frame.device_id for frame in frames] == ["arm_a", "arm_b"]
    assert frames[0].joint_states == {
        "arm_a_joint_1": 0.1,
        "arm_a_joint_2": 0.2,
    }
    assert frames[1].joint_states == {
        "arm_b_joint_1": 1.0,
        "arm_b_joint_2": 2.0,
    }


def test_projector_emits_only_complete_fresh_rate_limited_frames() -> None:
    """不完整、过期或限频窗口内的设备帧不得出站。"""

    projector = JointStateProjector(
        (_owner("arm_a", "arm_a_joint_1", "arm_a_joint_2"),),
        max_publish_hz=20.0,
    )
    projector.ingest(["arm_a_joint_1"], [0.1], observed_epoch_s=100.0)
    assert projector.drain(now_epoch_s=100.0) == ()
    projector.ingest(["arm_a_joint_2"], [0.2], observed_epoch_s=100.0)
    first = projector.drain(now_epoch_s=100.0)
    assert len(first) == 1
    assert first[0].sequence == 1

    projector.ingest(["arm_a_joint_1"], [0.3], observed_epoch_s=100.01)
    assert projector.drain(now_epoch_s=100.01) == ()
    second = projector.drain(now_epoch_s=100.051)
    assert len(second) == 1
    assert second[0].sequence == 2

    projector.ingest(["arm_a_joint_2"], [0.4], observed_epoch_s=101.0)
    assert projector.drain(now_epoch_s=102.1) == ()


def test_owner_rejects_unqualified_or_duplicate_identity() -> None:
    """限定前缀和重复设备身份都在启动前关闭失败。"""

    with pytest.raises(ValueError, match="完全限定"):
        _owner("arm_a", "joint_1")
    with pytest.raises(ValueError, match="device_id 不得重复"):
        JointStateProjector(
            (
                _owner("arm_a", "arm_a_joint_1"),
                _owner("arm_a", "arm_a_joint_2"),
            )
        )
