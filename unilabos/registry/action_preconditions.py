"""设备动作前置条件（ActionPrecondition）合同归一化。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def normalize_action_preconditions(value: Any) -> list[dict[str, Any]]:
    """校验并分离动作声明中的 fail-fast 设备状态前置条件。"""

    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError("preconditions 必须是字典列表")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            raise TypeError("preconditions 的每一项必须是字典")
        condition_id = raw.get("id")
        parameter = raw.get("parameter")
        properties = raw.get("properties")
        sensors = raw.get("sensors", {})
        policy = raw.get("policy", "fail_fast")
        max_age_seconds = raw.get("max_age_seconds", 5.0)
        message = raw.get("message", "设备动作前置条件不满足")
        if not isinstance(condition_id, str) or not condition_id.strip():
            raise ValueError("preconditions.id 必须是非空字符串")
        if condition_id in seen:
            raise ValueError(f"preconditions.id 重复: {condition_id}")
        if not isinstance(parameter, str) or not parameter.strip():
            raise ValueError("preconditions.parameter 必须是非空字符串")
        if not isinstance(properties, Mapping) or not properties:
            raise ValueError("preconditions.properties 必须是非空字典")
        if not isinstance(sensors, Mapping):
            raise TypeError("preconditions.sensors 必须是字典")
        if policy != "fail_fast":
            raise ValueError("当前只支持 preconditions.policy=fail_fast")
        if (
            isinstance(max_age_seconds, bool)
            or not isinstance(max_age_seconds, (int, float))
            or max_age_seconds <= 0
        ):
            raise ValueError("preconditions.max_age_seconds 必须大于 0")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("preconditions.message 必须是非空字符串")
        normalized_properties = _string_map(properties, "properties")
        normalized_sensors = _string_map(sensors, "sensors")
        if set(normalized_sensors) - set(normalized_properties):
            raise ValueError("preconditions.sensors 不得声明未知参数值")
        expected = raw.get("expected", True)
        if not isinstance(expected, (bool, int, float, str)):
            raise TypeError("preconditions.expected 必须是标量")
        seen.add(condition_id)
        normalized.append(
            {
                "id": condition_id,
                "parameter": parameter,
                "properties": normalized_properties,
                "sensors": normalized_sensors,
                "expected": expected,
                "policy": policy,
                "max_age_seconds": float(max_age_seconds),
                "message": message,
            }
        )
    return normalized


def _string_map(value: Mapping[Any, Any], field: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, item in value.items():
        normalized_key = str(key)
        if not normalized_key or not isinstance(item, str) or not item.strip():
            raise ValueError(f"preconditions.{field} 必须映射到非空字符串")
        result[normalized_key] = item
    return result


__all__ = ["normalize_action_preconditions"]
