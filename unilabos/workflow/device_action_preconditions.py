"""设备单动作调试（D1A）派发前的设备状态前置条件评估。"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Protocol


class DeviceActionPreconditionState(Protocol):
    """设备状态权威的最小只读端口。"""

    def latest_for(self, device_id: str) -> dict[str, dict[str, Any]]: ...


class DeviceActionPreconditionFailure(RuntimeError):
    """结构化前置条件失败；调用方决定如何投影到 HTTP。"""

    def __init__(self, details: dict[str, Any]):
        super().__init__(str(details["message"]))
        self.details = details


def evaluate_device_action_preconditions(
    *,
    state: DeviceActionPreconditionState | None,
    device_id: str,
    input_value: Mapping[str, Any],
    conditions: Sequence[Mapping[str, Any]],
    now_ms: int | None = None,
) -> None:
    """按合同逐项检查；任何未知、过期或不匹配均 fail closed。"""

    if not conditions:
        return
    checked_at_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    latest = state.latest_for(device_id) if state is not None else {}
    for condition in conditions:
        parameter = str(condition["parameter"])
        parameter_value = input_value.get(parameter)
        selector = str(parameter_value)
        properties = condition["properties"]
        property_name = properties.get(selector)
        sensor_name = condition.get("sensors", {}).get(selector)
        observation = latest.get(property_name) if property_name else None
        expected = condition.get("expected", True)
        actual = observation.get("value") if observation else None
        observed_at_ms = observation.get("updated_at") if observation else None
        max_age_ms = int(float(condition["max_age_seconds"]) * 1000)
        age_ms = (
            checked_at_ms - int(observed_at_ms)
            if isinstance(observed_at_ms, int)
            else None
        )
        if property_name is None:
            reason = "unknown_parameter_value"
        elif observation is None:
            reason = "unknown"
        elif age_ms is None or age_ms < 0 or age_ms > max_age_ms:
            reason = "stale"
        elif actual != expected:
            reason = "not_met"
        else:
            continue
        context = {
            "parameter": parameter,
            "value": parameter_value,
            "position": parameter_value,
            "sensor": sensor_name or property_name or "",
        }
        try:
            message = str(condition["message"]).format(**context)
        except (KeyError, ValueError):
            message = str(condition["message"])
        raise DeviceActionPreconditionFailure(
            {
                "precondition_id": str(condition["id"]),
                "reason": reason,
                "message": message,
                "device_id": device_id,
                "parameter": {"name": parameter, "value": parameter_value},
                "property": property_name,
                "sensor": sensor_name,
                "expected": expected,
                "actual": actual,
                "checked_at": datetime.fromtimestamp(
                    checked_at_ms / 1000, tz=timezone.utc
                ).isoformat(),
                "checked_at_ms": checked_at_ms,
                "observed_at_ms": observed_at_ms,
                "age_ms": age_ms,
                "max_age_seconds": float(condition["max_age_seconds"]),
                "timeout_policy": {
                    "mode": "fail_fast",
                    "timeout_seconds": 0,
                },
            }
        )


__all__ = [
    "DeviceActionPreconditionFailure",
    "DeviceActionPreconditionState",
    "evaluate_device_action_preconditions",
]
