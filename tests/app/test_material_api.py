"""验证由 OS 实验图投影的统一只读 Material API。"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from unilabos.app.local_bridge.local_api import LocalApiState, create_app
from unilabos.app.local_bridge.material_api import MaterialGraphCatalog
from unilabos.app.local_bridge.material_models import MaterialModelRegistry
from unilabos.app.local_bridge.offline_os import OfflineOS
from unilabos.app.local_bridge.resource_template_api import (
    ResourceTemplateProxy,
)
from unilabos.app.local_bridge.schedule_ws import ScheduleSession
from unilabos.app.ws_client import WebSocketClient
from unilabos.resources.material_state import CurrentMaterialState
from unilabos.resources.resource_tracker import ResourceTreeSet

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT_ROOT = REPOSITORY_ROOT / "unilabos" / "test" / "experiments"


def _client(graph_name: str) -> TestClient:
    offline = OfflineOS()
    schedule = ScheduleSession(
        offline.receive,
        session_id=f"material-api-{graph_name}",
    )
    offline.bind(schedule)
    state = LocalApiState(
        schedule,
        material_catalog=MaterialGraphCatalog(
            EXPERIMENT_ROOT / graph_name
        ),
    )
    return TestClient(create_app(lambda: state))


def test_original_liquid_handler_projects_materials_and_sites() -> None:
    with _client("plr_test.json") as client:
        response = client.get("/api/v1/materials?page=1&page_size=100")

    assert response.status_code == 200
    page = response.json()["data"]
    assert page["total"] == 8
    assert page["page"] == 1
    assert page["page_size"] == 100
    by_code = {item["code"]: item for item in page["items"]}
    assert set(by_code) == {
        "PLR_STATION",
        "deck",
        "trash",
        "trash_core96",
        "teaching_carrier",
        "teaching_tip_rack",
        "tip_rack",
        "plate",
    }
    assert len(by_code["tip_rack"]["config"]["sites"]) == 96
    assert len(by_code["plate"]["config"]["sites"]) == 95
    assert len(by_code["deck"]["config"]["sites"]) == 32
    first_tip = by_code["tip_rack"]["config"]["sites"][0]
    assert first_tip["kind"] == "tip-spot"
    assert first_tip["shape"] == "circle"
    assert first_tip["visual"]["state"] == "tip-present"
    assert by_code["PLR_STATION"]["revision"] > 0
    assert {
        material["revision"] for material in by_code.values()
    } == {by_code["PLR_STATION"]["revision"]}
    assert by_code["deck"]["config"]["placement"]["parentId"] == (
        by_code["PLR_STATION"]["uuid"]
    )
    assert (
        by_code["deck"]["config"]["rendering"]["dimensionsMm"][1]
        <= 80
    )


def test_converted_plr_graph_keeps_devices_and_parent_placements() -> None:
    with _client("plr_test_converted.json") as client:
        response = client.get("/api/v1/materials?page_size=100")

    assert response.status_code == 200
    by_code = {
        item["code"]: item for item in response.json()["data"]["items"]
    }
    assert set(by_code) == {
        "liquid_handler",
        "deck",
        "tip_rack",
        "plate_well",
        "arm_slider",
        "hotel",
    }
    assert by_code["deck"]["config"]["placement"]["parentId"] == (
        by_code["liquid_handler"]["uuid"]
    )
    assert by_code["tip_rack"]["config"]["placement"]["parentId"] == (
        by_code["deck"]["uuid"]
    )
    assert (
        "tiprack_96_high"
        in by_code["tip_rack"]["config"]["rendering"]["model"]["path"]
    )
    tip_instances = by_code["tip_rack"]["config"]["rendering"]["model"][
        "instances"
    ]
    assert tip_instances["path"].endswith("/resources/tip/meshes/tip.stl")
    assert tip_instances["siteKinds"] == ["tip-spot"]
    assert tip_instances["visibleStates"] == ["tip-present"]
    assert len(by_code["plate_well"]["config"]["sites"]) == 96
    assert len(by_code["deck"]["config"]["sites"]) == 11
    well_a1 = next(
        site
        for site in by_code["plate_well"]["config"]["sites"]
        if site["key"] == "plate_well_A1"
    )
    assert well_a1["poseInAnchor"]["positionMm"] == [10.2, 70.05, 3.0]
    assert well_a1["sizeMm"] == [8.2, 8.2, 38.0]
    assert well_a1["maxVolumeUl"] == 2000.0
    assert well_a1["visual"]["state"] == "filled"
    assert (
        by_code["liquid_handler"]["config"]["rendering"]["kind"]
        == "liquid-handler"
    )
    assert (
        by_code["liquid_handler"]["config"]["rendering"]["model"]["path"]
        == (
            "/api/v1/material-models/assets/devices/"
            "opentrons_liquid_handler/macro_device.xacro"
        )
    )
    assert (
        by_code["arm_slider"]["config"]["rendering"]["model"]["format"]
        == "xacro"
    )
    rotation_z = by_code["arm_slider"]["config"]["placement"]["pose"][
        "rotationDegXYZ"
    ][2]
    assert round(rotation_z) == -90


def test_material_list_filters_pages_and_detail_use_backend_contract() -> None:
    with _client("plr_test_converted.json") as client:
        filtered = client.get(
            "/api/v1/materials",
            params={"code": "RACK", "page": 1, "page_size": 1},
        )
        assert filtered.status_code == 200
        page = filtered.json()["data"]
        assert page["total"] == 1
        assert page["items"][0]["code"] == "tip_rack"

        material = page["items"][0]
        assert str(uuid.UUID(material["uuid"])) == material["uuid"]
        assert (
            str(uuid.UUID(material["resource_template_uuid"]))
            == material["resource_template_uuid"]
        )

        detail = client.get(f"/api/v1/materials/{material['uuid']}")
        assert detail.status_code == 200
        assert detail.json()["data"] == material

        by_template = client.get(
            "/api/v1/materials",
            params={
                "resource_template_uuid": (
                    material["resource_template_uuid"]
                ),
                "page_size": 100,
            },
        )
        assert by_template.status_code == 200
        assert by_template.json()["data"]["total"] >= 1


def test_registry_template_create_is_idempotent_and_compensatable() -> None:
    catalog = MaterialGraphCatalog(EXPERIMENT_ROOT / "plr_test.json")
    offline = OfflineOS()
    schedule = ScheduleSession(offline.receive, session_id="offline")
    offline.bind(schedule)

    def undo_create(material_uuid: str, command: dict) -> None:
        catalog.undo_create(
            material_uuid=material_uuid,
            creation_operation_id=command["creation_operation_id"],
            expected_revision=command["expected_revision"],
            idempotency_key=command["idempotency_key"],
        )

    state = LocalApiState(
        schedule,
        material_catalog=catalog,
        material_create=catalog.create_material,
        material_undo_create=undo_create,
    )
    template_uuid = "3b7372ea-c2b1-49f8-b910-f74738faf031"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(template_uuid)
        return httpx.Response(
            200,
            json=_create_plate_template(template_uuid),
            headers={"ETag": '"template-create-1"'},
            request=request,
        )

    proxy = ResourceTemplateProxy(
        "http://127.0.0.1:8002",
        transport=httpx.MockTransport(handler),
    )
    command = {
        "template_id": template_uuid,
        "name": "Registry Plate",
        "placement": {"kind": "unplaced"},
        "initial_contents": [],
        "expected_revision": catalog.list_materials(
            page_size=100
        )["items"][0]["revision"],
        "idempotency_key": "create-registry-plate",
    }

    with TestClient(create_app(lambda: state, proxy)) as client:
        created = client.post("/api/v1/materials", json=command)
        replay = client.post("/api/v1/materials", json=command)

        assert created.status_code == 200
        assert replay.status_code == 200
        result = created.json()["data"]
        assert replay.json()["data"] == result
        aggregate = result["aggregates"][0]
        assert aggregate["resource_template_uuid"] == template_uuid
        assert aggregate["name"] == "Registry Plate"
        assert aggregate["config"]["placement"] == {"kind": "unplaced"}
        assert len(aggregate["config"]["sites"]) == 4
        assert client.get(
            "/api/v1/materials?page_size=100"
        ).json()["data"]["total"] == 9

        undo_payload = {
            "creation_operation_id": result["creation_operation_id"],
            "expected_revision": aggregate["revision"],
            "idempotency_key": "undo-registry-plate",
        }
        undo_path = (
            f"/api/v1/materials/{result['primary_material_id']}"
            "/undo-create"
        )
        assert client.post(undo_path, json=undo_payload).status_code == 200
        assert client.post(undo_path, json=undo_payload).status_code == 200
        assert client.get(
            "/api/v1/materials?page_size=100"
        ).json()["data"]["total"] == 8


def test_material_query_reports_structured_errors_and_health_is_public() -> None:
    with _client("plr_test_converted.json") as client:
        invalid_page = client.get(
            "/api/v1/materials?page=0&page_size=101"
        )
        assert invalid_page.status_code == 400
        assert (
            invalid_page.json()["detail"]["code"]
            == "INVALID_MATERIAL_QUERY"
        )

        invalid_uuid = client.get("/api/v1/materials/not-a-uuid")
        assert invalid_uuid.status_code == 400
        assert (
            invalid_uuid.json()["detail"]["code"]
            == "INVALID_MATERIAL_UUID"
        )

        unknown_uuid = client.get(
            f"/api/v1/materials/{uuid.uuid4()}"
        )
        assert unknown_uuid.status_code == 404
        assert (
            unknown_uuid.json()["detail"]["code"]
            == "MATERIAL_NOT_FOUND"
        )

    unavailable = TestClient(create_app(lambda: None))
    assert unavailable.get("/health").json() == {"status": "ok"}
    assert unavailable.get("/api/v1/health").json() == {"status": "ok"}
    unavailable_materials = unavailable.get("/api/v1/materials")
    assert unavailable_materials.status_code == 503
    assert (
        unavailable_materials.json()["detail"]["code"]
        == "MATERIAL_GRAPH_UNAVAILABLE"
    )


def test_prcxi_deck_keeps_all_declared_slot_dimensions_and_coordinates() -> None:
    catalog = MaterialGraphCatalog(EXPERIMENT_ROOT / "prcxi_9320_slim.json")
    by_code = {
        item["code"]: item
        for item in catalog.list_materials(page_size=100)["items"]
    }
    deck = by_code["PRCXI_Deck"]
    sites = deck["config"]["sites"]

    assert deck["config"]["rendering"]["footprintMm"] == [542.0, 374.0]
    assert len(sites) == 16
    assert sites[0]["name"] == "T1"
    assert sites[0]["poseInAnchor"]["positionMm"] == [0.0, 0.0, 0.0]
    assert sites[0]["sizeMm"] == [128.0, 86.0, 0.0]
    assert sites[-1]["name"] == "T16"
    assert sites[-1]["poseInAnchor"]["positionMm"] == [
        414.0,
        288.0,
        0.0,
    ]


def test_prcxi_slot_wrappers_are_normalized_to_sites() -> None:
    catalog = MaterialGraphCatalog(
        EXPERIMENT_ROOT / "prcxi_9320_with_res_test.json"
    )
    page = catalog.list_materials(page_size=100)
    by_code = {item["code"]: item for item in page["items"]}

    assert page["total"] == 12
    assert not any(code.startswith("T") for code in by_code)
    deck = by_code["PRCXI_Deck"]
    assert len(deck["config"]["sites"]) == 16
    t1 = next(
        site for site in deck["config"]["sites"] if site["key"] == "T1"
    )
    assert t1["poseInAnchor"]["positionMm"] == [0.0, 288.0, 0.0]
    assert t1["sizeMm"] == [128.0, 86.0, 0.0]
    assert t1["visual"]["state"] == "occupied"
    assert by_code["RackT1"]["config"]["placement"] == {
        "kind": "site",
        "parentId": deck["uuid"],
        "siteId": t1["id"],
        "offsetPose": {
            "positionMm": [0.0, 0.0, 0.0],
            "rotationDegXYZ": [0.0, 0.0, 0.0],
        },
    }
    assert len(by_code["RackT1"]["config"]["sites"]) == 96
    assert by_code["PRCXI"]["config"]["rendering"]["footprintMm"] == [
        562.0,
        394.0,
    ]
    assert by_code["PRCXI"]["config"]["rendering"]["model"]["path"] == ""
    assert by_code["trash"]["config"]["rendering"]["model"]["path"] == ""


def test_local_models_are_registered_and_assets_are_served() -> None:
    with _client("plr_test_converted.json") as client:
        models = client.get("/api/v1/material-models")
        xacro = client.get(
            "/api/v1/material-models/assets/devices/"
            "opentrons_liquid_handler/macro_device.xacro"
        )
        mesh = client.get(
            "/api/v1/material-models/assets/devices/"
            "opentrons_liquid_handler/meshes/ot2-0.stl"
        )
        tip_mesh = client.get(
            "/api/v1/material-models/assets/resources/tip/meshes/tip.stl"
        )
        missing = client.get(
            "/api/v1/material-models/assets/devices/missing/model.urdf"
        )
        desktop_cors = client.get(
            "/api/v1/material-models/assets/devices/"
            "opentrons_liquid_handler/macro_device.xacro",
            headers={"Origin": "null"},
        )

    assert models.status_code == 200
    assert models.json()["data"]["total"] >= 5
    assert xacro.status_code == 200
    assert b"opentrons_liquid_handler" in xacro.content
    assert mesh.status_code == 200
    assert len(mesh.content) > 1000
    assert tip_mesh.status_code == 200
    assert len(tip_mesh.content) > 1000
    assert missing.status_code == 404
    assert desktop_cors.headers["access-control-allow-origin"] == "null"


def test_local_models_are_available_without_a_material_graph() -> None:
    offline = OfflineOS()
    schedule = ScheduleSession(
        offline.receive,
        session_id="material-models-without-graph",
    )
    offline.bind(schedule)
    state = LocalApiState(
        schedule,
        material_model_registry=MaterialModelRegistry(),
    )

    with TestClient(create_app(lambda: state)) as client:
        models = client.get("/api/v1/material-models")
        xacro = client.get(
            "/api/v1/material-models/assets/devices/"
            "arm_slider/macro_device.xacro"
        )
        materials = client.get("/api/v1/materials")

    assert models.status_code == 200
    assert models.json()["data"]["total"] >= 5
    assert xacro.status_code == 200
    assert materials.status_code == 503


def test_catalog_loads_offline_graph_once_then_uses_memory(
    tmp_path: Path,
) -> None:
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(
        (EXPERIMENT_ROOT / "plr_test_converted.json").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    catalog = MaterialGraphCatalog(graph_path)
    initial = catalog.list_materials(page_size=100)

    graph_path.write_text('{"nodes":[]}', encoding="utf-8")

    assert catalog.list_materials(page_size=100) == initial
    catalog.replace_snapshot(
        {
            "source_id": "replacement",
            "revision": initial["total"] + 1,
            "nodes": [],
        }
    )
    assert catalog.list_materials(page_size=100)["total"] == 0


def test_material_api_refreshes_from_os_current_resource_tree() -> None:
    payload = json.loads(
        (
            EXPERIMENT_ROOT / "plr_test_converted.json"
        ).read_text(encoding="utf-8")
    )
    resources = ResourceTreeSet.from_raw_dict_list(payload["nodes"])
    current = CurrentMaterialState(
        resources,
        source_id="plr_test_converted.json",
    )
    catalog = MaterialGraphCatalog()
    schedule: ScheduleSession

    async def send(message: dict) -> None:
        assert message["action"] == "query_material_snapshot"
        snapshot = current.snapshot()
        snapshot["request_id"] = message["data"]["request_id"]
        await schedule.handle_incoming(
            {"action": "material_snapshot", "data": snapshot}
        )

    schedule = ScheduleSession(send, session_id="material-memory")
    schedule.on_material_snapshot(catalog.replace_snapshot)
    state = LocalApiState(
        schedule,
        material_catalog=catalog,
        material_refresh=schedule.request_material_snapshot,
    )

    with TestClient(create_app(lambda: state)) as client:
        first = client.get("/api/v1/materials?page_size=100")
        assert first.status_code == 200
        first_rows = first.json()["data"]["items"]
        handler = next(
            item for item in first_rows if item["code"] == "liquid_handler"
        )
        first_revision = handler["revision"]

        node = next(
            instance
            for instance in resources.all_nodes
            if instance.res_content.id == "liquid_handler"
        )
        node.res_content.name = "OS memory renamed handler"

        second = client.get("/api/v1/materials?page_size=100")
        assert second.status_code == 200
        updated = next(
            item
            for item in second.json()["data"]["items"]
            if item["code"] == "liquid_handler"
        )

    assert updated["name"] == "OS memory renamed handler"
    assert updated["revision"] != first_revision


def test_os_schedule_client_answers_material_snapshot_query() -> None:
    payload = json.loads(
        (
            EXPERIMENT_ROOT / "plr_test_converted.json"
        ).read_text(encoding="utf-8")
    )
    resources = ResourceTreeSet.from_raw_dict_list(payload["nodes"])
    client = WebSocketClient()
    client.bind_material_state(
        resources,
        source_id="plr_test_converted.json",
    )

    asyncio.run(
        client.message_processor._process_message(  # noqa: SLF001
            "query_material_snapshot",
            {"request_id": "snapshot-request"},
        )
    )
    message = client.send_queue.get_nowait()

    assert message["action"] == "material_snapshot"
    assert message["data"]["request_id"] == "snapshot-request"
    assert message["data"]["schema"] == "unilab/material-snapshot-v1"
    assert message["data"]["nodes"]


def test_os_schedule_client_creates_and_compensates_resource_tree() -> None:
    payload = json.loads(
        (EXPERIMENT_ROOT / "plr_test.json").read_text(encoding="utf-8")
    )
    resources = ResourceTreeSet.from_raw_dict_list(payload["nodes"])
    client = WebSocketClient()
    client.bind_material_state(resources, source_id="plr_test.json")
    template_uuid = "3b7372ea-c2b1-49f8-b910-f74738faf031"
    command = {
        "template_id": template_uuid,
        "name": "Schedule Registry Plate",
        "placement": {"kind": "unplaced"},
        "initial_contents": [],
        "expected_revision": CurrentMaterialState(
            resources,
            source_id="plr_test.json",
        ).snapshot()["revision"],
        "idempotency_key": "schedule-create-plate",
    }

    asyncio.run(
        client.message_processor._process_message(  # noqa: SLF001
            "create_material",
            {
                "request_id": "create-request",
                "template": _create_plate_template(template_uuid),
                "command": command,
            },
        )
    )
    created = client.send_queue.get_nowait()

    assert created["action"] == "material_create_ack"
    create_data = created["data"]
    assert create_data["status"] == "created"
    assert any(
        node["name"] == "Schedule Registry Plate"
        for node in create_data["snapshot"]["nodes"]
    )

    asyncio.run(
        client.message_processor._process_message(  # noqa: SLF001
            "undo_create_material",
            {
                "request_id": "undo-request",
                "command": {
                    "source_node_id": create_data["source_node_id"],
                    "creation_operation_id": create_data[
                        "creation_operation_id"
                    ],
                    "expected_revision": create_data["snapshot"][
                        "revision"
                    ],
                    "idempotency_key": "schedule-undo-plate",
                },
            },
        )
    )
    undone = client.send_queue.get_nowait()

    assert undone["action"] == "material_undo_create_ack"
    assert undone["data"]["status"] == "undone"
    assert all(
        node["name"] != "Schedule Registry Plate"
        for node in undone["data"]["snapshot"]["nodes"]
    )


def _create_plate_template(template_uuid: str) -> dict:
    return {
        "uuid": template_uuid,
        "key": "plate-4",
        "source_namespace": "unilabos",
        "kind": "resource",
        "display_name": "四孔板",
        "status": "ready",
        "content_hash": "template-create-1",
        "creation": {
            "mode": "resource-tree",
            "available": True,
            "reason": None,
        },
        "geometry": {
            "dimensions_mm": {"x": 127, "y": 85, "z": 15},
        },
        "container_layout": {
            "type": "grid",
            "container_kind": "well",
            "rows": ["A", "B"],
            "columns": 2,
            "column_labels": [1, 2],
            "geometry": {
                "dimensions_mm": {"x": 8, "y": 8, "z": 10},
                "depth_mm": 10,
                "shape": "circle",
                "max_volume_ul": 200,
                "pitch_mm": {"x": 9, "y": -9},
                "offset_mm": {"x": 10, "y": 20, "z": 2},
                "first_key": "A1",
            },
        },
    }
