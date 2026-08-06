"""把运行时设备反馈幂等追加到工作流作业日志。"""

from __future__ import annotations

import hashlib
from typing import Any

from unilabos.workflow.json_codec import decode_json_bytes, encode_json
from unilabos.workflow.store import StoreConflict, utc_now


def _fingerprint(value: dict[str, Any]) -> str:
    return hashlib.sha256(encode_json(value, sort_keys=True)).hexdigest()


def _idempotency_key(
    *,
    source: str,
    job_uuid: str,
    feedback_data: dict[str, Any],
) -> str:
    event_id = feedback_data.get("feedback_event_id")
    identity = (
        event_id.strip()
        if isinstance(event_id, str) and event_id.strip()
        else _fingerprint(feedback_data)
    )
    return f"{source}:{job_uuid}:{identity}"


def _existing_feedback(coordinator: Any, key: str) -> dict[str, Any] | None:
    with coordinator._store.transaction() as connection:
        row = connection.execute(
            """
            SELECT sequence, feedback_type, data, observed_at, idempotency_key
            FROM workflow_node_job_feedback_history
            WHERE idempotency_key = ? AND deleted_at IS NULL
            """,
            (key,),
        ).fetchone()
    if row is None:
        return None
    return {
        "sequence": int(row["sequence"]),
        "feedback_type": row["feedback_type"],
        "data": decode_json_bytes(row["data"].encode("utf-8")),
        "observed_at": row["observed_at"],
        "idempotency_key": row["idempotency_key"],
    }


def commit_runtime_job_feedback(
    coordinator: Any,
    *,
    source: str,
    job_uuid: str,
    feedback_data: dict[str, Any],
) -> dict[str, int]:
    """追加一次反馈；同一设备事件重放时不生成新序号。"""

    key = _idempotency_key(
        source=source,
        job_uuid=job_uuid,
        feedback_data=feedback_data,
    )
    existing = _existing_feedback(coordinator, key)
    if existing is not None:
        if existing["data"] != feedback_data:
            raise StoreConflict("runtime feedback identity has other content")
        return {"through_sequence": existing["sequence"], "created": 0}

    current = coordinator._execution_job(job_uuid)
    sample = {
        "sequence": int(current["feedback_sequence"]) + 1,
        "feedback_type": "action_phase",
        "data": feedback_data,
        "observed_at": feedback_data.get("observed_at") or utc_now(),
        "idempotency_key": key,
    }
    try:
        return coordinator.commit_job_feedback(job_uuid, [sample])
    except StoreConflict:
        replayed = _existing_feedback(coordinator, key)
        if replayed is not None and replayed["data"] == feedback_data:
            return {"through_sequence": replayed["sequence"], "created": 0}
        raise


__all__ = ["commit_runtime_job_feedback"]
