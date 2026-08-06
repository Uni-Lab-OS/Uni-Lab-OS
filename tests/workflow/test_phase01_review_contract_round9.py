"""Phase 01 第九轮冻结 Backend 公共合同测试。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.models import CandidateCompilation, WorkflowNodeWrite
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
NODE_UUID = "22222222-2222-4222-8222-222222222222"
CATALOG_FINGERPRINT = f"sha256:{'5' * 64}"

INVALID_EVENT_CURSOR = {
    "error": {
        "code": "invalid_input",
        "message": "Last-Event-ID must be a non-negative integer",
    }
}
INTERNAL_ERROR = {
    "code": 500,
    "error": {
        "code": "internal_error",
        "message": "本地工作流服务出现错误，请重试或查看日志",
    },
}


class RecordingEventService(WorkflowService):
    def __init__(self, store: WorkflowStore) -> None:
        super().__init__(store)
        self.seen_after_ids: list[int] = []

    def list_events(
        self,
        *,
        after_id: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        del limit
        self.seen_after_ids.append(after_id)
        return {"items": [], "after_id": after_id}


class DamagedAppliedGraphStore(WorkflowStore):
    """Return one damaged copy only through the store's public graph seam."""

    damaged_field: str | None = None

    def get_graph(self, workflow_uuid: str) -> dict[str, Any]:
        graph = deepcopy(super().get_graph(workflow_uuid))
        if self.damaged_field == "workflow.name":
            graph["workflow"].pop("name")
        elif self.damaged_field == "node.name":
            graph["nodes"][0].pop("name")
        return graph


class ValidIndependentCandidateCompiler:
    compiler_version = "phase-01-review-round-9"
    template_catalog_fingerprint = CATALOG_FINGERPRINT

    def compile(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        python_source: str,
        source_uri: str,
        applied_graph: dict[str, Any],
    ) -> CandidateCompilation:
        del source_uri
        applied_workflow = applied_graph["workflow"]
        return CandidateCompilation(
            diagnostics=[],
            graph={
                "workflow": {
                    "uuid": workflow_uuid,
                    "create_time": applied_workflow["create_time"],
                    "update_time": applied_workflow["update_time"],
                    "meta_data": {},
                    "name": "valid independent candidate",
                    "tags": [],
                    "revision": workflow_revision,
                },
                "nodes": [],
                "edges": [],
                "node_templates": [],
                "handle_templates": [],
            },
            normalized_python_source=python_source,
            source_map=[],
            changeset={
                "kind": "graph",
                "created_node_uuids": [],
                "updated_node_uuids": [],
                "deleted_node_uuids": [node["uuid"] for node in applied_graph["nodes"]],
                "created_edge_uuids": [],
                "updated_edge_uuids": [],
                "deleted_edge_uuids": [],
                "reserved_metadata_changed": False,
            },
            compiler_version=self.compiler_version,
            template_catalog_fingerprint=self.template_catalog_fingerprint,
        )


@pytest.fixture()
def store(tmp_path: Path):
    opened = WorkflowStore(tmp_path / "workflow.db")
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture()
def damaged_store(tmp_path: Path):
    opened = DamagedAppliedGraphStore(tmp_path / "damaged-workflow.db")
    try:
        yield opened
    finally:
        opened.close()


def _response_status(messages: list[dict[str, Any]]) -> int:
    start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    return int(start["status"])


def _response_json(messages: list[dict[str, Any]]) -> dict[str, Any]:
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return json.loads(body)


def _get_events_through_asgi(
    app: FastAPI,
    *,
    last_event_id: str,
    disconnect_after: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    async def invoke() -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        request_sent = False

        async def receive() -> dict[str, Any]:
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {
                    "type": "http.request",
                    "body": b"",
                    "more_body": False,
                }
            if disconnect_after is None:
                await asyncio.sleep(0.01)
            else:
                while not disconnect_after():
                    await asyncio.sleep(0)
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            messages.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/v1/events",
            "raw_path": b"/api/v1/events",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"testserver"),
                (b"last-event-id", last_event_id.encode("utf-8")),
            ],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }
        await asyncio.wait_for(app(scope, receive, send), timeout=1)
        return messages

    return asyncio.run(invoke())


@pytest.mark.parametrize(
    "separator",
    ["\u001c", "\u001d", "\u001e", "\u001f"],
    ids=["file-separator", "group-separator", "record-separator", "unit-separator"],
)
def test_last_event_id_rejects_ascii_separators_outside_go_white_space(
    store: WorkflowStore,
    separator: str,
) -> None:
    service = RecordingEventService(store)

    messages = _get_events_through_asgi(
        create_workflow_app(service),
        last_event_id=f"{separator}10{separator}",
    )

    assert {
        "status": _response_status(messages),
        "body": _response_json(messages),
        "seen_after_ids": service.seen_after_ids,
    } == {
        "status": 400,
        "body": INVALID_EVENT_CURSOR,
        "seen_after_ids": [],
    }


@pytest.mark.parametrize(
    "white_space",
    ["\t", "\r", "\n", "\v", "\f", " "],
    ids=["tab", "carriage-return", "line-feed", "vertical-tab", "form-feed", "space"],
)
def test_last_event_id_trims_go_ascii_white_space(
    store: WorkflowStore,
    white_space: str,
) -> None:
    service = RecordingEventService(store)

    messages = _get_events_through_asgi(
        create_workflow_app(service),
        last_event_id=f"{white_space}10{white_space}",
        disconnect_after=lambda: bool(service.seen_after_ids),
    )

    assert {
        "status": _response_status(messages),
        "first_after_ids": service.seen_after_ids[:1],
    } == {
        "status": 200,
        "first_after_ids": [10],
    }


def test_last_event_id_go_ascii_white_space_only_becomes_cursor_zero(
    store: WorkflowStore,
) -> None:
    service = RecordingEventService(store)

    messages = _get_events_through_asgi(
        create_workflow_app(service),
        last_event_id="\t\r\n\v\f ",
        disconnect_after=lambda: bool(service.seen_after_ids),
    )

    assert {
        "status": _response_status(messages),
        "first_after_ids": service.seen_after_ids[:1],
    } == {
        "status": 200,
        "first_after_ids": [0],
    }


@pytest.mark.parametrize(
    "white_space",
    ["\u0085", "\u00a0", "\u2000", "\u3000"],
    ids=["next-line", "no-break-space", "en-quad", "ideographic-space"],
)
def test_last_event_id_trims_go_unicode_white_space(
    store: WorkflowStore,
    white_space: str,
) -> None:
    service = RecordingEventService(store)

    messages = _get_events_through_asgi(
        create_workflow_app(service),
        last_event_id=f"{white_space}10{white_space}",
        disconnect_after=lambda: bool(service.seen_after_ids),
    )

    assert {
        "status": _response_status(messages),
        "first_after_ids": service.seen_after_ids[:1],
    } == {
        "status": 200,
        "first_after_ids": [10],
    }


def test_last_event_id_go_unicode_white_space_only_becomes_cursor_zero(
    store: WorkflowStore,
) -> None:
    service = RecordingEventService(store)

    messages = _get_events_through_asgi(
        create_workflow_app(service),
        last_event_id="\u0085\u00a0\u2000\u3000",
        disconnect_after=lambda: bool(service.seen_after_ids),
    )

    assert {
        "status": _response_status(messages),
        "first_after_ids": service.seen_after_ids[:1],
    } == {
        "status": 200,
        "first_after_ids": [0],
    }


def _create_workflow(service: WorkflowService) -> dict[str, Any]:
    return service.create_workflow(
        name="phase 01 review round 9",
        tags=[],
        description=None,
        meta_data={},
        workflow_uuid=WORKFLOW_UUID,
    )


def _node() -> WorkflowNodeWrite:
    return WorkflowNodeWrite(
        uuid=NODE_UUID,
        name="applied node",
        status="idle",
        type="compute",
        pose={},
        param={},
        execution_policy={},
        disabled=False,
        minimized=False,
        meta_data={},
    )


def _authoring_service_with_damaged_applied_graph(
    store: DamagedAppliedGraphStore,
    tmp_path: Path,
    *,
    damaged_field: str,
) -> tuple[WorkflowService, int]:
    service = WorkflowService(
        store,
        compiler=ValidIndependentCandidateCompiler(),
    )
    _create_workflow(service)
    revision = 1
    if damaged_field == "node.name":
        service.save_graph(
            WORKFLOW_UUID,
            revision=1,
            nodes=[_node()],
            edges=[],
        )
        revision = 2
    package_root = tmp_path / "package"
    package_root.mkdir()
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="phase_01_review_round_9",
        package_root=package_root,
        relative_path="workflows/review.py",
    )
    store.damaged_field = damaged_field
    return service, revision


@pytest.mark.parametrize(
    "damaged_field",
    ["workflow.name", "node.name"],
    ids=["workflow-required-name", "node-required-name"],
)
def test_draft_reports_applied_store_invariant_as_internal_error(
    damaged_store: DamagedAppliedGraphStore,
    tmp_path: Path,
    damaged_field: str,
) -> None:
    service, revision = _authoring_service_with_damaged_applied_graph(
        damaged_store,
        tmp_path,
        damaged_field=damaged_field,
    )

    with TestClient(
        create_workflow_app(service),
        raise_server_exceptions=False,
    ) as client:
        response = client.put(
            f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/draft",
            json={
                "python_source": "build()\n",
                "expected_draft_hash": None,
                "expected_workflow_revision": revision,
            },
        )

    payload = (
        response.json()
        if response.headers.get("content-type", "").startswith("application/json")
        else None
    )
    assert {
        "status": response.status_code,
        "body": payload,
    } == {
        "status": 500,
        "body": INTERNAL_ERROR,
    }
