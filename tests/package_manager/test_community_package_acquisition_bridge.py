"""遗留启动期社区包必须委托统一可信 acquisition 的合同。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar


class _RecordingAdapter:
    """记录启动兼容桥构造和关闭的 Backend Adapter 替身。"""

    instances: ClassVar[list[_RecordingAdapter]] = []

    def __init__(self, base_url: str) -> None:
        """保存固定 Backend 根并注册实例。

        参数：``base_url`` 是兼容桥解析出的 API 根。
        返回：无。
        异常：无。
        """

        self.base_url = base_url
        self.closed = False
        self.instances.append(self)

    def close(self) -> None:
        """记录短生命周期 Adapter 已关闭。

        参数：无。
        返回：无。
        异常：无。
        """

        self.closed = True


class _CommunityClient:
    """只提供当前产品 Backend 根的最小旧客户端替身。"""

    remote_addr = "https://leap-lab.uat.bohrium.com/api/v1"


def test_legacy_community_cache_delegates_to_verified_acquisition(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """启动链不得使用 resolve 的签名 URL，并只提交派生工作区。

    参数：``tmp_path`` 隔离两层缓存；``monkeypatch`` 注入远端获取测试端口。
    返回：无；断言选择器、环境、缓存、原子目录和脱敏元数据合同。
    异常：若旧直链解压仍被调用或身份字段丢失则测试失败。
    """

    from unilabos.app import community_package_acquisition as bridge
    from unilabos.app.community_packages import _ensure_remote_item_cached

    digest = "sha256:" + "1" * 64
    catalog_digest = "sha256:" + "2" * 64
    content_digest = "sha256:" + "3" * 64
    observed: dict[str, Any] = {}

    def fake_acquire_package(
        request: Any,
        *,
        port: Any,
        cache: Any,
        environment: str,
        compile_catalog: Any,
        extract_source: Path,
    ) -> dict[str, Any]:
        """模拟统一获取成功并只写调用方授权的派生工作区。

        参数：与 ``acquire_package`` 深接口一致。
        返回：稳定可信下载结果。
        异常：无。
        """

        observed.update(
            {
                "request": request,
                "port": port,
                "cache_root": cache.root,
                "environment": environment,
                "compiler": compile_catalog,
            }
        )
        extract_source.mkdir(parents=True)
        extract_source.joinpath("pyproject.toml").write_text(
            "[project]\nname='bridge-package'\nversion='1.2.3'\n",
            encoding="utf-8",
        )
        extract_source.joinpath("bridge_package").mkdir()
        extract_source.joinpath("bridge_package/__init__.py").write_text(
            "", encoding="utf-8"
        )
        return {
            "version": "1.2.3",
            "artifact_digest": digest,
            "catalog_digest": catalog_digest,
            "content_digest": content_digest,
            "cache_key": f"community.bridge_package@1.2.3#{digest}",
        }

    _RecordingAdapter.instances.clear()
    monkeypatch.setattr(bridge, "LegacyTemplateBackendAdapter", _RecordingAdapter)
    monkeypatch.setattr(bridge, "acquire_package", fake_acquire_package)
    manifest: dict[str, Any] = {"packages": {}}
    package_dir = _ensure_remote_item_cached(
        {
            "class_namespace": "community.bridge_package",
            "package_info": {
                "name": "bridge-package",
                "normalized_name": "bridge-package",
                "version": "1.2.3",
                "class_namespace": "community.bridge_package",
                "artifact_digest": digest,
                "dependencies": ["example>=1"],
                "download_url": "https://signed.example/secret?token=hidden",
                "oss_object_key": "secret-object-key",
            },
        },
        tmp_path,
        manifest,
        http_client=_CommunityClient(),
    )

    assert package_dir is not None and package_dir.is_dir()
    assert observed["request"].package_name == "bridge-package"
    assert observed["request"].version == "1.2.3"
    assert observed["environment"] == "uat"
    assert observed["cache_root"] == (tmp_path / "package-cache" / "v1").resolve()
    assert _RecordingAdapter.instances[0].closed is True
    cached = manifest["packages"]["community.bridge_package"]
    assert cached["acquisition"] == "package-cache/v1"
    assert "download_url" not in cached
    persisted = json.loads(package_dir.joinpath("package_info.json").read_text())
    assert "download_url" not in persisted
    assert "oss_object_key" not in persisted
    assert persisted["artifact_digest"] == digest
