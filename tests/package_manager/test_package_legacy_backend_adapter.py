"""遗留设备广场包协议 HTTP Adapter 合同。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


class _Response:
    """提供 requests 响应最小测试形状。"""

    def __init__(
        self,
        status_code: int,
        payload: Any = None,
        *,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
    ) -> None:
        """固定状态、JSON、响应头和流式内容。

        参数：各字段对应 requests 响应的同名可观察值。
        返回：无。
        异常：无。
        """

        self.status_code = status_code
        self.payload = payload
        self.headers = headers or {}
        self.body = body

    def json(self) -> Any:
        """返回固定 JSON 载荷。

        参数：无。
        返回：构造时载荷。
        异常：无。
        """

        return self.payload

    def iter_content(self, chunk_size: int):
        """按调用方块大小返回固定二进制内容。

        参数：``chunk_size`` 是最大块长。
        返回：字节迭代器。
        异常：无。
        """

        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset : offset + chunk_size]

    def __enter__(self):
        """作为流式响应上下文返回自身。

        参数：无。
        返回：自身。
        异常：无。
        """

        return self

    def __exit__(self, *_args: object) -> bool:
        """退出流式响应上下文且不吞异常。

        参数：``_args`` 是上下文异常三元组。
        返回：固定 ``False``。
        异常：无。
        """

        return False


class _Session:
    """按顺序返回响应并记录 Backend 请求。"""

    def __init__(self, responses: list[_Response]) -> None:
        """保存响应队列和空调用记录。

        参数：``responses`` 是后续请求顺序。
        返回：无。
        异常：无。
        """

        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.get_calls: list[tuple[str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> _Response:
        """记录普通 Backend 请求并弹出下一响应。

        参数：``method``、``url`` 与 ``kwargs`` 是 Adapter 请求。
        返回：下一固定响应。
        异常：响应耗尽时测试列表抛出 ``IndexError``。
        """

        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)

    def get(self, url: str, **kwargs: Any) -> _Response:
        """记录公开下载 route 请求并弹出下一响应。

        参数：``url`` 和 ``kwargs`` 是请求字段。
        返回：下一固定响应。
        异常：响应耗尽时抛出 ``IndexError``。
        """

        self.get_calls.append((url, kwargs))
        return self.responses.pop(0)


def _package_info(*, artifact: str = "a") -> dict[str, Any]:
    """生成一个完整遗留 ``package_info`` fixture。

    参数：``artifact`` 是 Artifact 十六进制填充值。
    返回：三摘要和发行身份完整的字典。
    异常：无。
    """

    return {
        "name": "catalog-lab",
        "normalized_name": "catalog_lab",
        "version": "1.2.3",
        "class_namespace": "community.catalog_lab",
        "artifact_digest": "sha256:" + artifact * 64,
        "sha256": "sha256:" + artifact * 64,
        "catalog_digest": "sha256:" + "b" * 64,
        "content_digest": "sha256:" + "c" * 64,
    }


def _detail(template_uuid: str, *, artifact: str = "a", source: str = "lab:Device"):
    """生成公开模板详情成功信封。

    参数：``template_uuid`` 是模板身份；``artifact`` 是发布代；``source`` 是
    Python 源码身份。
    返回：HTTP 200 测试响应。
    异常：无。
    """

    return _Response(
        200,
        {
            "code": 0,
            "data": {
                "uuid": template_uuid,
                "package_info": _package_info(artifact=artifact),
                "source_registry": {"source_fqid": source},
            },
        },
    )


def test_legacy_upload_uses_file_scene_octet_stream_and_business_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上传必须使用 file scene、octet-stream、文件流且检查业务码。

    参数：``tmp_path`` 提供 wheel；``monkeypatch`` 替代裸 OSS PUT。
    返回：无；断言 OSS 不携带 Lab 头，Backend 使用同一固定根。
    异常：协议字段漂移或业务错误误报成功时测试失败。
    """

    from unilabos.package_manager.package_distribution.adapters import (
        legacy_backend as module,
    )
    from unilabos.package_manager.package_distribution.adapters.legacy_backend import (
        LegacyTemplateBackendAdapter,
    )

    wheel = tmp_path / "catalog_lab-1.2.3.whl"
    wheel.write_bytes(b"wheel-bytes")
    session = _Session(
        [
            _Response(200, {"code": 0, "data": {"data": []}}),
            _Response(
                200,
                {
                    "code": 0,
                    "data": {
                        "url": "https://oss.example/signed",
                        "path": "file/packages/object.whl",
                        "public_url": "https://oss.example/object.whl",
                        "content_type": "application/octet-stream",
                    },
                },
            ),
            _Response(200, {"code": 0, "data": None}),
        ]
    )
    put_calls: list[dict[str, Any]] = []

    def fake_put(_url: str, **kwargs: Any) -> _Response:
        """读取文件流并记录裸 PUT 字段。

        参数：``_url`` 是签名地址；``kwargs`` 是 requests PUT 参数。
        返回：HTTP 200 响应。
        异常：若上传体不是文件流则测试断言失败。
        """

        assert kwargs["data"].read() == b"wheel-bytes"
        put_calls.append(kwargs)
        return _Response(200)

    monkeypatch.setattr(module.requests, "put", fake_put)
    adapter = LegacyTemplateBackendAdapter(
        "https://leap-lab.uat.bohrium.com/api/v1",
        auth_secret="encoded-secret",
        session=session,
    )

    assert adapter.probe() == "legacy-template-package/v1"
    adapter.upload_release_artifact(
        wheel,
        normalized_name="catalog_lab",
        version="1.2.3",
    )
    adapter.publish_resources([{"id": "reader"}], {"name": "catalog-lab"})

    token_call = session.calls[1][2]
    assert token_call["params"]["scene"] == "file"
    assert token_call["params"]["content_type"] == "application/octet-stream"
    assert token_call["headers"]["Authorization"] == "Lab encoded-secret"
    assert put_calls[0]["headers"] == {"Content-Type": "application/octet-stream"}
    assert put_calls[0]["allow_redirects"] is False
    assert session.calls[2][2]["headers"]["Content-Encoding"] == "gzip"


def test_legacy_resource_http_200_with_nonzero_code_fails() -> None:
    """模板发布 HTTP 200 但业务码非零时必须关闭式失败。

    参数：无。
    返回：无；断言稳定 ``remote_business_error``。
    异常：若业务错误被接受则测试失败。
    """

    from unilabos.package_manager.package_distribution.adapters.legacy_backend import (
        LegacyTemplateBackendAdapter,
    )
    from unilabos.package_manager.package_distribution.errors import (
        PackageTransferError,
    )

    adapter = LegacyTemplateBackendAdapter(
        "https://leap-lab.uat.bohrium.com/api/v1",
        auth_secret="encoded-secret",
        session=_Session([_Response(200, {"code": 17, "error": {"msg": "secret"}})]),
    )

    with pytest.raises(PackageTransferError) as caught:
        adapter.publish_resources([{"id": "reader"}], {"name": "catalog-lab"})

    assert caught.value.code == "remote_business_error"
    assert "secret" not in str(caught.value)


def test_legacy_download_strips_auth_and_cookie_on_single_redirect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """公开 302 跳转到 OSS 时不得转发 Lab Authorization 或 Cookie。

    参数：``tmp_path`` 提供下载临时文件；``monkeypatch`` 注入裸 OSS 会话。
    返回：无；断言最多一次 302、HTTPS 和流式内容。
    异常：请求头或下载行为不安全时测试失败。
    """

    from unilabos.package_manager.package_distribution.adapters import (
        legacy_backend as module,
    )
    from unilabos.package_manager.package_distribution.adapters.legacy_backend import (
        LegacyTemplateBackendAdapter,
    )

    template_uuid = "11e27cf5-3ec8-4cfb-bb17-db941426e94e"
    backend_session = _Session(
        [_Response(302, headers={"Location": "https://oss.example/signed?token=x"})]
    )

    class BareSession:
        """记录 OSS 跳转请求的无状态会话。"""

        def __init__(self) -> None:
            """初始化空调用记录。

            参数：无。
            返回：无。
            异常：无。
            """

            self.calls: list[dict[str, Any]] = []

        def get(self, _url: str, **kwargs: Any) -> _Response:
            """记录 OSS GET 并返回 wheel 字节。

            参数：``_url`` 是签名地址；``kwargs`` 是安全请求字段。
            返回：HTTP 200 流式响应。
            异常：无。
            """

            self.calls.append(kwargs)
            return _Response(200, headers={"Content-Length": "5"}, body=b"wheel")

        def close(self) -> None:
            """关闭测试会话。

            参数：无。
            返回：无。
            异常：无。
            """

    bare = BareSession()
    monkeypatch.setattr(module.requests, "Session", lambda: bare)
    adapter = LegacyTemplateBackendAdapter(
        "https://leap-lab.uat.bohrium.com/api/v1",
        auth_secret="must-not-leak",
        session=backend_session,
    )
    target = tmp_path / "download.tmp"

    adapter.download_artifact(template_uuid, target)

    assert target.read_bytes() == b"wheel"
    assert "headers" not in backend_session.get_calls[0][1]
    assert bare.calls[0]["headers"] == {"Accept": "application/octet-stream"}
    assert bare.calls[0]["allow_redirects"] is False


def test_legacy_package_resolution_rejects_mixed_artifacts() -> None:
    """同包模板指向多个 Artifact 时包名选择必须失败关闭。

    参数：无。
    返回：无；断言 ``remote_package_ambiguous`` 且不会任取第一项。
    异常：若混合发布被合并则测试失败。
    """

    from unilabos.package_manager.package_distribution import PackageDownloadRequest
    from unilabos.package_manager.package_distribution.adapters.legacy_backend import (
        LegacyTemplateBackendAdapter,
    )
    from unilabos.package_manager.package_distribution.errors import (
        PackageTransferError,
    )

    first_uuid = "11e27cf5-3ec8-4cfb-bb17-db941426e94e"
    second_uuid = "21e27cf5-3ec8-4cfb-bb17-db941426e94e"
    package_detail = _Response(
        200,
        {
            "code": 0,
            "data": {
                "device_count": 2,
                "devices": [{"uuid": first_uuid}, {"uuid": second_uuid}],
            },
        },
    )
    adapter = LegacyTemplateBackendAdapter(
        "https://leap-lab.uat.bohrium.com/api/v1",
        session=_Session(
            [
                package_detail,
                _detail(first_uuid, artifact="a", source="lab:First"),
                _detail(second_uuid, artifact="d", source="lab:Second"),
            ]
        ),
    )

    with pytest.raises(PackageTransferError) as caught:
        adapter.resolve(PackageDownloadRequest(package_name="catalog-lab"))

    assert caught.value.code == "remote_package_ambiguous"
