"""Backend Authority bootstrap pagination contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from unilabos.workspace_host.authority_sync import BackendAuthorityBootstrapper


class _Response:
    status_code = 200

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def json(self) -> dict[str, Any]:
        return {"code": 0, "data": self._data}


class _Session:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.params: list[dict[str, object]] = []

    def get(
        self,
        _url: str,
        *,
        params: dict[str, object],
        timeout: float,
    ) -> _Response:
        del timeout
        self.params.append(dict(params))
        return _Response(self.pages[len(self.params) - 1])


def _bootstrapper(session: _Session) -> BackendAuthorityBootstrapper:
    return BackendAuthorityBootstrapper(
        "http://127.0.0.1:62000",
        "http://127.0.0.1:8080",
        "test-token",
        session=session,  # type: ignore[arg-type]
    )


def test_source_templates_advances_current_numbered_contract() -> None:
    session = _Session([
        {
            "items": [{"uuid": "template-1"}],
            "page": 1,
            "page_size": 100,
            "has_more": True,
        },
        {
            "items": [{"uuid": "template-2"}],
            "page": 2,
            "page_size": 100,
            "has_more": False,
        },
    ])

    assert _bootstrapper(session)._source_templates() == [
        {"uuid": "template-1"},
        {"uuid": "template-2"},
    ]
    assert session.params == [
        {"page": 1, "page_size": 100},
        {"page": 2, "page_size": 100},
    ]


def test_source_templates_advances_compact_numbered_contract() -> None:
    session = _Session([
        {"items": [{"uuid": "template-1"}], "has_more": True},
        {"items": [{"uuid": "template-2"}], "has_more": False},
    ])

    assert _bootstrapper(session)._source_templates() == [
        {"uuid": "template-1"},
        {"uuid": "template-2"},
    ]
    assert session.params == [
        {"page": 1, "page_size": 100},
        {"page": 2, "page_size": 100},
    ]


def test_source_templates_keeps_legacy_uuid_cursor_compatibility() -> None:
    session = _Session([
        {
            "items": [{"uuid": "template-1"}],
            "has_more": True,
            "next_cursor_uuid": "template-1",
        },
        {
            "items": [{"uuid": "template-2"}],
            "has_more": False,
            "next_cursor_uuid": None,
        },
    ])

    assert _bootstrapper(session)._source_templates() == [
        {"uuid": "template-1"},
        {"uuid": "template-2"},
    ]
    assert session.params == [
        {"page": 1, "page_size": 100},
        {"limit": 100, "cursor_uuid": "template-1"},
    ]


def test_bootstrap_reuses_matching_workspace_release_materials(
    tmp_path: Path,
) -> None:
    class ReleaseSession:
        def get(self, url: str, **_kwargs: object) -> _Response:
            assert url.endswith("/materials")
            return _Response({
                "items": [{
                    "meta_data": {
                        "source_node_id": "deck",
                        "unilab_release": {
                            "source_workspace": str(tmp_path.resolve()),
                        },
                    },
                }],
                "total": 1,
                "page": 1,
                "page_size": 100,
            })

        def post(self, *_args: object, **_kwargs: object) -> _Response:
            raise AssertionError("released target must not be initialized again")

    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps({
        "nodes": [{"id": "deck", "class": "device.deck"}],
    }))
    bootstrapper = BackendAuthorityBootstrapper(
        "http://127.0.0.1:62000",
        "http://127.0.0.1:8080",
        "test-token",
        source_workspace=tmp_path,
        session=ReleaseSession(),  # type: ignore[arg-type]
    )

    report = bootstrapper.bootstrap(graph_path)

    assert report.template_count == 0
    assert report.created_material_count == 0
    assert report.existing_material_count == 1
