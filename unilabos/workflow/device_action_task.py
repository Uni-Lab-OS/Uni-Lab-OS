"""设备页单 Action 运行的正式 Workflow Task/Job deep module。"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
from collections.abc import Callable, Mapping
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
    normalize_value,
    parse_input_contract,
    parse_output_contract,
    parse_value_schema,
)
from unilabos.workflow.service import WorkflowError, WorkflowService
from unilabos.workflow.store import WorkflowStore, utc_now
from unilabos.workflow.task_input import TaskInputError

_ORIGIN_KIND = "system/device-console"
_LOGGER = logging.getLogger(__name__)


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
        if authority_id != self._authority.authority_id:
            raise WorkflowError("not_found")

        payload_hash = self._payload_hash(
            authority_id=authority_id,
            template_catalog_fingerprint=template_catalog_fingerprint,
            workflow_node_template_uuid=workflow_node_template_uuid,
            device_id=device_id,
            input_value=input_value,
            description=description,
        )
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
                return self._view(existing["workflow_task_uuid"], conn=conn)

        if not self._admission.is_available():
            raise WorkflowError("admission_unavailable")

        try:
            with self._template_catalog.snapshot(self._authority) as snapshot:
                snapshot.assert_fingerprint(template_catalog_fingerprint)
                template = snapshot.require_node(workflow_node_template_uuid)
                if template.get("node_type") != "device":
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
                try:
                    self._admission.wake(view["task_uuid"], view["job_uuid"])
                except Exception:  # noqa: BLE001 - durable commit 后只允许异步恢复
                    # Task/Job 已提交；唤醒失败只能保持 durable pending，不能把
                    # 已成功的幂等创建伪装成 HTTP 失败。
                    _LOGGER.exception(
                        "D1A admission wake failed after durable task commit"
                    )
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
            raise WorkflowError("device_action_mismatch")
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

        workflow_uuid = (
            str(existing["workflow_uuid"]) if existing is not None else str(uuid4())
        )
        node_uuid = (
            str(existing["workflow_node_uuid"])
            if existing is not None
            else str(uuid4())
        )
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
        contract_snapshot = {
            "action_type": template["type"],
            "input_contract": input_contract,
            "output_contract": output_contract,
        }
        if existing is not None:
            if existing["contract_snapshot"] == _json(contract_snapshot):
                if existing["template_catalog_fingerprint"] != fingerprint:
                    conn.execute(
                        """
                        UPDATE device_action_system_source
                        SET template_catalog_fingerprint = ?, update_time = ?
                        WHERE authority_id = ?
                          AND workflow_node_template_uuid = ?
                        """,
                        (
                            fingerprint,
                            now,
                            self._authority.authority_id,
                            template_uuid,
                        ),
                    )
                return dict(existing)

            revision = int(existing["source_revision"]) + 1
            conn.execute(
                """
                UPDATE workflow
                SET update_time = ?, meta_data = ?, name = ?, revision = ?
                WHERE uuid = ? AND deleted_at IS NULL
                """,
                (
                    now,
                    _json(workflow_meta),
                    f"Device console: {template['display_name']}",
                    revision,
                    workflow_uuid,
                ),
            )
            conn.execute(
                """
                UPDATE workflow_node
                SET update_time = ?, meta_data = ?,
                    workflow_node_template_uuid = ?, name = ?, icon = ?,
                    footer = ?, action_name = ?, action_type = ?
                WHERE uuid = ? AND deleted_at IS NULL
                """,
                (
                    now,
                    _json(node_meta),
                    template_uuid,
                    template["display_name"],
                    template.get("icon"),
                    template.get("footer"),
                    template["name"],
                    template["type"],
                    node_uuid,
                ),
            )
            conn.execute(
                """
                UPDATE device_action_system_source
                SET source_revision = ?, template_catalog_fingerprint = ?,
                    contract_snapshot = ?, update_time = ?
                WHERE authority_id = ? AND workflow_node_template_uuid = ?
                """,
                (
                    revision,
                    fingerprint,
                    _json(contract_snapshot),
                    now,
                    self._authority.authority_id,
                    template_uuid,
                ),
            )
            updated = dict(existing)
            updated.update(
                source_revision=revision,
                template_catalog_fingerprint=fingerprint,
                contract_snapshot=_json(contract_snapshot),
                update_time=now,
            )
            return updated

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
                      'device', ?, '{}', '{}', ?, ?, ?, '{}', 0, 0, NULL)
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
                template["type"],
            ),
        )
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


class DeviceActionTaskRuntimeBridge:
    """把正式 D1A Task/Job 适配到既有 EdgeScheduler/HostNode 执行栈。"""

    def __init__(
        self,
        *,
        store: WorkflowStore,
        coordinator: Any,
        scheduler: Any,
        backend: Any,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        self._store = store
        self._coordinator = coordinator
        self._scheduler = scheduler
        self._backend = backend
        self._fault_hook = fault_hook
        self._started = False
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._cancel_thread: threading.Thread | None = None

    def _inject_fault(self, stage: str) -> None:
        hook = self._fault_hook
        if hook is not None:
            hook(stage)

    def bind_execution_stack(self, scheduler: Any, backend: Any) -> None:
        """在 production Edge stack 就绪后完成一次性反向绑定。"""

        if scheduler is None or backend is None:
            raise ValueError("D1A execution stack must be complete")
        with self._lock:
            if self._started:
                if self._scheduler is scheduler and self._backend is backend:
                    return
                raise RuntimeError("D1A execution stack is already bound")
            self._scheduler = scheduler
            self._backend = backend
        self.start()

    def unbind_execution_stack(self) -> None:
        """先移除 listener，再释放 integration 持有的 Edge stack。"""

        self.stop()
        with self._lock:
            self._scheduler = None
            self._backend = None

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            if self._scheduler is None or self._backend is None:
                return
            self._scheduler.audit_inventory_job_claims()
            self._scheduler.set_device_action_task_hooks(
                before=self._before_dispatch,
                on_error=self._dispatch_failed,
            )
            self._scheduler.set_device_action_fence_provider(
                self._scheduler.busy_inventory_device_action_keys
            )
            self._backend.add_job_status_listener(self._on_job_status)
            self._backend.add_job_completion_listener(self._on_job_finished)
            self._started = True
            self.recover_inventory_claims()
            self.replay_pending()
            self._stop_event.clear()
            self._cancel_thread = threading.Thread(
                target=self._run_cancel_sweep,
                name="device-action-task-runtime",
                daemon=True,
            )
            self._cancel_thread.start()

    def recover_inventory_claims(self) -> None:
        """Fail closed for every pre-restart D1A physical execution fact."""

        if not self._started:
            return
        with self._store.transaction() as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT d.workflow_task_uuid, d.workflow_node_job_uuid,
                           d.device_id, d.claim_status,
                           d.inventory_claim_uuid,
                           j.status AS job_status,
                           t.status AS task_status,
                           t.control_status AS task_control_status
                    FROM device_action_task AS d
                    JOIN workflow_node_job AS j
                      ON j.uuid = d.workflow_node_job_uuid
                    JOIN workflow_task AS t
                      ON t.uuid = d.workflow_task_uuid
                    WHERE d.claim_status IN ('claimed', 'unknown')
                       OR j.status = 'execution_unknown'
                    ORDER BY d.create_time, d.workflow_task_uuid
                    """
                )
            ]
        for row in rows:
            job_uuid = row["workflow_node_job_uuid"]
            claim = self._scheduler.find_inventory_job_claim(job_uuid, 1)
            if claim is None:
                acquired = self._scheduler.acquire_device_action_job_claim(
                    task_uuid=row["workflow_task_uuid"],
                    job_uuid=job_uuid,
                    device_id=row["device_id"],
                    attempt=1,
                )
                if acquired.status != "acquired" or acquired.claim is None:
                    raise WorkflowError("admission_unavailable")
                claim = acquired.claim
            receipt = self._scheduler.terminal_material_changeset(job_uuid, 1)
            if receipt is not None:
                self._recover_terminal_receipt(row, claim, receipt)
                continue
            if claim.state == "released":
                raise WorkflowError("reconciliation_required")
            evidence_fingerprint = hashlib.sha256(
                encode_json(
                    {
                        "job_uuid": job_uuid,
                        "phase": "startup_recovery",
                        "workflow_job_status": row["job_status"],
                        "legacy_claim_status": row["claim_status"],
                    },
                    sort_keys=True,
                )
            ).hexdigest()
            uncertain = self._scheduler.mark_device_action_job_claim_uncertain(
                claim=claim,
                reason="os_process_restart",
                evidence_fingerprint=evidence_fingerprint,
            )
            if uncertain.claim is None:
                raise WorkflowError("reconciliation_required")
            with self._store.transaction() as connection:
                now = utc_now()
                projection_changed = connection.execute(
                    """
                    UPDATE device_action_task
                    SET claim_status = 'unknown', inventory_claim_uuid = ?,
                        inventory_fencing_token = ?,
                        inventory_claim_set_fingerprint = ?, update_time = ?
                    WHERE workflow_node_job_uuid = ?
                      AND (claim_status <> 'unknown'
                           OR inventory_claim_uuid IS NOT ?
                           OR inventory_fencing_token IS NOT ?
                           OR inventory_claim_set_fingerprint IS NOT ?)
                    """,
                    (
                        uncertain.claim.uuid,
                        uncertain.claim.fencing_token,
                        uncertain.claim.set_fingerprint,
                        now,
                        job_uuid,
                        uncertain.claim.uuid,
                        uncertain.claim.fencing_token,
                        uncertain.claim.set_fingerprint,
                    ),
                ).rowcount
                attention_changed = 0
                if row["job_status"] in {
                    "succeeded",
                    "failed",
                    "canceled",
                    "timeout",
                }:
                    attention_changed = connection.execute(
                        """
                        UPDATE workflow_task
                        SET cleanup_status = 'requires_attention',
                            attention_reason = 'terminal_without_material_receipt',
                            update_time = ?
                        WHERE uuid = ?
                          AND (cleanup_status <> 'requires_attention'
                               OR attention_reason IS NOT
                                  'terminal_without_material_receipt')
                        """,
                        (now, row["workflow_task_uuid"]),
                    ).rowcount
                if projection_changed or attention_changed:
                    WorkflowStore._append_event(
                        connection,
                        event="device_action_task.changed",
                        data={"task_uuid": row["workflow_task_uuid"]},
                        now=now,
                    )
            self._scheduler.acknowledge_inventory_result(uncertain.outbox_sequence)

    def _recover_terminal_receipt(
        self,
        row: dict[str, Any],
        claim: Any,
        receipt: Any,
    ) -> None:
        """Replay C4/C5: receipt -> Workflow terminal -> Claim release."""

        result_payload = receipt.result
        output = result_payload.get("return_info")
        error_info = result_payload.get("error_info")
        if not isinstance(output, dict) or not isinstance(error_info, list):
            raise WorkflowError("reconciliation_required")
        terminal_status = receipt.outcome
        if terminal_status not in {"succeeded", "failed", "canceled", "timeout"}:
            raise WorkflowError("reconciliation_required")
        terminal_fingerprint = hashlib.sha256(
            encode_json(
                {
                    "job_uuid": row["workflow_node_job_uuid"],
                    "attempt": claim.attempt,
                    "terminal_job_status": terminal_status,
                    "material_changeset_uuid": receipt.uuid,
                    "material_changeset_fingerprint": (
                        receipt.deterministic_fingerprint
                    ),
                    "material_changeset_outcome": receipt.outcome,
                    "return_info": output,
                    "error_info": error_info,
                },
                sort_keys=True,
            )
        ).hexdigest()
        with self._store.transaction() as connection:
            current = connection.execute(
                """
                SELECT j.status AS job_status, t.status AS task_status
                FROM workflow_node_job AS j
                JOIN workflow_task AS t ON t.uuid = j.workflow_task_uuid
                WHERE j.uuid = ?
                """,
                (row["workflow_node_job_uuid"],),
            ).fetchone()
            if current is None:
                raise WorkflowError("reconciliation_required")
            now = utc_now()
            if current["job_status"] not in {
                "succeeded",
                "failed",
                "canceled",
                "timeout",
            }:
                connection.execute(
                    """
                    UPDATE workflow_node_job
                    SET status = ?, return_info = ?, error_info = ?,
                        uncertainty_reason = NULL,
                        finished_at = COALESCE(finished_at, ?), update_time = ?
                    WHERE uuid = ?
                    """,
                    (
                        terminal_status,
                        _json(output),
                        _json(error_info),
                        now,
                        now,
                        row["workflow_node_job_uuid"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE workflow_task
                    SET status = ?, output = ?, error_info = ?,
                        cleanup_status = 'pending', attention_reason = NULL,
                        finished_at = COALESCE(finished_at, ?), update_time = ?
                    WHERE uuid = ?
                    """,
                    (
                        terminal_status,
                        _json(output),
                        _json(error_info),
                        now,
                        now,
                        row["workflow_task_uuid"],
                    ),
                )
                self._coordinator._append_journal(
                    connection,
                    task_uuid=row["workflow_task_uuid"],
                    job_uuid=row["workflow_node_job_uuid"],
                    kind="uncertainty_resolved",
                    from_status=current["job_status"],
                    to_status=terminal_status,
                    data={"reason": "material_changeset_receipt_replayed"},
                    now=now,
                )
            elif current["job_status"] != terminal_status:
                raise WorkflowError("reconciliation_required")
            connection.execute(
                """
                UPDATE device_action_task
                SET claim_status = 'claimed', inventory_claim_uuid = ?,
                    inventory_fencing_token = ?,
                    inventory_claim_set_fingerprint = ?,
                    material_changeset_uuid = ?,
                    material_changeset_fingerprint = ?,
                    material_changeset_outbox_sequence = ?,
                    workflow_terminal_fingerprint = ?, update_time = ?
                WHERE workflow_node_job_uuid = ?
                """,
                (
                    claim.uuid,
                    claim.fencing_token,
                    claim.set_fingerprint,
                    receipt.uuid,
                    receipt.deterministic_fingerprint,
                    receipt.outbox_sequence,
                    terminal_fingerprint,
                    now,
                    row["workflow_node_job_uuid"],
                ),
            )
            self._runtime_events(
                connection,
                task_uuid=row["workflow_task_uuid"],
                now=now,
            )
        self._scheduler.acknowledge_inventory_result(receipt.outbox_sequence)
        release_result = self._scheduler.release_device_action_job_claim(
            claim=claim,
            receipt=receipt,
            workflow_terminal_fingerprint=terminal_fingerprint,
        )
        with self._store.transaction() as connection:
            now = utc_now()
            connection.execute(
                """
                UPDATE device_action_task
                SET claim_status = 'released', update_time = ?
                WHERE workflow_node_job_uuid = ?
                """,
                (now, row["workflow_node_job_uuid"]),
            )
            connection.execute(
                """
                UPDATE workflow_task
                SET cleanup_status = 'settled', update_time = ? WHERE uuid = ?
                """,
                (now, row["workflow_task_uuid"]),
            )
            self._runtime_events(
                connection,
                task_uuid=row["workflow_task_uuid"],
                now=now,
            )
        self._scheduler.acknowledge_inventory_result(release_result.outbox_sequence)

    def stop(self) -> None:
        with self._lock:
            self._started = False
            self._stop_event.set()
            if self._scheduler is not None:
                self._scheduler.set_device_action_task_hooks(
                    before=None,
                    on_error=None,
                )
                self._scheduler.set_device_action_fence_provider(None)
            if self._backend is not None:
                self._backend.remove_job_status_listener(self._on_job_status)
                self._backend.remove_job_completion_listener(self._on_job_finished)
        thread = self._cancel_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._cancel_thread = None

    def is_available(self) -> bool:
        return bool(
            self._started
            and self._scheduler is not None
            and self._backend is not None
            and getattr(self._backend, "_running", True)
        )

    def wake(self, task_uuid: str, job_uuid: str) -> None:
        if not self.is_available():
            raise WorkflowError("admission_unavailable")
        self._submit(task_uuid, expected_job_uuid=job_uuid)

    def replay_pending(self) -> None:
        if not self._started:
            return
        with self._store.transaction() as connection:
            task_uuids = [
                row["workflow_task_uuid"]
                for row in connection.execute(
                    """
                    SELECT d.workflow_task_uuid
                    FROM device_action_task AS d
                    JOIN workflow_task AS t ON t.uuid = d.workflow_task_uuid
                    JOIN workflow_node_job AS j
                      ON j.uuid = d.workflow_node_job_uuid
                    WHERE t.deleted_at IS NULL AND j.deleted_at IS NULL
                      AND t.status = 'pending' AND j.status = 'pending'
                    ORDER BY t.create_time, t.uuid
                    """
                )
            ]
        for task_uuid in task_uuids:
            self._submit(task_uuid)

    def busy_device_action_keys(self) -> set[str]:
        """Compatibility read delegates to the sole Inventory Claim authority."""

        if self._scheduler is None:
            return set()
        return set(self._scheduler.busy_inventory_device_action_keys())

    def sweep_cancellations(self) -> None:
        """把 durable cancel 状态投影到 scheduler/backend；外部调用幂等。"""

        if not self._started:
            return
        with self._store.transaction() as connection:
            pending_canceled = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT d.workflow_task_uuid, d.workflow_node_job_uuid
                    FROM device_action_task AS d
                    JOIN workflow_task AS t ON t.uuid = d.workflow_task_uuid
                    JOIN workflow_node_job AS j
                      ON j.uuid = d.workflow_node_job_uuid
                    WHERE t.status = 'canceled' AND j.status = 'canceled'
                      AND d.claim_status = 'pending'
                    """
                )
            ]
            cancel_requested = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT d.workflow_task_uuid, d.workflow_node_job_uuid,
                           c.uuid AS command_uuid
                    FROM device_action_task AS d
                    JOIN workflow_node_job AS j
                      ON j.uuid = d.workflow_node_job_uuid
                    JOIN workflow_task_command AS c
                      ON c.workflow_task_uuid = d.workflow_task_uuid
                     AND c.type = 'cancel' AND c.status = 'succeeded'
                    WHERE j.status = 'cancel_requested'
                      AND j.cancel_command_uuid IS NULL
                    ORDER BY c.consumed_at DESC, c.uuid DESC
                    """
                )
            ]
            now = utc_now()
            for item in pending_canceled:
                connection.execute(
                    """
                    UPDATE device_action_task
                    SET claim_status = 'released', update_time = ?
                    WHERE workflow_task_uuid = ? AND claim_status = 'pending'
                    """,
                    (now, item["workflow_task_uuid"]),
                )
            claimed: list[dict[str, Any]] = []
            for item in cancel_requested:
                changed = connection.execute(
                    """
                    UPDATE workflow_node_job
                    SET cancel_command_uuid = ?, update_time = ?
                    WHERE uuid = ? AND cancel_command_uuid IS NULL
                    """,
                    (
                        item["command_uuid"],
                        now,
                        item["workflow_node_job_uuid"],
                    ),
                ).rowcount
                if changed:
                    claimed.append(item)

        for item in pending_canceled:
            self._scheduler.cancel_device_action_task(item["workflow_task_uuid"])
        for item in claimed:
            job_uuid = item["workflow_node_job_uuid"]
            if self._backend.request_cancel(job_uuid):
                continue
            claim = self._scheduler.inventory_job_claim(job_uuid, 1)
            evidence_fingerprint = hashlib.sha256(
                encode_json(
                    {
                        "job_uuid": job_uuid,
                        "phase": "cancel_unconfirmed",
                        "command_uuid": item["command_uuid"],
                    },
                    sort_keys=True,
                )
            ).hexdigest()
            uncertain_result = self._scheduler.mark_device_action_job_claim_uncertain(
                claim=claim,
                reason="device_action_cancel_unconfirmed",
                evidence_fingerprint=evidence_fingerprint,
            )
            self._coordinator.mark_job_unknown(
                job_uuid,
                "device_action_cancel_unconfirmed",
            )
            with self._store.transaction() as connection:
                now = utc_now()
                connection.execute(
                    """
                    UPDATE device_action_task
                    SET claim_status = 'unknown', update_time = ?
                    WHERE workflow_node_job_uuid = ?
                    """,
                    (now, job_uuid),
                )
                WorkflowStore._append_event(
                    connection,
                    event="device_action_task.changed",
                    data={"task_uuid": item["workflow_task_uuid"]},
                    now=now,
                )
            self._scheduler.acknowledge_inventory_result(
                uncertain_result.outbox_sequence
            )

    def _run_cancel_sweep(self) -> None:
        while not self._stop_event.wait(0.1):
            try:
                self.sweep_cancellations()
            except (sqlite3.Error, RuntimeError):
                _LOGGER.exception("D1A cancel sweep failed")

    def _submit(
        self,
        task_uuid: str,
        *,
        expected_job_uuid: str | None = None,
    ) -> None:
        with self._lock, self._store.transaction() as connection:
            row = connection.execute(
                """
                SELECT d.*, t.status AS task_status, t.trace_context,
                       t.workflow_snapshot,
                       j.status AS job_status, j.param, j.workflow_node_uuid
                FROM device_action_task AS d
                JOIN workflow_task AS t ON t.uuid = d.workflow_task_uuid
                JOIN workflow_node_job AS j
                  ON j.uuid = d.workflow_node_job_uuid
                WHERE d.workflow_task_uuid = ?
                """,
                (task_uuid,),
            ).fetchone()
            if row is None:
                raise WorkflowError("not_found")
            if (
                expected_job_uuid is not None
                and row["workflow_node_job_uuid"] != expected_job_uuid
            ):
                raise WorkflowError("conflict")
            if row["task_status"] != "pending" or row["job_status"] != "pending":
                return
            if self._scheduler.has_device_action_task(task_uuid):
                return
            workflow_snapshot = _load(row["workflow_snapshot"], {})
            nodes = (
                workflow_snapshot.get("nodes")
                if isinstance(workflow_snapshot, dict)
                else None
            )
            frozen_node = (
                next(
                    (
                        node
                        for node in nodes
                        if isinstance(node, dict)
                        and node.get("uuid") == row["workflow_node_uuid"]
                    ),
                    None,
                )
                if isinstance(nodes, list)
                else None
            )
            action_type = (
                frozen_node.get("action_type")
                if isinstance(frozen_node, dict)
                else None
            )
            if not isinstance(action_type, str) or not action_type:
                raise WorkflowError("internal_error")
            job_uuid = row["workflow_node_job_uuid"]
            device_id = row["device_id"]
            action_name = row["action_name"]
            input_value = _load(row["param"], {})
        self._scheduler.submit_device_action_task(
            task_uuid=task_uuid,
            job_uuid=job_uuid,
            device_id=device_id,
            action_name=action_name,
            action_type=action_type,
            input_value=input_value,
        )

    def _before_dispatch(
        self,
        *,
        task_uuid: str,
        job_uuid: str,
        device_id: str,
        action_name: str,
    ) -> bool:
        if not self._is_d1a_job(job_uuid):
            return False
        claim_result = self._scheduler.acquire_device_action_job_claim(
            task_uuid=task_uuid,
            job_uuid=job_uuid,
            device_id=device_id,
            attempt=1,
        )
        if claim_result.status == "blocked":
            return False
        if claim_result.status != "acquired" or claim_result.claim is None:
            raise WorkflowError("conflict")
        claim = claim_result.claim
        self._inject_fault("after_inventory_claim_commit")
        with self._store.transaction() as connection:
            row = connection.execute(
                """
                SELECT d.*, t.status AS task_status, j.status AS job_status
                FROM device_action_task AS d
                JOIN workflow_task AS t ON t.uuid = d.workflow_task_uuid
                JOIN workflow_node_job AS j
                  ON j.uuid = d.workflow_node_job_uuid
                WHERE d.workflow_node_job_uuid = ?
                """,
                (job_uuid,),
            ).fetchone()
            if row is None:
                return False
            if (
                row["task_status"] != "pending"
                or row["job_status"] != "pending"
                or row["claim_status"] != "pending"
                or task_uuid != row["workflow_task_uuid"]
                or device_id != row["device_id"]
                or action_name != row["action_name"]
            ):
                raise WorkflowError("conflict")
            now = utc_now()
            connection.execute(
                """
                UPDATE workflow_task
                SET status = 'running', started_at = COALESCE(started_at, ?),
                    update_time = ?
                WHERE uuid = ?
                """,
                (now, now, row["workflow_task_uuid"]),
            )
            connection.execute(
                """
                UPDATE workflow_node_job
                SET status = 'dispatched', update_time = ? WHERE uuid = ?
                """,
                (now, job_uuid),
            )
            connection.execute(
                """
                UPDATE device_action_task
                SET admitted_device_id = device_id, claim_status = 'claimed',
                    inventory_claim_uuid = ?, inventory_fencing_token = ?,
                    inventory_claim_set_fingerprint = ?,
                    update_time = ?
                WHERE workflow_node_job_uuid = ?
                """,
                (
                    claim.uuid,
                    claim.fencing_token,
                    claim.set_fingerprint,
                    now,
                    job_uuid,
                ),
            )
            self._coordinator._append_journal(
                connection,
                task_uuid=row["workflow_task_uuid"],
                kind="task_transition",
                from_status="pending",
                to_status="running",
                now=now,
            )
            self._coordinator._append_journal(
                connection,
                task_uuid=row["workflow_task_uuid"],
                job_uuid=job_uuid,
                kind="job_transition",
                from_status="pending",
                to_status="dispatched",
                now=now,
            )
            self._runtime_events(
                connection,
                task_uuid=row["workflow_task_uuid"],
                now=now,
            )
        self._inject_fault("after_workflow_dispatch_commit")
        self._scheduler.acknowledge_inventory_result(claim_result.outbox_sequence)
        return True

    def _dispatch_failed(
        self,
        *,
        task_uuid: str,
        job_uuid: str,
        device_id: str,
        action_name: str,
        error: BaseException,
    ) -> None:
        del task_uuid, device_id, action_name
        if not self._is_d1a_job(job_uuid):
            return
        claim = self._scheduler.inventory_job_claim(job_uuid, 1)
        evidence_fingerprint = hashlib.sha256(
            encode_json(
                {
                    "job_uuid": job_uuid,
                    "phase": "dispatch_exception",
                    "error_type": type(error).__name__,
                },
                sort_keys=True,
            )
        ).hexdigest()
        uncertain_result = self._scheduler.mark_device_action_job_claim_uncertain(
            claim=claim,
            reason=f"edge_dispatch_unconfirmed:{type(error).__name__}",
            evidence_fingerprint=evidence_fingerprint,
        )
        self._coordinator.mark_job_unknown(
            job_uuid,
            f"edge_dispatch_unconfirmed:{type(error).__name__}",
        )
        with self._store.transaction() as connection:
            now = utc_now()
            row = connection.execute(
                """
                SELECT workflow_task_uuid FROM device_action_task
                WHERE workflow_node_job_uuid = ?
                """,
                (job_uuid,),
            ).fetchone()
            if row is None:
                return
            connection.execute(
                """
                UPDATE device_action_task
                SET claim_status = 'unknown', update_time = ?
                WHERE workflow_node_job_uuid = ?
                """,
                (now, job_uuid),
            )
            WorkflowStore._append_event(
                connection,
                event="device_action_task.changed",
                data={"task_uuid": row["workflow_task_uuid"]},
                now=now,
            )
        self._scheduler.acknowledge_inventory_result(uncertain_result.outbox_sequence)

    def _on_job_status(
        self,
        job_uuid: str,
        feedback_data: dict[str, Any],
        status: str,
    ) -> None:
        if not self._started or status != "running" or not self._is_d1a_job(job_uuid):
            return
        with self._store.transaction() as connection:
            job = connection.execute(
                """
                SELECT status, feedback_sequence
                FROM workflow_node_job WHERE uuid = ?
                """,
                (job_uuid,),
            ).fetchone()
            if job is None:
                return
            current_status = job["status"]
            sequence = int(job["feedback_sequence"]) + 1
        if current_status not in {"dispatched", "running"}:
            return
        claim = self._scheduler.inventory_job_claim(job_uuid, 1)
        running_result = None
        if claim.state in {"reserved", "uncertain"}:
            evidence_fingerprint = hashlib.sha256(
                encode_json(
                    {
                        "job_uuid": job_uuid,
                        "status": status,
                        "feedback": feedback_data,
                    },
                    sort_keys=True,
                )
            ).hexdigest()
            running_result = self._scheduler.mark_device_action_job_claim_running(
                claim=claim,
                evidence_fingerprint=evidence_fingerprint,
            )
            self._inject_fault("after_inventory_claim_running")
        elif claim.state != "running":
            return
        if current_status == "dispatched":
            self._coordinator.transition_job(job_uuid, "running")
        elif current_status != "running":
            return
        if feedback_data:
            observed_at = utc_now()
            fingerprint = hashlib.sha256(
                encode_json(feedback_data, sort_keys=True)
            ).hexdigest()
            self._coordinator.commit_job_feedback(
                job_uuid,
                [
                    {
                        "sequence": sequence,
                        "feedback_type": "feedback",
                        "data": feedback_data,
                        "observed_at": observed_at,
                        "idempotency_key": f"d1a:{job_uuid}:{sequence}:{fingerprint}",
                    }
                ],
            )
        if running_result is not None:
            self._scheduler.acknowledge_inventory_result(running_result.outbox_sequence)

    def _on_job_finished(
        self,
        job_uuid: str,
        success: bool,
        result: Any,
        suc_type: str,
    ) -> bool:
        if not self._started or not self._is_d1a_job(job_uuid):
            return False
        if suc_type == "transport_unknown":
            with self._store.transaction() as connection:
                current = connection.execute(
                    """
                    SELECT j.status, d.workflow_task_uuid
                    FROM workflow_node_job AS j
                    JOIN device_action_task AS d
                      ON d.workflow_node_job_uuid = j.uuid
                    WHERE j.uuid = ?
                    """,
                    (job_uuid,),
                ).fetchone()
            if current is None:
                return False
            if current["status"] in {"succeeded", "failed", "canceled", "timeout"}:
                return True
            claim = self._scheduler.inventory_job_claim(job_uuid, 1)
            evidence_fingerprint = hashlib.sha256(
                encode_json(
                    {
                        "job_uuid": job_uuid,
                        "phase": "transport_unknown",
                        "suc_type": suc_type,
                    },
                    sort_keys=True,
                )
            ).hexdigest()
            uncertain_result = self._scheduler.mark_device_action_job_claim_uncertain(
                claim=claim,
                reason="device_action_transport_unknown",
                evidence_fingerprint=evidence_fingerprint,
            )
            if current["status"] == "execution_unknown":
                self._scheduler.acknowledge_inventory_result(
                    uncertain_result.outbox_sequence
                )
                return True
            self._coordinator.mark_job_unknown(
                job_uuid,
                "device_action_transport_unknown",
            )
            with self._store.transaction() as connection:
                now = utc_now()
                connection.execute(
                    """
                    UPDATE device_action_task
                    SET claim_status = 'unknown', update_time = ?
                    WHERE workflow_node_job_uuid = ?
                    """,
                    (now, job_uuid),
                )
                WorkflowStore._append_event(
                    connection,
                    event="device_action_task.changed",
                    data={"task_uuid": current["workflow_task_uuid"]},
                    now=now,
                )
            self._scheduler.acknowledge_inventory_result(
                uncertain_result.outbox_sequence
            )
            return True
        with self._store.transaction() as connection:
            observed = connection.execute(
                """
                SELECT d.*, t.status AS task_status,
                       t.control_status AS task_control_status,
                       t.reconciliation_resume_control_status,
                       j.status AS job_status, t.workflow_snapshot
                FROM device_action_task AS d
                JOIN workflow_task AS t ON t.uuid = d.workflow_task_uuid
                JOIN workflow_node_job AS j
                  ON j.uuid = d.workflow_node_job_uuid
                WHERE d.workflow_node_job_uuid = ?
                """,
                (job_uuid,),
            ).fetchone()
        if observed is None or observed["job_status"] in {
            "succeeded",
            "failed",
            "canceled",
            "timeout",
        }:
            return observed is not None
        canceled = (
            observed["job_status"] == "cancel_requested"
            or observed["task_status"] == "canceling"
        )
        output: dict[str, Any] = {}
        error_info: list[dict[str, Any]] = []
        job_status = "canceled" if canceled else "failed"
        task_status = "canceled" if canceled else "failed"
        if success and not canceled and suc_type == "normal":
            try:
                task_snapshot = _load(observed["workflow_snapshot"], {})
                output_contract = task_snapshot["workflow"]["meta_data"]["unilab"][
                    "output_contract"
                ]
                output = self._normalize_output(
                    _json({"output_contract": output_contract}),
                    result,
                )
            except (KeyError, TypeError, ValueError, WorkflowSchemaError):
                error_info = [{"code": "invalid_device_action_result"}]
            else:
                job_status = "succeeded"
                task_status = "succeeded"
        elif not canceled:
            error_info = [
                {
                    "code": "device_action_failed",
                    "suc_type": str(suc_type or "normal"),
                }
            ]

        claim = self._scheduler.inventory_job_claim(job_uuid, 1)
        if claim.state == "reserved":
            terminal_evidence_fingerprint = hashlib.sha256(
                encode_json(
                    {
                        "job_uuid": job_uuid,
                        "phase": "terminal_evidence",
                        "success": bool(success),
                        "suc_type": str(suc_type or "normal"),
                    },
                    sort_keys=True,
                )
            ).hexdigest()
            running_result = self._scheduler.mark_device_action_job_claim_running(
                claim=claim,
                evidence_fingerprint=terminal_evidence_fingerprint,
            )
            if running_result.claim is None:
                raise WorkflowError("internal_error")
            claim = running_result.claim
        receipt = self._scheduler.commit_device_action_terminal_changeset(
            claim=claim,
            outcome=job_status,
            result={"return_info": output, "error_info": error_info},
        )
        self._inject_fault("after_material_changeset_commit")
        terminal_fingerprint = hashlib.sha256(
            encode_json(
                {
                    "job_uuid": job_uuid,
                    "attempt": claim.attempt,
                    "terminal_job_status": job_status,
                    "material_changeset_uuid": receipt.uuid,
                    "material_changeset_fingerprint": (
                        receipt.deterministic_fingerprint
                    ),
                    "material_changeset_outcome": receipt.outcome,
                    "return_info": output,
                    "error_info": error_info,
                },
                sort_keys=True,
            )
        ).hexdigest()

        with self._store.transaction() as connection:
            row = connection.execute(
                """
                SELECT d.*, t.status AS task_status,
                       t.control_status AS task_control_status,
                       t.reconciliation_resume_control_status,
                       j.status AS job_status, t.workflow_snapshot
                FROM device_action_task AS d
                JOIN workflow_task AS t ON t.uuid = d.workflow_task_uuid
                JOIN workflow_node_job AS j
                  ON j.uuid = d.workflow_node_job_uuid
                WHERE d.workflow_node_job_uuid = ?
                """,
                (job_uuid,),
            ).fetchone()
            if row is None or row["job_status"] in {
                "succeeded",
                "failed",
                "canceled",
                "timeout",
            }:
                return row is not None
            now = utc_now()
            was_unknown = row["job_status"] == "execution_unknown"
            resumed_control_status = row["task_control_status"]
            if was_unknown:
                resumed_control_status = (
                    row["reconciliation_resume_control_status"]
                    if row["reconciliation_resume_control_status"]
                    in {"active", "paused"}
                    else "active"
                )
            connection.execute(
                """
                UPDATE workflow_node_job
                SET status = ?, return_info = ?, error_info = ?,
                    uncertainty_reason = NULL,
                    finished_at = COALESCE(finished_at, ?), update_time = ?
                WHERE uuid = ?
                """,
                (
                    job_status,
                    _json(output),
                    _json(error_info),
                    now,
                    now,
                    job_uuid,
                ),
            )
            connection.execute(
                """
                UPDATE workflow_task
                SET status = ?, output = ?, error_info = ?,
                    control_status = ?,
                    cleanup_status = 'pending',
                    reconciliation_resume_control_status = NULL,
                    attention_reason = NULL,
                    finished_at = COALESCE(finished_at, ?), update_time = ?
                WHERE uuid = ?
                """,
                (
                    task_status,
                    _json(output),
                    _json(error_info),
                    resumed_control_status,
                    now,
                    now,
                    row["workflow_task_uuid"],
                ),
            )
            connection.execute(
                """
                UPDATE device_action_task
                SET claim_status = 'claimed',
                    material_changeset_uuid = ?,
                    material_changeset_fingerprint = ?,
                    material_changeset_outbox_sequence = ?,
                    workflow_terminal_fingerprint = ?, update_time = ?
                WHERE workflow_node_job_uuid = ?
                """,
                (
                    receipt.uuid,
                    receipt.deterministic_fingerprint,
                    receipt.outbox_sequence,
                    terminal_fingerprint,
                    now,
                    job_uuid,
                ),
            )
            self._coordinator._append_journal(
                connection,
                task_uuid=row["workflow_task_uuid"],
                job_uuid=job_uuid,
                kind=("uncertainty_resolved" if was_unknown else "job_transition"),
                from_status=row["job_status"],
                to_status=job_status,
                data=({"reason": "late_device_action_result"} if was_unknown else None),
                now=now,
            )
            self._coordinator._append_journal(
                connection,
                task_uuid=row["workflow_task_uuid"],
                kind="task_transition",
                from_status=row["task_status"],
                to_status=task_status,
                now=now,
            )
            self._runtime_events(
                connection,
                task_uuid=row["workflow_task_uuid"],
                now=now,
            )
        self._inject_fault("after_workflow_terminal_commit")
        self._scheduler.acknowledge_inventory_result(receipt.outbox_sequence)
        release_result = self._scheduler.release_device_action_job_claim(
            claim=claim,
            receipt=receipt,
            workflow_terminal_fingerprint=terminal_fingerprint,
        )
        self._inject_fault("after_inventory_claim_release")
        with self._store.transaction() as connection:
            now = utc_now()
            connection.execute(
                """
                UPDATE device_action_task
                SET claim_status = 'released', update_time = ?
                WHERE workflow_node_job_uuid = ?
                  AND workflow_terminal_fingerprint = ?
                """,
                (now, job_uuid, terminal_fingerprint),
            )
            connection.execute(
                """
                UPDATE workflow_task
                SET cleanup_status = 'settled', update_time = ?
                WHERE uuid = ?
                """,
                (now, observed["workflow_task_uuid"]),
            )
            self._runtime_events(
                connection,
                task_uuid=observed["workflow_task_uuid"],
                now=now,
            )
        self._scheduler.acknowledge_inventory_result(release_result.outbox_sequence)
        return True

    def _is_d1a_job(self, job_uuid: str) -> bool:
        if not job_uuid:
            return False
        return self._store.is_device_action_job(job_uuid)

    @staticmethod
    def _normalize_output(contract_snapshot: str, raw: Any) -> dict[str, Any]:
        snapshot = _load(contract_snapshot, {})
        contract = parse_output_contract(snapshot["output_contract"]).to_dict()
        if type(raw) is not dict:
            raise ValueError("device action result must be an object")
        expected = {item["name"] for item in contract["outputs"]}
        if set(raw) != expected:
            raise ValueError("device action result fields do not match")
        return {
            item["name"]: normalize_value(
                parse_value_schema(item["schema"]),
                raw[item["name"]],
            )
            for item in contract["outputs"]
        }

    def _runtime_events(
        self,
        connection: sqlite3.Connection,
        *,
        task_uuid: str,
        now: str,
    ) -> None:
        self._coordinator._append_invalidation(
            connection,
            task_uuid=task_uuid,
            now=now,
        )
        WorkflowStore._append_event(
            connection,
            event="device_action_task.changed",
            data={"task_uuid": task_uuid},
            now=now,
        )


class HostNodeDeviceActionLiveCatalog:
    """用 HostNode 完成态 mapping 与 A1 Catalog 组成内部 live validation port。"""

    def __init__(
        self,
        *,
        template_catalog: TemplateCatalog,
        authority: CatalogAuthority,
        host_node_getter: Any | None = None,
    ) -> None:
        self._template_catalog = template_catalog
        self._authority = authority
        self._host_node_getter = host_node_getter or self._default_host_getter

    def snapshot(self) -> dict[str, dict[str, Any]]:
        host_node = self._host_node_getter()
        if host_node is None:
            return {}
        mappings = getattr(host_node, "_action_value_mappings", {}) or {}
        namespaces = getattr(host_node, "devices_names", {}) or {}
        online = set(getattr(host_node, "_online_devices", set()) or set())
        with self._template_catalog.snapshot(self._authority) as catalog:
            templates = list(catalog.node_templates)

        result: dict[str, dict[str, Any]] = {}
        for raw_device_id, raw_actions in mappings.items():
            device_id = str(raw_device_id)
            if not isinstance(raw_actions, Mapping):
                continue
            projected_actions: dict[str, dict[str, Any]] = {}
            owner_uuids: set[str] = set()
            for raw_name, raw_action in raw_actions.items():
                name = str(raw_name)
                if not isinstance(raw_action, Mapping):
                    continue
                schema = raw_action.get("schema")
                if not isinstance(schema, Mapping):
                    continue
                live_type = self._type_name(raw_action.get("type"))
                matches = [
                    template
                    for template in templates
                    if template.get("name") == name
                    and template.get("type") == live_type
                    and encode_json(_detached(template.get("schema")), sort_keys=True)
                    == encode_json(_detached(schema), sort_keys=True)
                ]
                if len(matches) != 1:
                    continue
                owner_uuids.add(str(matches[0]["resource_template_uuid"]))
                projected_actions[name] = {
                    "type": live_type,
                    "schema": _detached(schema),
                }
            if not projected_actions or len(owner_uuids) != 1:
                continue
            namespace = str(namespaces.get(raw_device_id, "") or "").rstrip("/")
            if namespace and not namespace.startswith("/"):
                namespace = f"/{namespace}"
            device_key = f"{namespace}/{device_id}" if namespace else f"/{device_id}"
            is_online = (
                device_id in online
                or device_key in online
                or f"/devices/{device_id}" in online
            )
            result[device_id] = {
                "online": is_online,
                "resource_template_uuid": next(iter(owner_uuids)),
                "actions": projected_actions,
            }
        return result

    @staticmethod
    def _type_name(value: Any) -> str:
        if hasattr(value, "__module__") and hasattr(value, "__name__"):
            return f"{value.__module__}.{value.__name__}"
        return str(value or "")

    @staticmethod
    def _default_host_getter() -> Any:
        from unilabos.ros.nodes.presets.host_node import HostNode

        return HostNode.get_instance(0)


__all__ = [
    "DeviceActionAdmission",
    "DeviceActionLiveCatalog",
    "DeviceActionTaskRuntimeBridge",
    "DeviceActionTaskService",
    "HostNodeDeviceActionLiveCatalog",
]
