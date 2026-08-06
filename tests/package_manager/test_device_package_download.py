"""云端设备包下载、缓存与配置描述的 P1 合同测试。"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from unilabos.app.main import parse_args
from unilabos.package_manager.cli import run_package_command
from unilabos.package_manager.device_package import (
    DevicePackageError,
    download_device_package,
)
from unilabos.package_manager.distribution import BuildArtifact, build_workspace_wheel

_TEMPLATE_UUID = "50afbb58-0f53-4ad6-9f73-24cfeb90a834"
_DEFINITION_FQID = "community.review_lab.pump"


class _CopyDownloadPort:
    """把测试 wheel 复制到下载目标并记录请求 URL。"""

    def __init__(self, wheel: Path) -> None:
        """保存唯一允许下载的本地测试 wheel。"""

        self._wheel = wheel
        self.urls: list[str] = []

    def download(self, url: str, destination: Path) -> None:
        """记录 URL 后复制 Artifact，模拟 Backend 302 后的 OSS 响应。"""

        self.urls.append(url)
        destination.write_bytes(self._wheel.read_bytes())


class _RejectDownloadPort:
    """确保缓存命中路径不会再次访问网络。"""

    def download(self, url: str, destination: Path) -> None:
        """任何下载调用都立即失败，以证明缓存命中是无网络副作用的。"""

        del url, destination
        raise AssertionError("缓存命中后不得再次下载")


def _build_device_artifact(
    root: Path,
    *,
    init_signature: str = "endpoint: str, retries: int = 3, enabled: bool = False",
) -> BuildArtifact:
    """构建包含嵌入式 PackageCatalog 的最小测试设备 wheel。"""

    workspace = root / "workspace"
    package = workspace / "review_lab"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "device.py").write_text(
        (
            "from unilabos.registry.decorators import device\n\n"
            "@device(id=\"pump\", category=[\"test\"])\n"
            "class Pump:\n"
            f"    def __init__(self, {init_signature}):\n"
            "        self.endpoint = endpoint\n"
        ),
        encoding="utf-8",
    )
    (workspace / "pyproject.toml").write_text(
        """
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "review-lab"
version = "1.2.0"

[tool.setuptools.packages.find]
include = ["review_lab*"]
""".strip(),
        encoding="utf-8",
    )
    return build_workspace_wheel(workspace, root / "dist")


def _download_kwargs(
    tmp_path: Path,
    artifact: BuildArtifact,
    port: Any,
) -> dict[str, Any]:
    """生成每个测试共用的公开下载输入，避免身份字段漂移。"""

    return {
        "template_uuid": _TEMPLATE_UUID,
        "definition_fqid": _DEFINITION_FQID,
        "artifact_digest": artifact.artifact_digest,
        "backend_base_url": "https://backend.example/api/v1/",
        "working_dir": str(tmp_path / "runtime"),
        "port": port,
    }


def test_download_device_package_caches_wheel_and_returns_configuration_schema(
    tmp_path: Path,
) -> None:
    """首次下载必须校验并缓存 wheel，同时返回目标设备的固定配置 Schema。"""

    artifact = _build_device_artifact(tmp_path)
    port = _CopyDownloadPort(artifact.wheel)

    result = download_device_package(**_download_kwargs(tmp_path, artifact, port))

    assert result.cache_hit is False
    assert result.cache_key == (
        f"community.review_lab@1.2.0#{artifact.artifact_digest}"
    )
    assert result.configuration_schema == {
        "type": "object",
        "required": ["endpoint"],
        "properties": {
            "endpoint": {"type": "string"},
            "retries": {"type": "integer", "default": 3},
            "enabled": {"type": "boolean", "default": False},
        },
        "additionalProperties": False,
    }
    assert port.urls == [
        "https://backend.example/api/v1/lab/square/packages/releases/"
        f"{_TEMPLATE_UUID}/download"
    ]
    index = json.loads(
        (tmp_path / "runtime/community_packages/cache-index.json").read_text(
            encoding="utf-8"
        )
    )
    assert index["packages"]["community.review_lab"]["artifact_digest"] == (
        artifact.artifact_digest
    )
    assert not (tmp_path / "runtime/device-graph.json").exists()


def test_download_device_package_reuses_verified_cache_without_network(
    tmp_path: Path,
) -> None:
    """相同 namespace 与摘要的第二次请求必须幂等命中受管缓存。"""

    artifact = _build_device_artifact(tmp_path)
    download_device_package(
        **_download_kwargs(tmp_path, artifact, _CopyDownloadPort(artifact.wheel))
    )

    result = download_device_package(
        **_download_kwargs(tmp_path, artifact, _RejectDownloadPort())
    )

    assert result.cache_hit is True
    assert result.definition_fqid == _DEFINITION_FQID


def test_download_device_package_rejects_missing_definition_before_publish(
    tmp_path: Path,
) -> None:
    """Catalog 不含目标设备时不得更新可供 Runtime 解析的缓存索引。"""

    artifact = _build_device_artifact(tmp_path)
    kwargs = _download_kwargs(tmp_path, artifact, _CopyDownloadPort(artifact.wheel))
    kwargs["definition_fqid"] = "community.review_lab.balance"

    with pytest.raises(DevicePackageError, match="不存在或不唯一"):
        download_device_package(**kwargs)

    assert not (
        tmp_path / "runtime/community_packages/cache-index.json"
    ).exists()


def test_download_device_package_rejects_artifact_digest_mismatch(
    tmp_path: Path,
) -> None:
    """下载字节与云端摘要不一致时不得产生缓存索引或可用包身份。"""

    artifact = _build_device_artifact(tmp_path)
    kwargs = _download_kwargs(tmp_path, artifact, _CopyDownloadPort(artifact.wheel))
    kwargs["artifact_digest"] = f"sha256:{'0' * 64}"

    with pytest.raises(DevicePackageError, match="artifact digest mismatch"):
        download_device_package(**kwargs)

    assert not (
        tmp_path / "runtime/community_packages/cache-index.json"
    ).exists()


def test_download_device_package_projects_secret_configuration_without_plaintext_default(
    tmp_path: Path,
) -> None:
    """秘密初始化参数必须进入写入专用合同，不能阻断设备包下载。"""

    artifact = _build_device_artifact(
        tmp_path,
        init_signature="endpoint: str, password: str = 'unsafe-default'",
    )

    result = download_device_package(
        **_download_kwargs(tmp_path, artifact, _CopyDownloadPort(artifact.wheel))
    )

    assert result.configuration_schema["properties"]["password"] == {
        "type": "string",
        "writeOnly": True,
        "x-unilab-secret": True,
    }
    assert "default" not in result.configuration_schema["properties"]["password"]
    assert (
        tmp_path / "runtime/community_packages/cache-index.json"
    ).exists()


def test_package_download_cli_emits_one_final_json_document(tmp_path: Path) -> None:
    """CLI 必须解析文档约定参数并输出一行可机器解码的最终 JSON。"""

    artifact = _build_device_artifact(tmp_path)
    parser = parse_args()
    args = vars(
        parser.parse_args(
            [
                "--working_dir",
                str(tmp_path / "runtime"),
                "--addr",
                "https://backend.example/api/v1",
                "package",
                "download",
                "--template-uuid",
                _TEMPLATE_UUID,
                "--definition-fqid",
                _DEFINITION_FQID,
                "--artifact-digest",
                artifact.artifact_digest,
                "--json",
            ]
        )
    )
    output = StringIO()

    result = run_package_command(
        args,
        download_port=_CopyDownloadPort(artifact.wheel),
        stream=output,
    )

    lines = output.getvalue().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == result.to_dict()
    assert json.loads(lines[0])["status"] == "package_cached"
