import base64
from pathlib import Path

import httpx
import pytest

from unilabos.client.material_renderer import (
    MaterialRendererClient,
    MaterialRendererClientError,
)


def test_discovers_the_single_attached_renderer_through_workspace_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Host:
        token = "workspace-secret"

        def snapshot(self):
            return {
                "workspacePath": str(tmp_path),
                "components": {
                    "renderer": {
                        "phase": "ready",
                        "generation": "renderer-7",
                        "capabilities": [
                            "material-scene-inspect",
                            "material-scene-capture",
                        ],
                        "metadata": {
                            "automationBaseUrl": (
                                "http://127.0.0.1:3100/__unilab_renderer/v1"
                            ),
                            "automationContract": "unilab-material-renderer/v1",
                        },
                    }
                },
            }

    monkeypatch.setattr(
        "unilabos.client.material_renderer.ensure_workspace_host", lambda _path: Host()
    )

    client = MaterialRendererClient.discover(tmp_path)
    assert client.base_url.endswith("/__unilab_renderer/v1")
    assert client.renderer_generation == "renderer-7"
    client.close()


def test_capture_writes_valid_png_and_removes_base64_from_result(tmp_path: Path) -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"payload"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret"
        return httpx.Response(
            200,
            json={
                "schemaVersion": "unilab-material-renderer/v1",
                "requestId": "request-1",
                "ok": True,
                "result": {
                    "schemaVersion": "unilab-material-capture/v1",
                    "scene": {"layoutRevision": "revision-1"},
                    "image": {
                        "mimeType": "image/png",
                        "width": 800,
                        "height": 600,
                        "base64": base64.b64encode(png).decode(),
                    },
                },
            },
        )

    output = tmp_path / "scene.png"
    with MaterialRendererClient(
        "http://127.0.0.1:3100/__unilab_renderer/v1",
        "secret",
        workspace_path=str(tmp_path),
        renderer_generation="renderer-1",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = client.capture_scene(output, viewport=(800, 600))

    assert output.read_bytes() == png
    assert result["data"]["image"]["path"] == str(output)
    assert result["data"]["image"]["bytes"] == len(png)
    assert "base64" not in result["data"]["image"]


def test_fails_closed_when_renderer_is_not_attached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Host:
        token = "secret"

        def snapshot(self):
            return {"components": {"renderer": {"phase": "idle"}}}

    monkeypatch.setattr(
        "unilabos.client.material_renderer.ensure_workspace_host", lambda _path: Host()
    )
    with pytest.raises(MaterialRendererClientError) as caught:
        MaterialRendererClient.discover(tmp_path)
    assert caught.value.code == "material_renderer_not_attached"
