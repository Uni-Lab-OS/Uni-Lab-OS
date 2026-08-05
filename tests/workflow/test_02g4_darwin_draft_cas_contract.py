"""Round 02G4 Darwin 工作流创作草稿（Authoring Draft）安全保存合同。"""

from __future__ import annotations

import errno
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from unilabos.app.workflow_api import install_workflow_api
from unilabos.workflow import service as workflow_service_module
from unilabos.workflow.models import CandidateCompilation
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
INITIAL_SOURCE = "value = 'external authority'\n"
CHANGED_SOURCE = "value = 'application edit'\n"
CATALOG_FINGERPRINT = f"sha256:{'d' * 64}"
ORIGIN = "http://localhost:5173"


@dataclass
class DarwinFcntlProbe:
    """模拟有 `fcntl` 模块但没有 Linux file-lease 常量的 Darwin 能力。"""

    F_WRLCK: int = 1
    F_UNLCK: int = 2
    calls: list[tuple[int, int, Any | None]] = field(default_factory=list)

    def fcntl(
        self,
        descriptor: int,
        command: int,
        argument: Any | None = None,
    ) -> int:
        """记录一次 Linux 专属调用并以 Darwin 不支持错误拒绝。

        `descriptor` 是目标文件描述符，`command` 是 `fcntl` 命令，`argument`
        是可选命令参数；若错误地调用该接缝则记录调用并抛出 `OSError`，没有
        正常返回值。
        """

        self.calls.append((descriptor, command, argument))
        raise OSError(errno.ENOTSUP, "Darwin does not expose Linux file leases")


@dataclass
class DarwinSignalProbe:
    """模拟具备线程信号掩码但没有 `sigtimedwait` 的 Darwin 能力。"""

    SIGIO: int = 29
    SIG_BLOCK: int = 0
    SIG_SETMASK: int = 2
    mask_calls: list[tuple[int, set[int]]] = field(default_factory=list)

    def pthread_sigmask(self, how: int, mask: set[int]) -> set[int]:
        """记录 Linux lease cleanup 试图修改的线程信号掩码。

        `how` 是掩码操作，`mask` 是信号集合；返回空的先前掩码，避免测试修改
        宿主线程状态。该探针刻意不提供 `sigtimedwait`，与现场 Darwin 能力一致。
        """

        self.mask_calls.append((how, set(mask)))
        return set()


class SourceOnlyCompiler:
    """把工作流源码（Workflow Source）编译为不改变图的候选。"""

    compiler_version = "round-02g4-darwin-contract-v1"
    template_catalog_fingerprint = CATALOG_FINGERPRINT

    def __init__(self) -> None:
        """初始化空的编译源码记录；没有参数与返回值。"""

        self.sources: list[str] = []

    def compile(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        python_source: str,
        source_uri: str,
        applied_graph: dict[str, Any],
    ) -> CandidateCompilation:
        """生成源码保持原样的 source-only 候选。

        `workflow_uuid` 与 `workflow_revision` 标识工作流及其基准修订，
        `python_source` 是待编译工作流源码，`source_uri` 是可编辑包内逻辑身份，
        `applied_graph` 是当前已应用工作流图。返回不改变图、源码或身份的候选；
        本测试编译器不抛领域异常。
        """

        del workflow_uuid, workflow_revision, source_uri
        self.sources.append(python_source)
        return CandidateCompilation(
            diagnostics=[],
            graph=applied_graph,
            normalized_python_source=python_source,
            source_map=[],
            changeset={
                "kind": "source_only",
                "created_node_uuids": [],
                "updated_node_uuids": [],
                "deleted_node_uuids": [],
                "created_edge_uuids": [],
                "updated_edge_uuids": [],
                "deleted_edge_uuids": [],
                "reserved_metadata_changed": False,
            },
            compiler_version=self.compiler_version,
            template_catalog_fingerprint=self.template_catalog_fingerprint,
        )


@dataclass(frozen=True)
class DarwinAuthoringHarness:
    """保存 Darwin HTTP 合同需要的公开客户端与安全观测点。"""

    client: TestClient
    source_path: Path
    fcntl_probe: DarwinFcntlProbe
    signal_probe: DarwinSignalProbe
    compiler: SourceOnlyCompiler


@pytest.fixture()
def darwin_authoring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[DarwinAuthoringHarness]:
    """创建现场 Darwin 能力组合下的真实 Draft PUT HTTP 环境。

    `tmp_path` 提供隔离数据库与可编辑包，`monkeypatch` 安装 `dir_fd=True`、
    `fcntl` 模块存在但缺少 `F_SETLEASE/F_SETSIG`、线程信号掩码存在但缺少
    `sigtimedwait` 的能力。产出 HTTP 客户端、权威源码路径及 Linux 原语调用
    探针；fixture 退出时关闭客户端和工作流服务。
    """

    package_root = tmp_path / "editable_package"
    source_path = package_root / "workflows" / "darwin.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(INITIAL_SOURCE.encode("utf-8"))

    compiler = SourceOnlyCompiler()
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store, compiler=compiler)
    service.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="Darwin Draft CAS",
        tags=[],
        description=None,
        meta_data={},
    )
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="darwin_contract",
        package_root=package_root,
        relative_path="workflows/darwin.py",
    )

    fcntl_probe = DarwinFcntlProbe()
    signal_probe = DarwinSignalProbe()
    monkeypatch.setattr(
        workflow_service_module,
        "_DIRECTORY_FD_PATHS_SUPPORTED",
        True,
    )
    monkeypatch.setattr(workflow_service_module, "fcntl", fcntl_probe)
    monkeypatch.setattr(workflow_service_module, "signal", signal_probe)
    monkeypatch.setattr(workflow_service_module, "msvcrt", None, raising=False)

    app = FastAPI(title="Darwin Draft CAS contract")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept"],
    )
    install_workflow_api(app, service)

    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield DarwinAuthoringHarness(
                client=client,
                source_path=source_path,
                fcntl_probe=fcntl_probe,
                signal_probe=signal_probe,
                compiler=compiler,
            )
    finally:
        service.close()


def _observed_authoring(harness: DarwinAuthoringHarness) -> dict[str, Any]:
    """读取调用方开始编辑时观察到的创作聚合。

    `harness` 提供公开 HTTP 客户端；返回 Backend-shaped `data` 聚合，并在 GET
    失败时通过断言终止测试。
    """

    response = harness.client.get(
        f"/api/v1/workflows/{WORKFLOW_UUID}/authoring",
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 200
    return response.json()["data"]


def _save_draft(
    harness: DarwinAuthoringHarness,
    *,
    python_source: str,
    observed: dict[str, Any],
) -> Any:
    """按已观察 hash 与工作流修订调用公开 Draft PUT。

    `harness` 提供 HTTP 客户端，`python_source` 是完整请求源码，`observed` 是
    编辑开始时的创作聚合；返回原始 HTTP 响应供调用测试检查 envelope 与 CORS。
    """

    # 该可空值是调用方观察到的 Draft CAS 令牌；缺失源码只能发送 null。
    expected_draft_hash = (
        observed["draft"]["draft_hash"] if observed["draft"] is not None else None
    )
    return harness.client.put(
        f"/api/v1/workflows/{WORKFLOW_UUID}/authoring/draft",
        headers={"Origin": ORIGIN},
        json={
            "python_source": python_source,
            "expected_draft_hash": expected_draft_hash,
            "expected_workflow_revision": observed["workflow_revision"],
        },
    )


def test_darwin缺失源码的draft_put保持exclusive_create基线(
    darwin_authoring: DarwinAuthoringHarness,
) -> None:
    """缺失工作流源码可用 exclusive link 创建，且不进入 Linux lease 接缝。

    `darwin_authoring` 提供缺少强 CAS 原语的 Darwin HTTP 环境；测试没有返回值，
    并证明调用方观察到 `draft=null` 后仍能安全创建源码、编译候选、保持工作流
    修订不变，同时不触发 Linux file lease 或 signal cleanup。
    """

    darwin_authoring.source_path.unlink()
    observed = _observed_authoring(darwin_authoring)
    assert observed["draft"] is None

    response = _save_draft(
        darwin_authoring,
        python_source=INITIAL_SOURCE,
        observed=observed,
    )

    assert {
        "status": response.status_code,
        "cors": response.headers.get("access-control-allow-origin"),
        "source_bytes": darwin_authoring.source_path.read_bytes(),
        "compiler_sources": darwin_authoring.compiler.sources,
        "linux_fcntl_call_count": len(darwin_authoring.fcntl_probe.calls),
        "linux_signal_mask_call_count": len(darwin_authoring.signal_probe.mask_calls),
    } == {
        "status": 200,
        "cors": ORIGIN,
        "source_bytes": INITIAL_SOURCE.encode("utf-8"),
        "compiler_sources": [INITIAL_SOURCE],
        "linux_fcntl_call_count": 0,
        "linux_signal_mask_call_count": 0,
    }
    payload = response.json()
    assert payload["code"] == 0
    assert payload["data"]["workflow_revision"] == observed["workflow_revision"]
    assert payload["data"]["draft"]["python_source"] == INITIAL_SOURCE
    assert payload["data"]["candidate"] is not None


def test_darwin相同源码的draft_put无需替换即可成功核对并编译(
    darwin_authoring: DarwinAuthoringHarness,
) -> None:
    """相同工作流源码字节必须返回 200，且不得进入 Linux lease 接缝。

    `darwin_authoring` 提供缺少强 CAS 原语的 Darwin HTTP 环境；测试没有返回值，
    并证明 no-op 保存仍会编译候选、保持工作流修订与权威源码字节不变。
    """

    observed = _observed_authoring(darwin_authoring)

    response = _save_draft(
        darwin_authoring,
        python_source=INITIAL_SOURCE,
        observed=observed,
    )

    assert {
        "status": response.status_code,
        "cors": response.headers.get("access-control-allow-origin"),
        "source_bytes": darwin_authoring.source_path.read_bytes(),
        "compiler_sources": darwin_authoring.compiler.sources,
        "linux_fcntl_call_count": len(darwin_authoring.fcntl_probe.calls),
        "linux_signal_mask_call_count": len(darwin_authoring.signal_probe.mask_calls),
    } == {
        "status": 200,
        "cors": ORIGIN,
        "source_bytes": INITIAL_SOURCE.encode("utf-8"),
        "compiler_sources": [INITIAL_SOURCE],
        "linux_fcntl_call_count": 0,
        "linux_signal_mask_call_count": 0,
    }
    payload = response.json()
    assert payload["code"] == 0
    assert payload["data"]["workflow_revision"] == observed["workflow_revision"]
    assert payload["data"]["draft"]["draft_hash"] == observed["draft"]["draft_hash"]
    assert payload["data"]["candidate"] is not None


def test_darwin不同源码的draft_put受控冲突且保留包内权威字节(
    darwin_authoring: DarwinAuthoringHarness,
) -> None:
    """缺少强 CAS 时异字节保存必须返回既有冲突 envelope 与 CORS。

    `darwin_authoring` 提供现场 Darwin 能力组合；测试没有返回值，并证明失败不会
    编译未落盘源码、不会调用 Linux lease/cleanup，也不会覆盖可编辑包中的权威
    工作流源码（Workflow Source）字节。
    """

    observed = _observed_authoring(darwin_authoring)

    response = _save_draft(
        darwin_authoring,
        python_source=CHANGED_SOURCE,
        observed=observed,
    )

    assert {
        "status": response.status_code,
        "cors": response.headers.get("access-control-allow-origin"),
        "source_bytes": darwin_authoring.source_path.read_bytes(),
        "compiler_sources": darwin_authoring.compiler.sources,
        "linux_fcntl_call_count": len(darwin_authoring.fcntl_probe.calls),
        "linux_signal_mask_call_count": len(darwin_authoring.signal_probe.mask_calls),
    } == {
        "status": 409,
        "cors": ORIGIN,
        "source_bytes": INITIAL_SOURCE.encode("utf-8"),
        "compiler_sources": [],
        "linux_fcntl_call_count": 0,
        "linux_signal_mask_call_count": 0,
    }
    assert response.json() == {
        "code": 409,
        "error": {
            "code": "draft_hash_conflict",
            "message": "草稿已被其他程序修改，请查看差异后再保存",
        },
    }
