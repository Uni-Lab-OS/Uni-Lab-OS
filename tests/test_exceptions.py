"""设备异常体系单测"""
from unilabos.utils.exception import (
    DeviceException,
    DeviceExceptionCategory,
    DeviceExceptionSeverity,
    UserAction,
    TimeoutException,
    ParameterError,
    ModbusConnectionError,
    OPCUAConnectionError,
    EmergencyStopError,
    PLCStepTimeout,
    SensorError,
    ResourceConflictError,
    TipPickupError,
)


def _alarm_kwargs():
    return dict(
        device_id="dev-1",
        device_uuid="uuid-1",
        action_name="do_something",
        task_id="task-1",
        job_id="job-1",
    )


def test_device_exception_defaults():
    exc = DeviceException("base error")
    assert exc.category == DeviceExceptionCategory.UNKNOWN
    assert exc.severity == DeviceExceptionSeverity.ERROR


def test_subclass_category_severity_overrides():
    assert ModbusConnectionError.category == DeviceExceptionCategory.NETWORK
    assert ModbusConnectionError.severity == DeviceExceptionSeverity.ERROR

    assert EmergencyStopError.category == DeviceExceptionCategory.HARDWARE
    assert EmergencyStopError.severity == DeviceExceptionSeverity.CRITICAL

    assert ParameterError.category == DeviceExceptionCategory.PARAMETER
    assert ParameterError.severity == DeviceExceptionSeverity.WARNING


def test_to_alarm_dict_has_required_fields():
    exc = ModbusConnectionError("connect fail", device_snapshot={"foo": "bar"})
    d = exc.to_alarm_dict(**_alarm_kwargs())
    for key in (
        "device_id", "action_name", "exception_type",
        "suggested_actions", "device_snapshot", "traceback",
        "require_confirmation",
    ):
        assert key in d
    assert d["exception_type"] == "ModbusConnectionError"
    assert d["device_snapshot"] == {"foo": "bar"}
    assert d["require_confirmation"] is True


def test_plc_step_timeout_extra_fields():
    exc = PLCStepTimeout("step stuck", current_step=3, expected_step=4)
    d = exc.to_alarm_dict(**_alarm_kwargs())
    assert d["current_step"] == 3
    assert d["expected_step"] == 4


def test_tip_pickup_error_extra_fields():
    exc = TipPickupError("pickup fail", tip_position="A1", remaining_tips=5)
    d = exc.to_alarm_dict(**_alarm_kwargs())
    assert d["tip_position"] == "A1"
    assert d["remaining_tips"] == 5


def test_user_action_handler_not_serialized():
    async def my_handler(exception, decision):
        return None

    exc = TipPickupError(
        "fail",
        tip_position="A1",
        remaining_tips=1,
        suggested_actions=[
            UserAction("use_next_tip", "下一个", "切换", handler=my_handler),
            UserAction("abort", "终止"),
        ],
    )
    d = exc.to_alarm_dict(**_alarm_kwargs())
    for item in d["suggested_actions"]:
        assert set(item.keys()) == {"action", "label", "description"}
        assert "handler" not in item


def test_default_suggested_actions_non_empty():
    for cls in (
        DeviceException, TimeoutException, ParameterError,
        ModbusConnectionError, OPCUAConnectionError, EmergencyStopError,
        PLCStepTimeout, SensorError, ResourceConflictError, TipPickupError,
    ):
        if cls is PLCStepTimeout:
            exc = cls("msg", current_step=0, expected_step=1)
        elif cls is TipPickupError:
            exc = cls("msg", tip_position="A1", remaining_tips=0)
        else:
            exc = cls("msg")
        assert len(exc.suggested_actions) > 0
