"""设备包上传一次性凭据和最小 HTTP 传输的安全合同测试。"""

from __future__ import annotations

import base64
import importlib
import json
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from unilabos.package_manager.publication_http import PackagePublicationHttpClient
from unilabos.package_manager.upload_auth import (
    PackageUploadAuthError,
    read_package_upload_credentials,
)

main_module = importlib.import_module("unilabos.app.main")
secure_upload_module = importlib.import_module(
    "unilabos.package_manager.secure_upload"
)


def test_upload_credentials_are_read_from_closed_stdin_contract() -> None:
    """stdin 只接受版本化 AK/SK JSON，并生成现有 Lab Authorization 载荷。"""

    credentials = read_package_upload_credentials(
        StringIO(
            json.dumps(
                {
                    "schema_version": "unilab-package-upload-auth/v1",
                    "ak": "access-key",
                    "sk": "secret-key",
                }
            )
        )
    )

    decoded = base64.b64decode(credentials.auth_secret()).decode("utf-8")
    assert decoded == "access-key:secret-key"


def test_upload_credentials_reject_extra_fields_without_echoing_secret() -> None:
    """非法凭据合同失败关闭，错误正文不得回显 AK、SK 或未知字段值。"""

    secret = "must-never-appear"
    with pytest.raises(PackageUploadAuthError) as captured:
        read_package_upload_credentials(
            StringIO(
                json.dumps(
                    {
                        "schema_version": "unilab-package-upload-auth/v1",
                        "ak": "access-key",
                        "sk": secret,
                        "debug": secret,
                    }
                )
            )
        )

    assert secret not in str(captured.value)


def test_publication_diagnostics_do_not_persist_authorization(
    tmp_path: Path,
) -> None:
    """发布诊断只保存包正文和响应，不把 Authorization 或原始凭据写入磁盘。"""

    session = _RecordingSession()
    client = PackagePublicationHttpClient(
        base_url="https://leap-lab.uat.bohrium.com/api/v1",
        auth_secret="reversible-auth-secret",
        working_dir=tmp_path,
        session=session,  # type: ignore[arg-type]
    )

    response = client.upload_package_resources(
        [{"id": "community.review_lab.pump"}],
        {"name": "review-lab", "version": "1.2.0"},
    )
    client.close()

    assert response.status_code == 200
    assert session.headers["Authorization"] == "Lab reversible-auth-secret"
    diagnostics = "\n".join(
        path.read_text(encoding="utf-8") for path in tmp_path.iterdir()
    )
    assert "Authorization" not in diagnostics
    assert "reversible-auth-secret" not in diagnostics


def test_publication_network_error_does_not_echo_authorization(
    tmp_path: Path,
) -> None:
    """发布网络异常必须脱敏，不能把 Session 里的 Lab Authorization 带回 UI。"""

    session = _FailingSession()
    client = PackagePublicationHttpClient(
        base_url="https://leap-lab.test.bohrium.com/api/v1",
        auth_secret="must-never-appear",
        working_dir=tmp_path,
        session=session,  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError) as captured:
        client.upload_package_resources([], {"name": "review-lab"})
    client.close()

    assert "网络异常" in str(captured.value)
    assert "must-never-appear" not in str(captured.value)


def test_secure_stdin_upload_binds_selected_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """安全上传入口必须绑定所选 UAT 地址并在结束时关闭短生命周期连接。"""

    observed: dict[str, Any] = {}

    class _FakePublicationClient:
        """记录 Main 交付的非秘密发布上下文并模拟短生命周期连接。"""

        def __init__(self, **kwargs: Any) -> None:
            """保存构造参数；参数是地址、鉴权载荷和受管目录，函数无返回值。"""

            observed.update(kwargs)

        def close(self) -> None:
            """记录连接已关闭；函数无参数和返回值。"""

            observed["closed"] = True

    def _fake_run_package_command(
        args: dict[str, Any],
        *,
        http_client: Any,
    ) -> None:
        """记录上传命令与注入 client；函数成功时无返回值。"""

        observed["args"] = args
        observed["client"] = http_client

    monkeypatch.setattr(
        secure_upload_module,
        "PackagePublicationHttpClient",
        _FakePublicationClient,
    )
    monkeypatch.setattr(
        secure_upload_module,
        "run_package_command",
        _fake_run_package_command,
    )
    secure_upload_module.run_secure_package_upload(
        {
            "addr": "uat",
            "working_dir": str(tmp_path),
            "package_action": "upload",
        },
        input_stream=StringIO(
            json.dumps(
                {
                    "schema_version": "unilab-package-upload-auth/v1",
                    "ak": "uat-ak",
                    "sk": "uat-sk",
                }
            )
        ),
    )

    assert observed["base_url"] == "https://leap-lab.uat.bohrium.com/api/v1"
    assert Path(observed["working_dir"]) == tmp_path
    assert base64.b64decode(str(observed["auth_secret"])).decode("utf-8") == (
        "uat-ak:uat-sk"
    )
    assert observed["closed"] is True


def test_main_dispatches_stdin_upload_before_local_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """根 CLI 识别 ``--auth-stdin`` 后必须在任何 local_config 加载前提前返回。"""

    observed: dict[str, Any] = {}

    def _fake_secure_upload(args: dict[str, Any]) -> None:
        """记录 Main 交付的解析参数；函数成功时无返回值。"""

        observed.update(args)

    monkeypatch.setattr(
        secure_upload_module,
        "run_secure_package_upload",
        _fake_secure_upload,
    )
    monkeypatch.setattr(main_module, "load_config_from_file", pytest.fail)
    monkeypatch.setattr(
        main_module.sys,
        "argv",
        [
            "unilab",
            "--working_dir",
            str(tmp_path),
            "--addr",
            "uat",
            "package",
            "upload",
            "--path",
            str(tmp_path),
            "--auth-stdin",
            "--json",
        ],
    )

    main_module.main()

    assert observed["auth_stdin"] is True
    assert observed["addr"] == "uat"


class _RecordingSession:
    """记录发布请求且不访问网络的最小 requests.Session 测试替身。"""

    def __init__(self) -> None:
        """初始化可观察请求头；构造函数无参数和返回值。"""

        self.headers: dict[str, str] = {}

    def post(self, *_args, **_kwargs):
        """接收一次资源发布并返回 HTTP 200 测试响应。"""

        return SimpleNamespace(status_code=200, text='{"code":0}')

    def close(self) -> None:
        """模拟关闭连接池；函数无参数和返回值。"""


class _FailingSession(_RecordingSession):
    """模拟携带敏感请求头但只抛出网络异常的发布 Session。"""

    def post(self, *_args, **_kwargs):
        """模拟发布端不可达；函数固定抛出 requests 网络异常。"""

        raise requests.RequestException("wire failure")
