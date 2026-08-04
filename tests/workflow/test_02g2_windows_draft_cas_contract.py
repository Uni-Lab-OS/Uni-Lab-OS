"""Round 02G2 Windows Draft CAS 的独立 RED 合同测试。"""

from __future__ import annotations

import errno
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow import service as workflow_service_module
from unilabos.workflow.models import CandidateCompilation, WorkflowNodeWrite
from unilabos.workflow.service import WorkflowConflict, WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
NODE_UUID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
CATALOG_FINGERPRINT = f"sha256:{'c' * 64}"
INITIAL_SOURCE = "seed()\n"
NORMALIZED_SOURCE = "build()\n"
EXTERNAL_SOURCE = b"external_authority()\n"
WORKFLOW_RESERVED = {
    "input_contract": {"version": 1, "parameters": []},
    "output_contract": {"version": 1, "outputs": []},
    "output_bindings": {},
}
NODE_RESERVED = {"input_bindings": {}}


class WindowsMsvcrt:
    """模拟 Windows `msvcrt.locking` 的单进程字节区间锁边界。"""

    LK_LOCK = 0
    LK_NBLCK = 1
    LK_NBRLCK = 2
    LK_RLCK = 3
    LK_UNLCK = 4

    def __init__(self) -> None:
        """初始化空锁集合；除实例本身外无参数，也没有返回值。"""

        self._locks: set[tuple[int, int, int, int]] = set()
        self._on_next_lock: Callable[[], None] | None = None
        self.external_change_count = 0

    def arm_external_change(self, callback: Callable[[], None]) -> None:
        """安排下一次加锁时的外部改写。

        `callback` 是代表外部编辑器或 Git 写入的无参回调；本方法没有返回值。
        """

        self._on_next_lock = callback

    def locking(self, descriptor: int, mode: int, size: int) -> None:
        """按文件身份和当前位置模拟加锁或解锁。

        `descriptor` 是锁定文件描述符，`mode` 是 `msvcrt` 锁模式，`size`
        是从当前位置开始的字节数。本方法没有返回值；锁争用时抛出 `OSError`，
        未知模式抛出 `ValueError`。
        """

        stat_result = os.fstat(descriptor)
        lock_offset = os.lseek(descriptor, 0, os.SEEK_CUR)
        # 锁身份覆盖同一文件、同一区间；这是测试内 Windows 锁权威。
        lock_identity = (
            stat_result.st_dev,
            stat_result.st_ino,
            lock_offset,
            size,
        )
        if mode == self.LK_UNLCK:
            self._locks.discard(lock_identity)
            return
        if mode not in {self.LK_LOCK, self.LK_NBLCK, self.LK_NBRLCK, self.LK_RLCK}:
            raise ValueError(f"unsupported msvcrt lock mode: {mode}")
        if lock_identity in self._locks:
            raise OSError(errno.EACCES, "Windows byte-range lock is held")
        self._locks.add(lock_identity)
        if self._on_next_lock is not None:
            callback = self._on_next_lock
            self._on_next_lock = None
            self.external_change_count += 1
            callback()

    @staticmethod
    def get_osfhandle(descriptor: int) -> int:
        """把 `descriptor` 文件描述符作为模拟 Windows 句柄返回。"""

        return descriptor


class NormalizingGraphCompiler:
    """生成固定规范化源码和单节点候选图的测试编译器。"""

    compiler_version = "round-02g2-test-v1"
    template_catalog_fingerprint = CATALOG_FINGERPRINT

    def compile(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        python_source: str,
        source_uri: str,
        applied_graph: dict[str, Any],
    ) -> CandidateCompilation:
        """把 Draft 规范化并返回可应用的固定单节点工作流图。

        `workflow_uuid` 与 `workflow_revision` 标识候选所属工作流及基准修订，
        `python_source` 是待编译 Draft，`source_uri` 是源码身份，`applied_graph`
        是当前已应用工作流图（Applied Workflow Graph）。返回包含规范化源码、
        固定候选图和变更集的 `CandidateCompilation`；该测试编译器不抛领域异常。
        """

        del workflow_uuid, workflow_revision, source_uri
        normalized_source = (
            python_source if python_source.endswith("\n") else f"{python_source}\n"
        )
        candidate_graph = {
            "workflow": {
                **applied_graph["workflow"],
                "meta_data": {
                    **applied_graph["workflow"]["meta_data"],
                    "unilab": WORKFLOW_RESERVED,
                },
            },
            "nodes": [
                WorkflowNodeWrite(
                    uuid=NODE_UUID,
                    workflow_node_template_uuid=None,
                    name="Windows CAS node",
                    status="idle",
                    type="compute",
                    pose={},
                    param={},
                    execution_policy={},
                    meta_data={"unilab": NODE_RESERVED},
                ).model_dump()
            ],
            "edges": [],
            "node_templates": [],
            "handle_templates": [],
        }
        return CandidateCompilation(
            diagnostics=[],
            graph=candidate_graph,
            normalized_python_source=normalized_source,
            source_map=[],
            changeset={
                "kind": "graph",
                "created_node_uuids": [NODE_UUID],
                "updated_node_uuids": [],
                "deleted_node_uuids": [],
                "created_edge_uuids": [],
                "updated_edge_uuids": [],
                "deleted_edge_uuids": [],
                "reserved_metadata_changed": True,
            },
            compiler_version=self.compiler_version,
            template_catalog_fingerprint=self.template_catalog_fingerprint,
        )


@dataclass(frozen=True)
class WindowsAuthoringHarness:
    """保存一个 Windows 工作流创作（Authoring）服务测试环境。"""

    service: WorkflowService
    source_path: Path
    msvcrt: WindowsMsvcrt
    dir_fd_attempts: list[str]


@pytest.fixture()
def windows_authoring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[WindowsAuthoringHarness]:
    """创建无 `dir_fd`/`fcntl`、但有 `msvcrt` 的 Windows 服务环境。

    `tmp_path` 提供隔离持久化目录，`monkeypatch` 安装 Windows 能力边界。
    产出真实 `WorkflowService`、Draft 路径、锁模拟器和违规调用记录；fixture
    退出时关闭服务及其持久存储。
    """

    package_root = tmp_path / "package"
    source_path = package_root / "workflows" / "windows.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(INITIAL_SOURCE, encoding="utf-8")
    store = WorkflowStore(tmp_path / "workflow.db")
    service = WorkflowService(store, compiler=NormalizingGraphCompiler())
    service.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="Windows Draft CAS",
        tags=[],
        description=None,
        meta_data={},
    )
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="windows_cas",
        package_root=package_root,
        relative_path="workflows/windows.py",
    )

    original_open = os.open
    dir_fd_attempts: list[str] = []

    def windows_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        """模拟 Windows `os.open`。

        `path`、`flags`、`mode` 保持 `os.open` 含义，`dir_fd` 在 Windows
        必须为空。返回绝对路径打开的文件描述符；收到 `dir_fd` 时记录违规调用
        并抛出 `NotImplementedError`。
        """

        if dir_fd is not None:
            dir_fd_attempts.append(os.fsdecode(path))
            raise NotImplementedError("dir_fd is unavailable on Windows")
        return original_open(path, flags, mode)

    windows_msvcrt = WindowsMsvcrt()
    monkeypatch.setattr(
        workflow_service_module,
        "_DIRECTORY_FD_PATHS_SUPPORTED",
        False,
    )
    monkeypatch.setattr(workflow_service_module, "fcntl", None)
    monkeypatch.setattr(workflow_service_module, "_LEASE_BREAK_SIGNAL", None)
    monkeypatch.setattr(
        workflow_service_module,
        "msvcrt",
        windows_msvcrt,
        raising=False,
    )
    monkeypatch.setattr(os, "open", windows_open)

    try:
        yield WindowsAuthoringHarness(
            service=service,
            source_path=source_path,
            msvcrt=windows_msvcrt,
            dir_fd_attempts=dir_fd_attempts,
        )
    finally:
        service.close()


def test_windows_save_draft_with_matching_cas_persists_and_returns_candidate(
    windows_authoring: WindowsAuthoringHarness,
) -> None:
    """匹配 Draft hash 与工作流修订时，Windows 保存必须落盘并返回候选。

    `windows_authoring` 提供真实服务与 Windows 文件边界；测试没有返回值，
    保存冲突或候选缺失都会作为断言失败暴露。
    """

    service = windows_authoring.service
    before = service.get_authoring(WORKFLOW_UUID)

    saved = service.save_draft(
        WORKFLOW_UUID,
        python_source="build()",
        expected_draft_hash=before["draft"]["draft_hash"],
        expected_workflow_revision=before["workflow_revision"],
    )

    assert windows_authoring.source_path.read_bytes() == b"build()"
    assert saved["draft"]["python_source"] == "build()"
    assert saved["candidate"] is not None
    assert saved["candidate"]["base_workflow_revision"] == 1
    assert saved["candidate"]["draft_hash"] == saved["draft"]["draft_hash"]
    assert saved["candidate"]["normalized_python_source"] == NORMALIZED_SOURCE
    assert windows_authoring.dir_fd_attempts == []


def test_windows_save_draft_rejects_external_change_during_cas_window(
    windows_authoring: WindowsAuthoringHarness,
) -> None:
    """CAS 加锁窗口出现外部改写时必须冲突，且不得覆盖外部权威字节。

    `windows_authoring` 提供真实服务和可注入竞争的 Windows 锁边界；测试没有
    返回值，并要求稳定抛出 `WorkflowConflict(draft_hash_conflict)`。
    """

    service = windows_authoring.service
    source_path = windows_authoring.source_path
    before = service.get_authoring(WORKFLOW_UUID)
    windows_authoring.msvcrt.arm_external_change(
        lambda: source_path.write_bytes(EXTERNAL_SOURCE)
    )

    with pytest.raises(WorkflowConflict) as captured:
        service.save_draft(
            WORKFLOW_UUID,
            python_source="build()",
            expected_draft_hash=before["draft"]["draft_hash"],
            expected_workflow_revision=before["workflow_revision"],
        )

    assert captured.value.code == "draft_hash_conflict"
    assert windows_authoring.msvcrt.external_change_count == 1
    assert source_path.read_bytes() == EXTERNAL_SOURCE
    assert service.get_authoring(WORKFLOW_UUID)["draft"]["python_source"] == (
        EXTERNAL_SOURCE.decode("utf-8")
    )
    assert windows_authoring.dir_fd_attempts == []


def test_windows_materialized_draft_applies_graph_and_revision(
    windows_authoring: WindowsAuthoringHarness,
) -> None:
    """Windows 保存规范化 Draft 后应能 Apply 并持久化正确图与修订。

    `windows_authoring` 提供真实服务与 Windows 文件边界；测试没有返回值，
    通过公开 Apply Interface 验证图、源码和工作流修订的同一权威结果。
    """

    service = windows_authoring.service
    before = service.get_authoring(WORKFLOW_UUID)
    preview = service.save_draft(
        WORKFLOW_UUID,
        python_source="build()",
        expected_draft_hash=before["draft"]["draft_hash"],
        expected_workflow_revision=before["workflow_revision"],
    )
    materialized = service.save_draft(
        WORKFLOW_UUID,
        python_source=preview["candidate"]["normalized_python_source"],
        expected_draft_hash=preview["draft"]["draft_hash"],
        expected_workflow_revision=preview["workflow_revision"],
    )

    applied = service.apply_authoring(
        WORKFLOW_UUID,
        candidate_hash=materialized["candidate"]["candidate_hash"],
    )
    persisted_graph = service.get_graph(WORKFLOW_UUID)

    assert windows_authoring.source_path.read_text(encoding="utf-8") == (
        NORMALIZED_SOURCE
    )
    assert applied["apply_result"]["kind"] == "graph"
    assert applied["apply_result"]["previous_workflow_revision"] == 1
    assert applied["apply_result"]["workflow_revision"] == 2
    assert applied["authoring"]["state"] == "applied"
    assert applied["authoring"]["workflow_revision"] == 2
    assert applied["authoring"]["applied_graph"] == persisted_graph
    assert persisted_graph["workflow"]["revision"] == 2
    assert [node["uuid"] for node in persisted_graph["nodes"]] == [NODE_UUID]
    assert (
        applied["authoring"]["applied_source"]["source_hash"]
        == (materialized["draft"]["draft_hash"])
    )
    assert windows_authoring.dir_fd_attempts == []
