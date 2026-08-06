"""可信工作流创作转换（Trusted Authoring Transform）目录代际刷新合同。"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.app.test_authoring_transform_api import (
    CATALOG_UNAVAILABLE,
    RecordingTransformEngine,
    _assert_success,
    _generate_body,
)
from unilabos.config.config import BasicConfig
from unilabos.workflow import composition
from unilabos.workflow.models import CandidateCompilation

GENERATION_A_FINGERPRINT = "sha256:" + "a" * 64
GENERATION_B_FINGERPRINT = "sha256:" + "b" * 64
AUTHORING_TRANSFORM_PATHS = {
    "/api/v1/authoring/compile",
    "/api/v1/authoring/generate-python",
    "/api/v1/authoring/validate",
}


class _GenerationTransformEngine(RecordingTransformEngine):
    """把公开转换响应标记为一个确定的模板目录代际。"""

    def __init__(self, *, fingerprint: str) -> None:
        """保存当前模板目录指纹。

        参数说明：``fingerprint`` 是此编译器代际的稳定目录指纹。返回：无；
        新实例沿用记录型引擎的合法转换行为。
        """

        super().__init__()
        self._fingerprint = fingerprint

    def _result(
        self,
        operation: str,
        values: dict[str, Any],
    ) -> CandidateCompilation:
        """生成带本代目录指纹的合法公共转换结果。

        参数说明：``operation`` 是公开转换操作名，``values`` 是闭合请求字段。
        返回：复用记录型引擎构造、但带当前代指纹的候选编译结果。
        """

        result = super()._result(operation, values)
        result.template_catalog_fingerprint = self._fingerprint
        return result


@pytest.fixture(autouse=True)
def _clean_product_server(monkeypatch: pytest.MonkeyPatch) -> Any:
    """隔离进程级工作流服务与产品 Web 配置。

    参数说明：``monkeypatch`` 在用例结束时恢复全局配置。返回：pytest 生命周期
    值；前后均清理工作流权威，避免其他测试留下路由依赖。
    """

    composition.reset_workflow_service_for_test()
    monkeypatch.setattr(BasicConfig, "working_dir", "")
    try:
        yield
    finally:
        composition.reset_workflow_service_for_test()


def _reload_server() -> Any:
    """重载产品 Web 模块并取得未安装工作流路由的新应用。

    参数：无。返回：重新载入的 ``unilabos.app.web.server`` 模块。
    """

    return importlib.reload(importlib.import_module("unilabos.app.web.server"))


def _setup_product_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workflow_service: Any,
) -> Any:
    """用指定工作流服务（WorkflowService）装配真实产品 Web 应用。

    参数：``tmp_path`` 提供隔离工作目录；``monkeypatch`` 注入产品组合接缝；
    ``workflow_service`` 是本用例要观察的身份稳定服务。返回：已完成产品路由
    装配的 FastAPI 应用；库存权威（Inventory Authority）与调度器（Scheduler）
    在本测试中均明确缺席。
    """

    monkeypatch.setattr(BasicConfig, "working_dir", str(tmp_path / "runtime"))
    monkeypatch.setattr(BasicConfig, "workflow_editable_package_roots", ())
    scheduler_integration = importlib.import_module(
        "unilabos.app.scheduler.integration"
    )

    def no_inventory_service() -> None:
        """返回缺席的本地库存权威（Inventory Authority）。"""

        return

    def no_edge_scheduler() -> None:
        """返回缺席的本地调度器（Scheduler）。"""

        return

    def compose_runtime(
        _working_dir: str,
        *,
        editable_package_roots: tuple[str, ...],
    ) -> Any:
        """返回测试指定的工作流服务（WorkflowService）。

        参数：``_working_dir`` 是产品工作目录；``editable_package_roots`` 是显式
        可编辑包授权。返回：调用方提供且可推进编译器代际的同一服务对象。
        """

        assert editable_package_roots == ()
        return workflow_service

    monkeypatch.setattr(
        scheduler_integration,
        "get_inventory_service",
        no_inventory_service,
    )
    monkeypatch.setattr(
        scheduler_integration,
        "get_edge_scheduler",
        no_edge_scheduler,
    )
    monkeypatch.setattr(composition, "compose_workflow_runtime", compose_runtime)
    return _reload_server().setup_server()


def test_product_does_not_mount_authoring_transforms_without_compiler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺少编译器时必须保持基线产品路由形状。

    参数：``tmp_path`` 提供隔离工作目录；``monkeypatch`` 注入没有编译器的
    工作流服务（WorkflowService）。返回：无；断言三个可信工作流创作转换
    （Trusted Authoring Transform）路径均不进入公开 OpenAPI，等价于未挂载。
    """

    app = _setup_product_app(
        tmp_path,
        monkeypatch,
        SimpleNamespace(compiler=None),
    )

    assert AUTHORING_TRANSFORM_PATHS.isdisjoint(app.openapi()["paths"])


def test_installed_authoring_transform_maps_lost_compiler_to_catalog_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """路由安装后编译器被撤销时必须返回目录不可用信封。

    参数：``tmp_path`` 提供隔离工作目录；``monkeypatch`` 注入可在请求前撤销
    编译器的工作流服务（WorkflowService）。返回：无；断言后端信封
    （Backend Envelope）稳定为 ``code=5001``，不得退化为内部错误。
    """

    workflow_service = SimpleNamespace(
        compiler=_GenerationTransformEngine(fingerprint=GENERATION_A_FINGERPRINT)
    )
    app = _setup_product_app(tmp_path, monkeypatch, workflow_service)
    workflow_service.compiler = None

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/authoring/generate-python",
            json=_generate_body(),
        )

    assert response.status_code == 200
    assert response.json() == CATALOG_UNAVAILABLE


def test_installed_authoring_transform_observes_rebuilt_compiler_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一 HTTP 路由必须逐请求读取重建后的工作流创作编译器。

    参数说明：``tmp_path`` 提供只用于触发产品装配的隔离工作目录；
    ``monkeypatch`` 注入同一个可变工作流服务。返回：无；第一次
    ``generate-python`` 必须观察目录代际 A，把服务编译器切换到代际 B 后，
    第二次请求必须观察 B，不得继续使用路由安装时捕获的 A。
    """

    # ``generation_a`` 与 ``generation_b`` 分别代表应用已发布工作流前后的
    # 模板目录代际；``workflow_service`` 身份始终不变，只有编译器引用推进。
    generation_a = _GenerationTransformEngine(fingerprint=GENERATION_A_FINGERPRINT)
    generation_b = _GenerationTransformEngine(fingerprint=GENERATION_B_FINGERPRINT)
    workflow_service = SimpleNamespace(compiler=generation_a)
    app = _setup_product_app(tmp_path, monkeypatch, workflow_service)
    with TestClient(app) as client:
        first = client.post(
            "/api/v1/authoring/generate-python",
            json=_generate_body(),
        )
        workflow_service.compiler = generation_b
        second = client.post(
            "/api/v1/authoring/generate-python",
            json=_generate_body(),
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert (
        _assert_success(first.json())["template_catalog_fingerprint"]
        == GENERATION_A_FINGERPRINT
    )
    assert (
        _assert_success(second.json())["template_catalog_fingerprint"]
        == GENERATION_B_FINGERPRINT
    )
