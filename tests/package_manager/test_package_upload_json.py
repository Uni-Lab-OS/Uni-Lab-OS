"""Electron 复用 `package upload --json` 所需的稳定最终输出合同测试。"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from unilabos.app.main import parse_args
from unilabos.package_manager import cli


def test_package_upload_parser_accepts_final_json_flag() -> None:
    """上传子命令必须接受 Electron 需要的最终 JSON 标志。"""

    parser = parse_args()
    parsed = vars(
        parser.parse_args(
            ["package", "upload", "--path", "/workspace/package", "--json"]
        )
    )

    assert parsed["package_action"] == "upload"
    assert parsed["package_json"] is True


def test_package_upload_json_reports_published_identity(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """上传完成后只输出可供 Main 复核的发布身份与内容摘要。"""

    artifact = SimpleNamespace(
        wheel=tmp_path / "review_lab-1.2.0-py3-none-any.whl",
        artifact_digest="sha256:" + "a" * 64,
        catalog=SimpleNamespace(
            namespace="community.review_lab",
            catalog_digest="sha256:" + "b" * 64,
            distribution=SimpleNamespace(name="review-lab", version="1.2.0"),
        ),
    )
    monkeypatch.setattr(cli, "build_workspace_wheel", lambda *_args: artifact)
    monkeypatch.setattr(cli, "publish_build", lambda *_args, **_kwargs: object())
    output = StringIO()

    cli.run_package_command(
        {
            "package_action": "upload",
            "package_path": str(tmp_path),
            "package_json": True,
        },
        http_client=object(),
        stream=output,
    )

    assert json.loads(output.getvalue()) == {
        "status": "published",
        "distribution": "review-lab",
        "version": "1.2.0",
        "namespace": "community.review_lab",
        "catalog_digest": "sha256:" + "b" * 64,
        "artifact_digest": "sha256:" + "a" * 64,
    }
