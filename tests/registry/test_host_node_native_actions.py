from unilabos.registry.registry import _native_empty_in_action
from unilabos_msgs.action import EmptyIn


def test_native_empty_in_action_preserves_business_schema() -> None:
    original = {
        "type": "UniLabJsonCommand",
        "goal": {"unexpected": "unexpected"},
        "feedback": {"unexpected": "unexpected"},
        "result": {"unexpected": "unexpected"},
        "goal_default": {"unexpected": 1},
        "schema": {
            "properties": {
                "result": {
                    "properties": {
                        "status": {"type": "string"},
                    }
                }
            }
        },
        "feedback_interval": 1.0,
    }

    configured = _native_empty_in_action(original)

    assert configured["type"] is EmptyIn
    assert configured["goal"] == {}
    assert configured["feedback"] == {}
    assert configured["result"] == {}
    assert configured["goal_default"] == {}
    assert configured["schema"] == original["schema"]
    assert configured["feedback_interval"] == 1.0
    assert original["type"] == "UniLabJsonCommand"
