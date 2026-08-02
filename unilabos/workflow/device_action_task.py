"""设备页单 Action 运行的正式 Workflow Task/Job deep module。"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from typing import Any, Protocol
from uuid import uuid4

from unilabos.workflow.catalog import (
    CatalogAuthority,
    TemplateCatalog,
    TemplateCatalogMismatch,
    TemplateCatalogStale,
    TemplateCatalogUnavailable,
)
from unilabos.workflow.json_codec import decode_json_bytes, encode_json
from unilabos.workflow.schema import (
    WorkflowSchemaError,
    parse_input_contract,
    parse_output_contract,
)
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore, utc_now
from unilabos.workflow.task_input import TaskInputError

_ORIGIN_KIND = "system/device-console"


class DeviceActionLiveCatalog(Protocol):
    """HostNode 完成态设备/Action 注册表的只读快照 port。"""

    def snapshot(self) -> dict[str, dict[str, Any]]: ...


class DeviceActionAdmission(Protocol):
    """正式 runtime admission 的最小唤醒 port。"""

    def is_available(self) -> bool: ...

    def wake(self, task_uuid: str, job_uuid: str) -> None: ...


def _json(value: Any) -> str:
    return encode_json(value, sort_keys=True).decode("utf-8")


def _load(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    return decode_json_bytes(value.encode("utf-8"))


def _detached(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _detached(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_detached(item) for item in value]
    return value


def _contains_unsupported_contract(value: Any) -> bool:
    if isinstance(value, Mapping):
        if value.get("$slot") == "ResourceSlot":
            return True
        if value.get("x-unilabos-editor-control") == "site_selector":
            return True
        if value.get("editor_control") in {"material_port", "site_selector"}:
            return True
        if value.get("implicit") is True or value.get("implicit_passthrough") is True:
            return True
        return any(_contains_unsupported_contract(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_unsupported_contract(item) for item in value)
    return False


def _workflow_value_schema(raw: Any) -> dict[str, Any]:
    """把 Action JSON Schema 投影到冻结的 Workflow v1 值词汇。"""

    if not isinstance(raw, Mapping):
        raise WorkflowError("device_action_mismatch")
    if "$slot" in raw:
        raise WorkflowError("unsupported_contract")
    if "anyOf" in raw:
        members = raw.get("anyOf")
        if not isinstance(members, (list, tuple)):
            raise WorkflowError("device_action_mismatch")
        return {"anyOf": [_workflow_value_schema(member) for member in members]}
    kind = raw.get("type")
    if kind == "null":
        return {"type": "null"}
    if kind == "object":
        return {"type": "object"}
    if kind == "array":
        result: dict[str, Any] = {
            "type": "array",
            "items": _workflow_value_schema(raw.get("items")),
        }
        for field in ("minItems", "maxItems"):
            if field in raw:
                result[field] = raw[field]
        return result
    allowed = {
        "string": ("enum", "minLength", "maxLength"),
        "integer": ("enum", "minimum", "maximum"),
        "number": ("enum", "minimum", "maximum"),
        "boolean": ("enum",),
    }
    if kind not in allowed:
        raise WorkflowError("device_action_mismatch")
    result = {"type": kind}
    for field in allowed[kind]:
        if field in raw:
            result[field] = raw[field]
    return result


def _contracts(template: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    raw_schema = template.get("schema")
    if not isinstance(raw_schema, Mapping):
        raise WorkflowError("device_action_mismatch")
    schema = _detached(raw_schema)
    if _contains_unsupported_contract(schema):
        raise WorkflowError("unsupported_contract")
    extension = schema.get("x-unilabos-action-contract")
    properties = schema.get("properties")
    if not isinstance(extension, dict) or not isinstance(properties, dict):
        raise WorkflowError("device_action_mismatch")
    goal = properties.get("goal")
    result = properties.get("result")
    if not isinstance(goal, dict) or not isinstance(result, dict):
        raise WorkflowError("device_action_mismatch")
    goal_properties = goal.get("properties")
    result_properties = result.get("properties")
    input_order = extension.get("input_order")
    output_order = extension.get("output_order")
    required = goal.get("required")
    if (
        not isinstance(goal_properties, dict)
        or not isinstance(result_properties, dict)
        or not isinstance(input_order, list)
        or not isinstance(output_order, list)
        or not isinstance(required, list)
    ):
        raise WorkflowError("device_action_mismatch")

    parameters: list[dict[str, Any]] = []
    for name in input_order:
        if not isinstance(name, str) or name not in goal_properties:
            raise WorkflowError("device_action_mismatch")
        action_schema = goal_properties[name]
        descriptor: dict[str, Any] = {
            "name": name,
            "schema": _workflow_value_schema(action_schema),
            "required": name in required,
        }
        if name not in required:
            if not isinstance(action_schema, dict) or "default" not in action_schema:
                raise WorkflowError("device_action_mismatch")
            descriptor["default"] = action_schema["default"]
        parameters.append(descriptor)

    outputs: list[dict[str, Any]] = []
    for name in output_order:
        if not isinstance(name, str) or name not in result_properties:
            raise WorkflowError("device_action_mismatch")
        outputs.append(
            {
                "name": name,
                "schema": _workflow_value_schema(result_properties[name]),
                "implicit": False,
            }
        )
    try:
        input_contract = parse_input_contract(
            {"version": 1, "parameters": parameters}
        ).to_dict()
        output_contract = parse_output_contract(
            {"version": 1, "outputs": outputs}
        ).to_dict()
    except WorkflowSchemaError:
        raise WorkflowError("device_action_mismatch") from None
    return input_contract, output_contract


class DeviceActionTaskService:
    """原子创建并公开设备页单 Action 的正式 Task/Job。"""

    def __init__(
        self,
        *,
        store: WorkflowStore,
        template_catalog: TemplateCatalog,
        authority: CatalogAuthority,
        live_catalog: DeviceActionLiveCatalog,
        admission: DeviceActionAdmission,
    ) -> None:
        self._store = store
        self._template_catalog = template_catalog
        self._authority = authority
        self._live_catalog = live_catalog
        self._admission = admission
        self._workflow_service = WorkflowService(store)

    def create(
        self,
        *,
        authority_id: str,
        template_catalog_fingerprint: str,
        workflow_node_template_uuid: str,
        device_id: str,
        input_value: dict[str, Any],
        idempotency_key: str,
        description: str | None,
    ) -> dict[str, Any]:
        if not self._admission.is_available():
            raise WorkflowError("admission_unavailable")
        if authority_id != self._authority.authority_id:
            raise WorkflowError("not_found")

        try:
            with self._template_catalog.snapshot(self._authority) as snapshot:
                snapshot.assert_fingerprint(template_catalog_fingerprint)
                template = snapshot.require_node(workflow_node_template_uuid)
                if template.get("type") != "action":
                    raise WorkflowError("unsupported_contract")
                input_contract, output_contract = _contracts(template)
                handles = [
                    item
                    for item in snapshot.handle_templates
                    if item.get("workflow_node_template_uuid")
                    == workflow_node_template_uuid
                ]
                if _contains_unsupported_contract(handles):
                    raise WorkflowError("unsupported_contract")
                self._assert_live_action(template, device_id)
                payload_hash = self._payload_hash(
                    authority_id=authority_id,
                    template_catalog_fingerprint=template_catalog_fingerprint,
                    workflow_node_template_uuid=workflow_node_template_uuid,
                    device_id=device_id,
                    input_value=input_value,
                    description=description,
                )
                replayed = False
                with self._store.transaction() as conn:
                    existing = conn.execute(
                        """
                        SELECT workflow_task_uuid, canonical_payload_hash
                        FROM device_action_task
                        WHERE authority_id = ? AND device_id = ?
                          AND idempotency_key = ?
                        """,
                        (authority_id, device_id, idempotency_key),
                    ).fetchone()
                    if existing is not None:
                        if existing["canonical_payload_hash"] != payload_hash:
                            raise WorkflowError("idempotency_conflict")
                        view = self._view(existing["workflow_task_uuid"], conn=conn)
                        replayed = True
                    else:
                        source = self._ensure_system_source(
                            conn,
                            template=template,
                            handles=handles,
                            input_contract=input_contract,
                            output_contract=output_contract,
                            fingerprint=template_catalog_fingerprint,
                        )
                        graph = self._store.get_graph(
                            source["workflow_uuid"], conn=conn
                        )
                        try:
                            prepared = self._workflow_service._prepare_task_input(
                                graph,
                                run_mode="single_node",
                                target_node_uuid=source["workflow_node_uuid"],
                                input_value=input_value,
                            )
                        except TaskInputError:
                            raise WorkflowError("invalid_input") from None
                        if len(prepared.jobs) != 1:
                            raise WorkflowError("internal_error")
                        task_uuid = str(uuid4())
                        job_uuid = prepared.jobs[0]["uuid"]
                        now = utc_now()
                        self._insert_task_job(
                            conn,
                            task_uuid=task_uuid,
                            job_uuid=job_uuid,
                            source=source,
                            prepared=prepared,
                            description=description,
                            now=now,
                        )
                        conn.execute(
                            """
                            INSERT INTO device_action_task(
                                workflow_task_uuid, workflow_node_job_uuid,
                                authority_id, template_catalog_fingerprint,
                                workflow_node_template_uuid, device_id,
                                action_name, action_display_name,
                                canonical_payload_hash, idempotency_key,
                                admitted_device_id, claim_status,
                                create_time, update_time
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL,
                                      'pending', ?, ?)
                            """,
                            (
                                task_uuid,
                                job_uuid,
                                authority_id,
                                template_catalog_fingerprint,
                                workflow_node_template_uuid,
                                device_id,
                                template["name"],
                                template["display_name"],
                                payload_hash,
                                idempotency_key,
                                now,
                                now,
                            ),
                        )
                        self._store._append_event(
                            conn,
                            event="device_action_task.changed",
                            data={"task_uuid": task_uuid},
                            now=now,
                        )
                        view = self._view(task_uuid, conn=conn)
            if not replayed:
                self._admission.wake(view["task_uuid"], view["job_uuid"])
            return view
        except TemplateCatalogStale:
            raise WorkflowError("template_catalog_conflict") from None
        except TemplateCatalogUnavailable:
            raise WorkflowError("template_catalog_unavailable") from None
        except TemplateCatalogMismatch:
            raise WorkflowError("not_found") from None
        except sqlite3.IntegrityError:
            raise WorkflowError("conflict") from None

    def get(self, task_uuid: str) -> dict[str, Any]:
        try:
            return self._view(task_uuid)
        except (sqlite3.Error, TypeError):
            raise WorkflowError("not_found") from None

    def _assert_live_action(
        self,
        template: Mapping[str, Any],
        device_id: str,
    ) -> None:
        live = self._live_catalog.snapshot()
        device = live.get(device_id) if isinstance(live, dict) else None
        if not isinstance(device, dict):
            raise WorkflowError("not_found")
        actions = device.get("actions")
        action = (
            actions.get(template.get("name")) if isinstance(actions, dict) else None
        )
        if not isinstance(action, dict):
            raise WorkflowError("not_found")
        if (
            device.get("online") is not True
            or device.get("resource_template_uuid")
            != template.get("resource_template_uuid")
            or action.get("type") != template.get("type")
            or encode_json(_detached(action.get("schema")), sort_keys=True)
            != encode_json(_detached(template.get("schema")), sort_keys=True)
        ):
            raise WorkflowError("device_action_mismatch")

    @staticmethod
    def _payload_hash(**payload: Any) -> str:
        return (
            "sha256:" + hashlib.sha256(encode_json(payload, sort_keys=True)).hexdigest()
        )

    def _ensure_system_source(
        self,
        conn: sqlite3.Connection,
        *,
        template: Mapping[str, Any],
        handles: list[Mapping[str, Any]],
        input_contract: dict[str, Any],
        output_contract: dict[str, Any],
        fingerprint: str,
    ) -> dict[str, Any]:
        template_uuid = str(template["uuid"])
        existing = conn.execute(
            """
            SELECT * FROM device_action_system_source
            WHERE authority_id = ? AND workflow_node_template_uuid = ?
            """,
            (self._authority.authority_id, template_uuid),
        ).fetchone()
        if existing is not None:
            return dict(existing)

        target_handles = {
            str(item["data_key"] or item["handle_key"]): item
            for item in handles
            if item.get("io_type") == "target"
        }
        source_handles = {
            str(item["data_key"] or item["handle_key"]): item
            for item in handles
            if item.get("io_type") == "source"
        }
        try:
            input_bindings = {
                str(target_handles[item["name"]]["uuid"]): {"parameter": item["name"]}
                for item in input_contract["parameters"]
            }
            output_bindings = {
                item["name"]: {
                    "kind": "node_output",
                    "workflow_node_uuid": "",
                    "source_handle_uuid": str(source_handles[item["name"]]["uuid"]),
                }
                for item in output_contract["outputs"]
            }
        except KeyError:
            raise WorkflowError("device_action_mismatch") from None

        workflow_uuid = str(uuid4())
        node_uuid = str(uuid4())
        for binding in output_bindings.values():
            binding["workflow_node_uuid"] = node_uuid
        now = utc_now()
        workflow_meta = {
            "unilab": {
                "input_contract": input_contract,
                "output_contract": output_contract,
                "output_bindings": output_bindings,
            }
        }
        node_meta = {"unilab": {"input_bindings": input_bindings}}
        conn.execute(
            """
            INSERT INTO workflow(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, name, tags, revision
            ) VALUES (?, ?, ?, NULL, NULL, ?, ?, '[]', 1)
            """,
            (
                workflow_uuid,
                now,
                now,
                _json(workflow_meta),
                f"Device console: {template['display_name']}",
            ),
        )
        conn.execute(
            """
            INSERT INTO workflow_node(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_uuid, workflow_node_template_uuid,
                parent_uuid, material_uuid, name, status, type, icon, pose,
                param, footer, action_name, action_type, execution_policy,
                disabled, minimized, script
            ) VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, NULL, NULL, ?, 'idle',
                      'device', ?, '{}', '{}', ?, ?, 'action', '{}', 0, 0, NULL)
            """,
            (
                node_uuid,
                now,
                now,
                _json(node_meta),
                workflow_uuid,
                template_uuid,
                template["display_name"],
                template.get("icon"),
                template.get("footer"),
                template["name"],
            ),
        )
        contract_snapshot = {
            "input_contract": input_contract,
            "output_contract": output_contract,
        }
        conn.execute(
            """
            INSERT INTO device_action_system_source(
                authority_id, workflow_node_template_uuid, workflow_uuid,
                workflow_node_uuid, origin_kind, source_revision,
                template_catalog_fingerprint, contract_snapshot,
                create_time, update_time
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            """,
            (
                self._authority.authority_id,
                template_uuid,
                workflow_uuid,
                node_uuid,
                _ORIGIN_KIND,
                fingerprint,
                _json(contract_snapshot),
                now,
                now,
            ),
        )
        return {
            "authority_id": self._authority.authority_id,
            "workflow_node_template_uuid": template_uuid,
            "workflow_uuid": workflow_uuid,
            "workflow_node_uuid": node_uuid,
            "origin_kind": _ORIGIN_KIND,
            "source_revision": 1,
        }

    @staticmethod
    def _insert_task_job(
        conn: sqlite3.Connection,
        *,
        task_uuid: str,
        job_uuid: str,
        source: Mapping[str, Any],
        prepared: Any,
        description: str | None,
        now: str,
    ) -> None:
        job = prepared.jobs[0]
        conn.execute(
            """
            INSERT INTO workflow_task(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_uuid, status, workflow_snapshot,
                execution_plan, run_mode, target_node_uuid, control_status,
                cleanup_status, trace_context, input, output, error_info
            ) VALUES (?, ?, ?, NULL, ?, '{}', ?, 'pending', ?, ?,
                      'single_node', ?, 'active', 'none', '{}', ?, '{}', '[]')
            """,
            (
                task_uuid,
                now,
                now,
                description,
                source["workflow_uuid"],
                _json(prepared.workflow_snapshot),
                _json(prepared.execution_plan),
                source["workflow_node_uuid"],
                _json(prepared.resolved_input),
            ),
        )
        conn.execute(
            """
            INSERT INTO workflow_node_job(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_task_uuid, workflow_node_uuid,
                material_uuid, feedback_sequence, topological_index,
                executor_kind, execution_policy, execution_timeout_seconds,
                status, attempt, param, feedback_data, return_info,
                control_data, error_info
            ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, ?, ?, 0, ?, ?, ?, ?,
                      'pending', 1, ?, '{}', '{}', '{}', '[]')
            """,
            (
                job_uuid,
                now,
                now,
                task_uuid,
                job["workflow_node_uuid"],
                job.get("material_uuid"),
                job["topological_index"],
                job["executor_kind"],
                _json(job.get("execution_policy") or {}),
                int(job.get("execution_timeout_seconds") or 0),
                _json(job.get("param") or {}),
            ),
        )

    def _view(
        self,
        task_uuid: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        database = conn or self._store._conn
        row = database.execute(
            """
            SELECT d.*, t.status, t.control_status, t.cleanup_status,
                   t.input, t.output, t.error_info,
                   t.create_time AS task_create_time,
                   t.update_time AS task_update_time,
                   t.started_at, t.finished_at,
                   j.status AS job_status, j.feedback_sequence
            FROM device_action_task AS d
            JOIN workflow_task AS t ON t.uuid = d.workflow_task_uuid
            JOIN workflow_node_job AS j ON j.uuid = d.workflow_node_job_uuid
            WHERE d.workflow_task_uuid = ?
              AND t.deleted_at IS NULL AND j.deleted_at IS NULL
            """,
            (task_uuid,),
        ).fetchone()
        if row is None:
            raise WorkflowError("not_found")
        return {
            "task_uuid": row["workflow_task_uuid"],
            "job_uuid": row["workflow_node_job_uuid"],
            "authority_id": row["authority_id"],
            "template_catalog_fingerprint": row["template_catalog_fingerprint"],
            "workflow_node_template_uuid": row["workflow_node_template_uuid"],
            "name": row["action_name"],
            "display_name": row["action_display_name"],
            "device_id": row["device_id"],
            "status": row["status"],
            "control_status": row["control_status"],
            "cleanup_status": row["cleanup_status"],
            "input": _load(row["input"], {}),
            "output": _load(row["output"], {}),
            "error_info": _load(row["error_info"], []),
            "job_status": row["job_status"],
            "feedback_cursor": row["feedback_sequence"],
            "create_time": row["task_create_time"],
            "update_time": row["task_update_time"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
        }


__all__ = [
    "DeviceActionAdmission",
    "DeviceActionLiveCatalog",
    "DeviceActionTaskService",
]
