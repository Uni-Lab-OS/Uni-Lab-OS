from unilabos.ros.nodes.base_device_node import _action_return_failure


def test_explicit_success_false_is_an_action_failure() -> None:
    assert (
        _action_return_failure(
            {
                "success": False,
                "error": "device rejected command",
            }
        )
        == "device rejected command"
    )


def test_latency_all_timeout_is_an_action_failure() -> None:
    assert (
        _action_return_failure(
            {
                "status": "all_timeout",
                "avg_rtt_ms": -1.0,
                "test_count": 0,
            }
        )
        == "动作返回失败状态: all_timeout"
    )


def test_successful_structured_result_remains_successful() -> None:
    assert _action_return_failure({"success": True, "status": "success"}) == ""
    assert _action_return_failure({"status": "idle", "value": 0}) == ""
