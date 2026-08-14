"""HTTP 必须在工作流冷启动完成前对外监听。"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request

from fastapi.testclient import TestClient

from unilabos.app.web import server as web_server


def test_start_server_listens_before_workflow_runtime_mount_finishes(
    monkeypatch,
) -> None:
    """工作流源码固定点装配再慢，/status 也不能被挡住。"""

    entered = threading.Event()
    release = threading.Event()

    def blocking_mount() -> None:
        entered.set()
        assert release.wait(timeout=30)

    monkeypatch.setattr(web_server, "_mount_workflow_runtime", blocking_mount)
    monkeypatch.setattr(web_server, "workflow_routes_mounted", False)
    monkeypatch.setattr(web_server, "workflow_routes_mount_error", None)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()

    server_thread = threading.Thread(
        target=web_server.start_server,
        kwargs={"host": "127.0.0.1", "port": port, "open_browser": False},
        daemon=True,
        name="test_unilab_http",
    )
    server_thread.start()

    assert entered.wait(timeout=20)
    deadline = time.time() + 15
    last_error = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/openapi.json",
                timeout=2,
            ) as response:
                assert response.status == 200
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/v1/workflows",
                    timeout=2,
                )
            except urllib.error.HTTPError as error:
                assert error.code == 503
                payload = json.loads(error.read().decode())
                assert payload["error"]["code"] == "workflow_runtime_mounting"
                assert payload["error"]["retryable"] is True
            else:
                raise AssertionError("装配完成前工作流目录必须返回 503")
            release.set()
            return
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            last_error = str(error)
            time.sleep(0.1)
    release.set()
    raise AssertionError(f"HTTP 在工作流装配完成前未监听: {last_error}")


def test_workflow_catalog_returns_retryable_503_before_runtime_mounts(
    monkeypatch,
) -> None:
    """目录页在工作流合同挂载前必须看到可重试 503，而不是 404。"""

    monkeypatch.setattr(web_server, "workflow_routes_mounted", False)
    monkeypatch.setattr(web_server, "workflow_routes_mount_error", None)
    client = TestClient(web_server.app)

    response = client.get("/api/v1/workflows")
    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "workflow_runtime_mounting",
        "message": "工作流目录仍在装配，请稍后重试",
        "retryable": True,
    }
    assert client.get("/api/openapi.json").status_code == 200


def test_failed_workflow_runtime_mount_is_not_retryable(monkeypatch) -> None:
    """装配失败后目录请求停止伪装成冷启动。"""

    monkeypatch.setattr(web_server, "workflow_routes_mounted", False)
    monkeypatch.setattr(
        web_server,
        "workflow_routes_mount_error",
        "compose_workflow_runtime exploded",
    )

    response = TestClient(web_server.app).get("/api/v1/workflows")
    assert response.status_code == 503
    payload = response.json()["error"]
    assert payload["code"] == "workflow_runtime_unavailable"
    assert payload["retryable"] is False
    assert "compose_workflow_runtime exploded" in payload["message"]
