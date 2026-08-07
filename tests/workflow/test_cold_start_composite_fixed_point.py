"""组合工作流（Composite Workflow）冷启动依赖固定点合同。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.registry.test_f05_material_source_catalog import _Registry
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.package_manager import WorkspaceSource, compile_package_source
from unilabos.package_manager.workspace_runtime.activation import (
    workflow_source_plan_from_catalog,
)
from unilabos.workflow import composition
from unilabos.workflow.composition import compose_local_workflow_template_runtime
from unilabos.workflow.models import CandidateCompilation

from .test_c1_r2_static_expansion_contract import (
    CHILD_WORKFLOW_UUID as PUBLISHED_CHILD_WORKFLOW_UUID,
)
from .test_c1_r2_static_expansion_contract import (
    PARENT_WORKFLOW_UUID as PUBLISHED_PARENT_WORKFLOW_UUID,
)
from .test_c1_r4_production_wiring import (
    PACKAGE_ID as PUBLISHED_PACKAGE_ID,
)
from .test_c1_r4_production_wiring import _seed_applied_child
from .test_c1_r4_production_wiring import _write_package as _write_published_package

# 两个稳定身份刻意让父工作流（Workflow）按 UUID 排在子工作流之前。
PARENT_WORKFLOW_UUID = "11111111-1111-4111-8111-111111111111"
CHILD_WORKFLOW_UUID = "22222222-2222-4222-8222-222222222222"
INITIAL_CATALOG_FINGERPRINT = f"sha256:{'a' * 64}"
CHILD_READY_CATALOG_FINGERPRINT = f"sha256:{'b' * 64}"


class ColdStartCompositeCompiler:
    """模拟恢复回调使外部子合同目录事实前进的可控接缝。

    本替身只验证固定点编排算法；它不表示普通子工作流源码编译可以发布子合同。
    产品发布语义由本文件的真实 ``apply_authoring`` 回归单独证明。
    """

    compiler_version = "cold-start-fixed-point-v1"

    def __init__(self) -> None:
        """建立子合同尚不可用的冷启动目录。

        参数：无。
        返回：无；``compiled_workflow_uuids`` 记录实际编译顺序，
        ``child_contract_available`` 表示测试外部目录是否已进入下一代际。
        异常：无。
        """

        # ``compiled_workflow_uuids`` 是启动恢复实际访问的工作流身份顺序证据。
        self.compiled_workflow_uuids: list[str] = []
        # ``child_contract_available`` 只模拟恢复回调期间外部目录事实前进；普通
        # 工作流源码编译不具有发布权威。
        self.child_contract_available = False

    @property
    def template_catalog_fingerprint(self) -> str:
        """返回当前模板目录（Template Catalog）代际指纹。

        参数：无。
        返回：子工作流合同尚不可用时返回初始指纹，可用后返回下一代指纹。
        异常：无。
        """

        if self.child_contract_available:
            return CHILD_READY_CATALOG_FINGERPRINT
        return INITIAL_CATALOG_FINGERPRINT

    def compile(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        python_source: str,
        source_uri: str,
        applied_graph: dict[str, Any],
    ) -> CandidateCompilation:
        """按冷启动顺序编译父、子工作流源码。

        参数：``workflow_uuid`` 与 ``workflow_revision`` 标识当前工作流；
        ``python_source`` 与 ``source_uri`` 是已授权源码及来源；``applied_graph``
        是当前应用图。返回：父来源过早编译时返回
        ``composite_child_not_found``；处理子身份时测试外部目录前进，后续父来源
        返回可信候选。该副作用不是产品编译器语义。
        异常：未知工作流身份触发测试断言失败。
        """

        # 这些编译上下文字段在本测试只作为公开接口形状，不参与目录模拟。
        del workflow_revision, source_uri
        assert workflow_uuid in {PARENT_WORKFLOW_UUID, CHILD_WORKFLOW_UUID}
        self.compiled_workflow_uuids.append(workflow_uuid)
        if (
            workflow_uuid == PARENT_WORKFLOW_UUID
            and not self.child_contract_available
        ):
            return CandidateCompilation(
                diagnostics=[
                    {
                        "severity": "error",
                        "code": "composite_child_not_found",
                        "message": "子工作流 material_transfer 尚未进入发布目录",
                    }
                ],
                compiler_version=self.compiler_version,
                template_catalog_fingerprint=self.template_catalog_fingerprint,
            )
        if workflow_uuid == CHILD_WORKFLOW_UUID:
            self.child_contract_available = True
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


class MutableCatalogCompiler:
    """提供源码不变、模板目录代际可变的可信编译接缝。"""

    compiler_version = "catalog-observation-v1"

    def __init__(self) -> None:
        """建立初始模板目录代际并清空编译记录。

        参数：无。
        返回：无；``catalog_fingerprint`` 可由测试推进，``compile_calls`` 记录
        每次编译绑定的工作流身份与目录指纹。
        异常：无。
        """

        # ``catalog_fingerprint`` 是监视器也必须观察的模板目录代际身份。
        self.catalog_fingerprint = INITIAL_CATALOG_FINGERPRINT
        # ``compile_calls`` 证明相同源码是否真正按新目录重新编译。
        self.compile_calls: list[tuple[str, str]] = []

    @property
    def template_catalog_fingerprint(self) -> str:
        """返回当前模板目录代际指纹。

        参数：无。
        返回：测试显式设置的规范 SHA-256 指纹。
        异常：无。
        """

        return self.catalog_fingerprint

    def compile(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        python_source: str,
        source_uri: str,
        applied_graph: dict[str, Any],
    ) -> CandidateCompilation:
        """用当前目录代际生成不改变应用图的可信候选。

        参数：``workflow_uuid``、``workflow_revision`` 和 ``source_uri`` 是编译
        上下文；``python_source`` 是当前工作流源码；``applied_graph`` 是应用图。
        返回：绑定当前目录指纹的源码候选。异常：无。
        """

        # 本适配器只验证源码与目录共同组成观测代，不解释修订和来源 URI。
        del workflow_revision, source_uri
        self.compile_calls.append((workflow_uuid, self.catalog_fingerprint))
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
            template_catalog_fingerprint=self.catalog_fingerprint,
        )


@pytest.fixture(autouse=True)
def _isolate_workflow_composition() -> Any:
    """隔离进程唯一工作流运行时（Workflow Runtime）。

    参数：无。
    返回：pytest 生命周期控制值。
    异常：组合根清理失败时原样传播，防止后续测试复用旧权威。
    """

    composition.reset_workflow_service_for_test()
    try:
        yield
    finally:
        composition.reset_workflow_service_for_test()


def _write_parent_before_child_package(package_root: Path) -> None:
    """写入父来源先于子来源的最小可编辑包（Editable Package）。

    参数：``package_root`` 是公开工作流组合根授权的包目录。
    返回：无；清单声明父、子两个稳定身份，源码内容只作为编译器输入证据。
    异常：文件系统写入失败时原样传播。
    """

    # ``source_root`` 是清单内两个工作流源码（Workflow Source）的规范父目录。
    source_root = package_root / "cold_start_lab" / "workflows"
    source_root.mkdir(parents=True)
    (source_root / "single_sample.py").write_text(
        "parent = material_transfer()\n",
        encoding="utf-8",
    )
    (source_root / "material_transfer.py").write_text(
        "child = transfer_material()\n",
        encoding="utf-8",
    )
    package_root.joinpath("package.yaml").write_text(
        "package:\n"
        "  name: cold_start_lab\n"
        "workflows:\n"
        f"  - workflow_uuid: {PARENT_WORKFLOW_UUID}\n"
        "    source: cold_start_lab/workflows/single_sample.py\n"
        f"  - workflow_uuid: {CHILD_WORKFLOW_UUID}\n"
        "    source: cold_start_lab/workflows/material_transfer.py\n",
        encoding="utf-8",
    )


def test_fresh_database_retries_parent_after_child_catalog_becomes_available(
    tmp_path: Path,
) -> None:
    """全新数据库冷启动编排必须把可前进目录回调推进到依赖固定点。

    参数：``tmp_path`` 隔离真实包目录与全新 ``workflow_history.db``。
    返回：无；断言父工作流先失败、子合同随后可用、父工作流同次启动再次编译并
    离开 ``draft_invalid``。本测试只覆盖固定点编排，不把普通子源码编译当成
    发布；真实发布另由 ``apply_authoring`` 回归覆盖。异常：若启动仍只做单轮恢复，本测试以
    ``composite_child_not_found`` 保留状态失败。
    """

    # ``package_root`` 是公开组合根唯一允许发现源码的可编辑包目录。
    package_root = tmp_path / "workspace"
    _write_parent_before_child_package(package_root)
    # ``compiler`` 模拟父来源先执行、随后外部目录事实在恢复回调间前进的顺序。
    compiler = ColdStartCompositeCompiler()

    service = composition.compose_workflow_runtime(
        tmp_path / "runtime",
        compiler=compiler,
        editable_package_roots=(package_root,),
        start_source_monitor=False,
    )

    # ``parent_authoring`` 是服务公开后的最终工作流创作（Authoring）投影。
    parent_authoring = service.get_authoring(PARENT_WORKFLOW_UUID)
    assert parent_authoring["state"] == "unapplied_source_only"
    assert parent_authoring["draft"]["diagnostics"] == []
    assert compiler.compiled_workflow_uuids == [
        PARENT_WORKFLOW_UUID,
        CHILD_WORKFLOW_UUID,
        PARENT_WORKFLOW_UUID,
    ]


def test_catalog_generation_change_recompiles_unchanged_workspace_source(
    tmp_path: Path,
) -> None:
    """模板目录换代必须让统一监视器重编译未改字节的工作流源码。

    参数：``tmp_path`` 隔离真实包与 SQLite。
    返回：无；断言源码文件签名不变时目录指纹仍改变公开观测签名，并通过
    ``submit_source_change`` 产生绑定新目录代际的候选版本（Candidate）。
    异常：若签名仍只含文件身份，本测试会在新旧签名相等处 RED。
    """

    # ``package_root`` 复用两个登记来源，但本断言只观察父工作流的稳定源码。
    package_root = tmp_path / "workspace"
    _write_parent_before_child_package(package_root)
    # ``compiler`` 的目录代际可独立于工作流源码字节推进。
    compiler = MutableCatalogCompiler()
    service = composition.compose_workflow_runtime(
        tmp_path / "runtime",
        compiler=compiler,
        editable_package_roots=(package_root,),
        start_source_monitor=False,
    )
    initial_signature = service.source_signature(PARENT_WORKFLOW_UUID)
    initial_compile_count = len(compiler.compile_calls)

    compiler.catalog_fingerprint = CHILD_READY_CATALOG_FINGERPRINT
    changed_signature = service.source_signature(PARENT_WORKFLOW_UUID)

    assert changed_signature != initial_signature
    assert service.submit_source_change(
        PARENT_WORKFLOW_UUID,
        observed_signature=changed_signature,
    )
    parent_authoring = service.get_authoring(PARENT_WORKFLOW_UUID)
    assert len(compiler.compile_calls) == initial_compile_count + 1
    assert parent_authoring["candidate"]["template_catalog_fingerprint"] == (
        CHILD_READY_CATALOG_FINGERPRINT
    )
    assert service.list_events(after_sequence=0)["items"][-1]["data"]["cause"] == (
        "catalog_changed"
    )


def test_applying_child_refreshes_parent_without_rewriting_parent_source(
    tmp_path: Path,
) -> None:
    """应用子工作流后必须自动重编译同目录的组合父工作流。

    参数：``tmp_path`` 隔离全新工作流、库存 SQLite 与真实作者源码。
    返回：无；先证明父工作流因已登记但未应用的子合同失败，再通过真实
    ``apply_authoring``、编译器重建器和模板投影发布子合同，最后断言父源码字节
    未修改也自动得到候选版本（Candidate）。异常：若仍需手工 PUT 父源码，父
    创作投影会继续保留 ``composite_child_unapplied`` 并使测试失败。
    """

    # ``selected_root`` 是生产组合根明确授权的可编辑包（Editable Package）。
    selected_root = tmp_path / "editable"
    selected_root.mkdir()
    _write_published_package(selected_root)
    # ``pyproject.toml`` 与包初始化文件让测试走产品工作区统一包目录编译路径，
    # 避免遗留可编辑根缺少模块/符号目录身份而退化为未登记语义。
    selected_root.joinpath("pyproject.toml").write_text(
        '[project]\nname = "c1-product-lab"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    selected_root.joinpath(PUBLISHED_PACKAGE_ID, "__init__.py").write_text(
        "",
        encoding="utf-8",
    )
    # ``source_plan`` 是工作区包目录（PackageCatalog）同次编译产生的完整来源
    # 身份，包含组合解析所需的 module、symbol 与内容哈希。
    workspace_source = WorkspaceSource(selected_root)
    package_catalog = compile_package_source(workspace_source)
    source_plan = workflow_source_plan_from_catalog(
        source=workspace_source,
        catalog=package_catalog,
    )
    # ``parent_source_path`` 是父工作流源码权威；测试前后字节必须完全相同。
    parent_source_path = (
        selected_root / PUBLISHED_PACKAGE_ID / "workflows" / "parent.py"
    )
    parent_source_before = parent_source_path.read_bytes()
    # ``inventory_store`` 为模板投影提供本地资源模板身份持久化边界。
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        service, _projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory_store,
            registry=_Registry(),
            editable_source_discovery_plan=source_plan,
            start_source_monitor=False,
        )
        initial_parent = service.get_authoring(PUBLISHED_PARENT_WORKFLOW_UUID)
        initial_codes = {
            diagnostic["code"]
            for diagnostic in initial_parent["draft"]["diagnostics"]
        }
        assert initial_parent["state"] == "draft_invalid"
        assert "composite_child_unapplied" in initial_codes
        assert "composite_child_not_found" not in initial_codes

        # ``child_candidate`` 是真实编译器为尚未发布的子工作流签发的持久候选。
        child_authoring = service.get_authoring(PUBLISHED_CHILD_WORKFLOW_UUID)
        child_candidate = child_authoring["candidate"]
        assert child_candidate is not None, child_authoring["draft"]["diagnostics"]
        service.apply_authoring(
            PUBLISHED_CHILD_WORKFLOW_UUID,
            candidate_hash=child_candidate["candidate_hash"],
        )

        # ``refreshed_parent`` 必须来自发布后目录依赖刷新，不能依赖文件重写事件。
        refreshed_parent = service.get_authoring(PUBLISHED_PARENT_WORKFLOW_UUID)
        assert refreshed_parent["state"] == "unapplied_graph"
        assert refreshed_parent["candidate"] is not None
        assert refreshed_parent["draft"]["diagnostics"] == []
        assert parent_source_path.read_bytes() == parent_source_before
    finally:
        composition.reset_workflow_service_for_test()
        inventory_store.close()


def test_restart_recompiles_persisted_parent_failure_against_current_catalog(
    tmp_path: Path,
) -> None:
    """启动恢复必须用当前发布目录替换父工作流的旧失败诊断。

    参数：``tmp_path`` 隔离真实工作区与 SQLite。
    返回：无；第一进程持久化 ``composite_child_unapplied``，停机期间子工作流
    获得同修订应用事实，第二进程不改父源码也必须生成父候选版本（Candidate）。
    异常：若启动只比较源码哈希并复用旧诊断，重启后的父状态仍为
    ``draft_invalid`` 并使测试失败。
    """

    # ``selected_root`` 包含真实父子作者源码和统一包目录声明。
    selected_root = tmp_path / "editable"
    selected_root.mkdir()
    _write_published_package(selected_root)
    selected_root.joinpath("pyproject.toml").write_text(
        '[project]\nname = "c1-product-lab"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    selected_root.joinpath(PUBLISHED_PACKAGE_ID, "__init__.py").write_text(
        "",
        encoding="utf-8",
    )
    workspace_source = WorkspaceSource(selected_root)
    package_catalog = compile_package_source(workspace_source)
    source_plan = workflow_source_plan_from_catalog(
        source=workspace_source,
        catalog=package_catalog,
    )
    inventory_store = InventoryStore(str(tmp_path / "inventory.db"))
    try:
        first_service, _first_projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory_store,
            registry=_Registry(),
            editable_source_discovery_plan=source_plan,
            start_source_monitor=False,
        )
        first_parent = first_service.get_authoring(PUBLISHED_PARENT_WORKFLOW_UUID)
        first_codes = {
            diagnostic["code"]
            for diagnostic in first_parent["draft"]["diagnostics"]
        }
        assert first_codes == {"composite_child_unapplied"}
        composition.reset_workflow_service_for_test()

        # ``_seed_applied_child`` 模拟进程关闭期间已经完成的子工作流发布事实；父
        # 源码和父创作记录均保持不变，用于验证启动恢复而非文件监视。
        _seed_applied_child(tmp_path, selected_root)
        restarted, _restarted_projection = compose_local_workflow_template_runtime(
            tmp_path,
            inventory_store=inventory_store,
            registry=_Registry(),
            editable_source_discovery_plan=source_plan,
            start_source_monitor=False,
        )
        restarted_parent = restarted.get_authoring(PUBLISHED_PARENT_WORKFLOW_UUID)

        assert restarted_parent["state"] == "unapplied_graph"
        assert restarted_parent["candidate"] is not None
        assert restarted_parent["draft"]["diagnostics"] == []
    finally:
        composition.reset_workflow_service_for_test()
        inventory_store.close()
