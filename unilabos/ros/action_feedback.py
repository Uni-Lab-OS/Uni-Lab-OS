"""设备动作向工作流作业反馈的轻量上下文通道。"""

from __future__ import annotations

import contextvars
import json
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from unilabos.utils.log import logger

ACTION_FEEDBACK_TOPIC = "/unilabos/action_feedback"
ACTION_FEEDBACK_LOG_MARKER = "[UNILAB-ACTION-FEEDBACK]"

ActionFeedbackPublisher = Callable[[dict[str, Any]], bool]


@dataclass
class _ActionFeedbackContext:
    publish: ActionFeedbackPublisher
    identity: dict[str, str]
    sequence: int = 0
    last_change_key: str = ""
    last_publish_monotonic: float = 0.0


_CURRENT_ACTION_FEEDBACK: contextvars.ContextVar[_ActionFeedbackContext | None] = (
    contextvars.ContextVar("unilabos_action_feedback", default=None)
)


@contextmanager
def attach_action_feedback(
    publisher: ActionFeedbackPublisher,
    *,
    job_uuid: str = "",
    task_uuid: str = "",
    device_id: str = "",
    action_name: str = "",
) -> Iterator[None]:
    """把当前驱动调用绑定到对应的工作流作业反馈出口。"""

    token = _CURRENT_ACTION_FEEDBACK.set(
        _ActionFeedbackContext(
            publish=publisher,
            identity={
                "job_uuid": job_uuid,
                "task_uuid": task_uuid,
                "device_id": device_id,
                "action_name": action_name,
            },
        )
    )
    try:
        yield
    finally:
        _CURRENT_ACTION_FEEDBACK.reset(token)


def publish_action_feedback(
    phase: str,
    data: Mapping[str, Any] | None = None,
    *,
    force: bool = False,
    heartbeat_interval_s: float = 5.0,
) -> bool:
    """发布一次有界结构化反馈；数值计时变化只按心跳频率上报。"""

    context = _CURRENT_ACTION_FEEDBACK.get()
    if context is None or not isinstance(phase, str) or not phase.strip():
        return False
    details = dict(data or {})
    semantic = {
        key: value
        for key, value in details.items()
        if key not in {"elapsed_s", "remaining_s", "observed_at"}
    }
    change_key = json.dumps(
        {"phase": phase.strip(), "data": semantic},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    now = time.monotonic()
    heartbeat_due = (
        context.last_publish_monotonic <= 0
        or now - context.last_publish_monotonic >= max(0.1, heartbeat_interval_s)
    )
    if not force and change_key == context.last_change_key and not heartbeat_due:
        return False

    context.sequence += 1
    identity = context.identity
    feedback_event_id = f"{identity['job_uuid'] or 'standalone'}:{context.sequence}"
    payload: dict[str, Any] = {
        "phase": phase.strip(),
        "feedback_event_id": feedback_event_id,
        "feedback_sequence": context.sequence,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "task_uuid": identity["task_uuid"],
        "job_uuid": identity["job_uuid"],
        "goal": {
            "device_id": identity["device_id"],
            "action_name": identity["action_name"],
        },
        "effect": {
            "identity": feedback_event_id,
            "phase": phase.strip(),
        },
        **details,
    }
    if not context.publish(payload):
        return False
    logger.info(
        f"{ACTION_FEEDBACK_LOG_MARKER} "
        f"{json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)}"
    )
    context.last_change_key = change_key
    context.last_publish_monotonic = now
    return True


def decode_action_feedback(value: Mapping[str, Any]) -> dict[str, Any]:
    """把 ``StrSingleInput`` 的 JSON 字符串反馈还原为结构化对象。"""

    raw = value.get("feedback")
    if not isinstance(raw, str):
        return dict(value)
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return dict(value)
    return decoded if isinstance(decoded, dict) else dict(value)


__all__ = [
    "ACTION_FEEDBACK_LOG_MARKER",
    "ACTION_FEEDBACK_TOPIC",
    "attach_action_feedback",
    "decode_action_feedback",
    "publish_action_feedback",
]
