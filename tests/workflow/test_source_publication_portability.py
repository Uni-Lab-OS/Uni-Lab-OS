"""工作流源码（Workflow Source）跨平台发现和发布合同测试。"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from unilabos.workflow import composition, source_publication, source_workspace
from unilabos.workflow.models import CandidateCompilation
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
CATALOG_FINGERPRINT = f"sha256:{'f' * 64}"


class SourceOnlyCompiler:
    """为发布测试生成不改变工作流图的候选版本（Candidate Revision）。"""

    compiler_version = "portable-source-v1"
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
        """把任意测试源码编译为仅源码变化的确定性候选版本。

        参数：工作流身份/修订、源码 URI 和源码提供编译上下文；``applied_graph``
        是保持不变的已应用图。返回：合法候选编译结果；不产生外部状态。
        """

        del workflow_uuid, workflow_revision, source_uri
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


class WindowsMsvcrt:
    """模拟 Windows 可用的最小文件锁模块。"""

    LK_NBLCK = 1
    LK_UNLCK = 2

    def __init__(self) -> None:
        """建立锁定调用记录；参数无，返回无。"""

        self.calls: list[tuple[int, int, int]] = []

    def locking(self, descriptor: int, mode: int, length: int) -> None:
        """记录目标描述符上的独占锁或解锁。

        参数：``descriptor`` 是目标文件；``mode`` 是锁操作；``length`` 是锁定
        字节数。返回：无；测试替身不改变宿主 Linux 文件锁。
        """

        self.calls.append((descriptor, mode, length))


class MacOSFcntl:
    """只提供 POSIX ``flock``、不提供 Linux 文件租约的 macOS 替身。"""

    LOCK_EX = 1
    LOCK_NB = 2
    LOCK_UN = 4

    def __init__(self) -> None:
        """建立 ``flock`` 调用记录；参数无，返回无。"""

        self.calls: list[tuple[int, int]] = []

    def flock(self, descriptor: int, operation: int) -> None:
        """记录 POSIX 目标文件锁生命周期。

        参数：``descriptor`` 是目标文件；``operation`` 是锁定或解锁标志。
        返回：无；测试替身不改变宿主文件锁。
        """

        self.calls.append((descriptor, operation))


@pytest.fixture(autouse=True)
def clean_composition() -> Any:
    """隔离每个用例使用的进程级工作流组合根。

    参数：无。返回：pytest 生命周期控制值；前后均停止源码监视器（Source
    Monitor）并关闭工作流服务（WorkflowService）。
    """

    composition.reset_workflow_service_for_test()
    try:
        yield
    finally:
        composition.reset_workflow_service_for_test()


def _seed_workflow(working_dir: Path) -> None:
    """在产品数据库中创建待绑定源码的既有工作流（Workflow）。

    参数：``working_dir`` 决定 ``workflow_history.db``。返回：无；播种连接在
    返回前关闭。
    """

    service = WorkflowService(WorkflowStore(working_dir / "workflow_history.db"))
    try:
        service.create_workflow(
            workflow_uuid=WORKFLOW_UUID,
            name="portable source",
            tags=[],
            description=None,
            meta_data={},
        )
    finally:
        service.close()


def _write_package(selected_root: Path) -> Path:
    """写入发现、读取和保存共用的最小可编辑包（Editable Package）。

    参数：``selected_root`` 是当前配置授权目录。返回：规范工作流源码
    （Workflow Source）路径；manifest 与初始源码均已完整落盘。
    """

    source_path = selected_root / "portable_lab" / "workflows" / "demo.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("value = 'initial'\n", encoding="utf-8")
    selected_root.joinpath("package.yaml").write_text(
        "package:\n"
        "  name: portable_lab\n"
        "workflows:\n"
        f"  - workflow_uuid: {WORKFLOW_UUID}\n"
        "    source: portable_lab/workflows/demo.py\n",
        encoding="utf-8",
    )
    return source_path


def test_windows_without_dir_fd_can_discover_read_and_save_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows 无 ``dir_fd`` 时正常源码链路必须使用安全绝对路径回退。

    参数：``tmp_path`` 隔离产品数据库与包；``monkeypatch`` 禁止所有相对目录
    描述符调用并注入 Windows 锁。返回：无；证明发现、恢复读取和 CAS 保存都成功。
    """

    working_dir = tmp_path / "runtime"
    selected_root = tmp_path / "editable"
    source_path = _write_package(selected_root)
    _seed_workflow(working_dir)
    original_open = os.open
    original_replace = os.replace
    dir_fd_attempts: list[str] = []
    windows_lock = WindowsMsvcrt()

    @contextmanager
    def windows_directory_guard(_paths: Sequence[Path]) -> Iterator[None]:
        """模拟已固定的 Windows 工作流草稿（Workflow Draft）目录链。

        参数：``_paths`` 是待固定目录链，本测试只验证平台选择。返回：上下文无值。
        """

        yield

    def windows_replace(target: Path, replacement: Path, backup: Path) -> None:
        """在 Linux 上模拟保留旧稿 backup 的 ``ReplaceFileW``。

        参数：三个路径依次是规范草稿、完整替换稿与旧稿备份。返回：无；使用测试
        捕获的宿主原子替换完成等价文件布局。
        """

        original_replace(target, backup)
        original_replace(replacement, target)

    def windows_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        """拒绝 Windows 不支持的 ``dir_fd``，其余打开委托宿主实现。

        参数：路径、标志、模式和可选目录描述符与 ``os.open`` 一致。返回：打开
        的文件描述符；出现 ``dir_fd`` 时抛出 ``NotImplementedError`` 并记录。
        """

        if dir_fd is not None:
            dir_fd_attempts.append(os.fsdecode(path))
            raise NotImplementedError("dir_fd is unavailable on Windows")
        return original_open(path, flags, mode)

    monkeypatch.setattr(
        source_workspace,
        "_DIRECTORY_FD_PATHS_SUPPORTED",
        False,
        raising=False,
    )
    monkeypatch.setattr(
        source_publication,
        "_PLATFORM",
        "win32",
        raising=False,
    )
    monkeypatch.setattr(source_publication, "_fcntl", None, raising=False)
    monkeypatch.setattr(
        source_publication,
        "_msvcrt",
        windows_lock,
        raising=False,
    )
    monkeypatch.setattr(
        source_publication,
        "hold_windows_directory_chain",
        windows_directory_guard,
    )
    monkeypatch.setattr(
        source_publication,
        "replace_windows_file_with_backup",
        windows_replace,
    )
    monkeypatch.setattr(source_publication, "fcntl", None, raising=False)
    monkeypatch.setattr(os, "open", windows_open)

    service = composition.compose_workflow_runtime(
        working_dir,
        compiler=SourceOnlyCompiler(),
        editable_package_roots=(selected_root,),
    )
    baseline = service.get_authoring(WORKFLOW_UUID)
    saved = service.save_draft(
        WORKFLOW_UUID,
        python_source="value = 'changed'\n",
        expected_draft_hash=baseline["draft"]["draft_hash"],
        expected_workflow_revision=baseline["workflow_revision"],
    )

    assert saved["draft"]["python_source"] == "value = 'changed'\n"
    assert source_path.read_text(encoding="utf-8") == "value = 'changed'\n"
    assert dir_fd_attempts == []
    assert windows_lock.calls


def test_macos_uses_flock_without_assuming_linux_file_leases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """macOS CAS 发布必须用 ``flock``，不能假设 Linux 租约常量存在。

    参数：``tmp_path`` 隔离服务和规范源码；``monkeypatch`` 注入仅含 ``flock``
    的模块。返回：无；保存成功且观察到同一目标描述符的锁定与解锁。
    """

    database_path = tmp_path / "workflow.db"
    package_root = tmp_path / "portable_lab"
    source_path = package_root / "workflows" / "demo.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("value = 'initial'\n", encoding="utf-8")
    service = WorkflowService(
        WorkflowStore(database_path),
        compiler=SourceOnlyCompiler(),
    )
    service.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="macOS source",
        tags=[],
        description=None,
        meta_data={},
    )
    service.register_editable_source(
        workflow_uuid=WORKFLOW_UUID,
        package_id="portable_lab",
        package_root=package_root,
        relative_path="workflows/demo.py",
    )
    baseline = service.get_authoring(WORKFLOW_UUID)
    macos_lock = MacOSFcntl()
    monkeypatch.setattr(
        source_publication,
        "_PLATFORM",
        "darwin",
        raising=False,
    )
    monkeypatch.setattr(source_publication, "_fcntl", macos_lock, raising=False)
    monkeypatch.setattr(source_publication, "_msvcrt", None, raising=False)
    monkeypatch.setattr(source_publication, "fcntl", macos_lock, raising=False)

    try:
        saved = service.save_draft(
            WORKFLOW_UUID,
            python_source="value = 'changed'\n",
            expected_draft_hash=baseline["draft"]["draft_hash"],
            expected_workflow_revision=baseline["workflow_revision"],
        )
    finally:
        service.close()

    assert saved["draft"]["python_source"] == "value = 'changed'\n"
    assert [operation for _descriptor, operation in macos_lock.calls] == [
        MacOSFcntl.LOCK_EX | MacOSFcntl.LOCK_NB,
        MacOSFcntl.LOCK_UN,
    ]
