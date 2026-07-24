"""Quick Debug Alpha: v1 ``@action`` metadata compatibility contract."""

from __future__ import annotations

from unilabos.registry.decorators import action, get_action_meta


def test_v1_action_without_contract_keeps_exact_metadata_shape() -> None:
    """Adding the v2 contract must not add or mutate any v1 metadata field."""

    @action(
        goal={"volume": "aspirate_volume"},
        feedback={"progress": "progress"},
        result={"transferred": "actual_volume"},
        goal_default={"volume": 10.0},
        placeholder_keys={"source": "source_id"},
        always_free=True,
        is_protocol=True,
        description="legacy transfer",
        auto_prefix=True,
        parent=True,
        feedback_interval=0.5,
    )
    def legacy_transfer(volume: float) -> float:
        return volume

    assert get_action_meta(legacy_transfer) == {
        "action_type": None,
        "goal": {"volume": "aspirate_volume"},
        "feedback": {"progress": "progress"},
        "result": {"transferred": "actual_volume"},
        "handles": {},
        "goal_default": {"volume": 10.0},
        "placeholder_keys": {"source": "source_id"},
        "always_free": True,
        "is_protocol": True,
        "description": "legacy transfer",
        "auto_prefix": True,
        "parent": True,
        "feedback_interval": 0.5,
    }


def test_v1_action_default_metadata_remains_byte_for_byte_serializable() -> None:
    """The no-argument decorator remains the historical UniLabJsonCommand form."""

    @action()
    def ping() -> None:
        return None

    assert get_action_meta(ping) == {
        "action_type": None,
        "goal": {},
        "feedback": {},
        "result": {},
        "handles": {},
        "goal_default": {},
        "placeholder_keys": {},
        "always_free": False,
        "is_protocol": False,
        "description": "",
        "auto_prefix": False,
        "parent": False,
    }
