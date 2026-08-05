"""F06 R2 组合工作流调用（CompositeWorkflowInvocation）静态展开 RED。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from unilabos.workflow.authoring_identity import (
    authoring_edge_uuid,
    expanded_node_uuid,
)
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot
from unilabos.workflow.catalog import PublishedSourceCatalog
from unilabos.workflow.composite import (
    CompositeAuthoring,
    project_published_workflow_contract,
)

PARENT_WORKFLOW_UUID = "44444444-4444-4444-8444-444444444444"
INVOCATION_UUID = "11111111-1111-4111-8111-111111111111"
OTHER_INVOCATION_UUID = "11111111-1111-4111-8111-111111111112"
CHILD_WORKFLOW_UUID = "a1000000-0000-4000-8000-000000000001"
CHILD_NODE_UUID = "22222222-2222-4222-8222-222222222222"
EXPANDED_CHILD_NODE_UUID = "b6b35f79-80d0-5b77-a0eb-9646bcb36808"
EXPANDED_GRANDCHILD_NODE_UUID = "7b221513-105e-5c92-9859-1a3c2015fafb"
EXPANDED_EDGE_UUID = "b3e67370-ee6e-54b5-9dd1-6d44c5a5854f"
HOST_RESOURCE_TEMPLATE_UUID = "a2000000-0000-4000-8000-000000000001"
ACTION_RESOURCE_TEMPLATE_UUID = "a2000000-0000-4000-8000-000000000002"
ACTION_TEMPLATE_UUID = "a3000000-0000-4000-8000-000000000001"
CHILD_TEMPLATE_UUID = "a3000000-0000-4000-8000-000000000011"
ACTION_VALUE_TARGET_UUID = "a4000000-0000-4000-8000-000000000001"
ACTION_VALUE_SOURCE_UUID = "55555555-5555-4555-8555-555555555555"
ACTION_READY_TARGET_UUID = "a4000000-0000-4000-8000-000000000003"
ACTION_READY_SOURCE_UUID = "a4000000-0000-4000-8000-000000000004"
GRANDCHILD_VALUE_TARGET_UUID = "66666666-6666-4666-8666-666666666666"
CHILD_VALUE_TARGET_UUID = "a5000000-0000-4000-8000-000000000001"
CHILD_VALUE_SOURCE_UUID = "a5000000-0000-4000-8000-000000000002"
CHILD_READY_TARGET_UUID = "a5000000-0000-4000-8000-000000000003"
CHILD_READY_SOURCE_UUID = "a5000000-0000-4000-8000-000000000004"
APPLIED_SOURCE_HASH = "sha256:" + "3" * 64
CONTRACT_DIGEST = (
    "sha256:689aaac733eba27d13279d242a71fc3c8bc41f0c144d41261dc160a52b46a1cf"
)


@dataclass
class MemorySnapshotProvider:
    """只读返回已发布工作流快照并记录读取次数的测试端口。"""

    snapshots: dict[str, dict[str, Any]]
    read_count: int = 0

    def get_published_workflow_snapshot(self, workflow_uuid: str) -> dict[str, Any]:
        """按工作流 UUID 返回快照副本；不存在时抛出 ``LookupError``。"""

        self.read_count += 1
        try:
            return self.snapshots[workflow_uuid]
        except KeyError:
            raise LookupError(workflow_uuid) from None


def _source_catalog() -> PublishedSourceCatalog:
    """构造只含一个子工作流来源的已发布源码目录。"""

    return PublishedSourceCatalog.from_records(
        [
            {
                "workflow_uuid": CHILD_WORKFLOW_UUID,
                "definition_fqid": "c1_published_lab.workflows.prepare_sample",
                "module": "c1_published_lab.workflows.child",
                "symbol": "prepare_sample",
                "source_uri": "package://c1_published_lab/workflows/child.py",
                "definition_content_hash": "sha256:" + "1" * 64,
            }
        ]
    )


def _handle(
    handle_uuid: str,
    key: str,
    io_type: str,
    *,
    ready: bool = False,
) -> dict[str, Any]:
    """构造动作节点的数值或 ready 连接点（Handle）模板。"""

    value_type = "boolean" if ready else "number"
    return {
        "uuid": handle_uuid,
        "workflow_node_template_uuid": ACTION_TEMPLATE_UUID,
        "handle_key": key,
        "io_type": io_type,
        "display_name": key.title(),
        "description": "",
        "type": value_type,
        "required": io_type == "target" and not ready,
        "data_source": "dependency" if ready else "executor",
        "data_key": key,
        "meta_data": {
            "unilab": {
                "value_schema": {"type": value_type},
                **({"structural_role": "ready"} if ready else {}),
            }
        },
    }


def _applied_snapshot() -> dict[str, Any]:
    """构造一个单动作且输入输出边界完整的已应用子工作流。"""

    timestamp = "2026-08-02T00:00:00Z"
    return {
        "workflow": {
            "uuid": CHILD_WORKFLOW_UUID,
            "revision": 7,
            "name": "Prepare sample",
            "tags": [],
            "description": "fixture",
            "create_time": timestamp,
            "update_time": timestamp,
            "meta_data": {
                "unilab": {
                    "input_contract": {
                        "version": 1,
                        "parameters": [
                            {
                                "name": "value",
                                "schema": {"type": "number"},
                                "required": True,
                            }
                        ],
                    },
                    "output_contract": {
                        "version": 1,
                        "outputs": [
                            {
                                "name": "result",
                                "schema": {"type": "number"},
                                "implicit": False,
                            }
                        ],
                    },
                    "output_bindings": {
                        "result": {
                            "kind": "node_output",
                            "workflow_node_uuid": CHILD_NODE_UUID,
                            "source_handle_uuid": ACTION_VALUE_SOURCE_UUID,
                        }
                    },
                }
            },
        },
        "applied_source": {
            "workflow_revision": 7,
            "source_hash": APPLIED_SOURCE_HASH,
            "python_source": "def prepare_sample(*, value: float): ...\n",
            "source_map": [],
            "compiler_version": "fixture",
            "template_catalog_fingerprint": "sha256:" + "4" * 64,
        },
        "nodes": [
            {
                "uuid": CHILD_NODE_UUID,
                "workflow_uuid": CHILD_WORKFLOW_UUID,
                "workflow_node_template_uuid": ACTION_TEMPLATE_UUID,
                "parent_uuid": None,
                "name": "measure",
                "status": "idle",
                "type": "device",
                "pose": {},
                "param": {},
                "execution_policy": {},
                "disabled": False,
                "minimized": False,
                "meta_data": {
                    "unilab": {
                        "input_bindings": {
                            ACTION_VALUE_TARGET_UUID: {"parameter": "value"}
                        }
                    }
                },
                "create_time": timestamp,
                "update_time": timestamp,
            }
        ],
        "edges": [],
        "node_templates": [_action_template()],
        "handle_templates": _action_handles(),
    }


def _action_template() -> dict[str, Any]:
    """构造内部动作节点模板。"""

    return {
        "uuid": ACTION_TEMPLATE_UUID,
        "resource_template_uuid": ACTION_RESOURCE_TEMPLATE_UUID,
        "name": "measure",
        "display_name": "Measure",
        "description": "fixture",
        "class": "c1_published_lab.devices:Measure",
        "goal": {"value": "value"},
        "goal_default": {},
        "feedback": {},
        "result": {"result": "result"},
        "schema": None,
        "type": "action",
        "node_type": "device",
        "meta_data": {},
    }


def _action_handles() -> list[dict[str, Any]]:
    """返回内部动作的业务与结构连接点全集。"""

    return [
        _handle(ACTION_VALUE_TARGET_UUID, "value", "target"),
        _handle(ACTION_VALUE_SOURCE_UUID, "result", "source"),
        _handle(ACTION_READY_TARGET_UUID, "ready", "target", ready=True),
        _handle(ACTION_READY_SOURCE_UUID, "ready", "source", ready=True),
    ]


def _world_components() -> tuple[
    CompositeAuthoring,
    MemorySnapshotProvider,
    AuthoringCatalogSnapshot,
    PublishedSourceCatalog,
]:
    """装配并暴露失败关闭测试所需的四个只读组件。"""

    source_catalog = _source_catalog()
    source = source_catalog.resolve(
        "c1_published_lab.workflows.child",
        "prepare_sample",
    )
    snapshot = _applied_snapshot()
    projected = project_published_workflow_contract(
        source=source,
        applied_snapshot=snapshot,
        host_node_resource_template={
            "uuid": HOST_RESOURCE_TEMPLATE_UUID,
            "name": "host_node",
            "display_name": "Host Node",
        },
    )
    assert projected is not None
    workflow_template = {**projected.template, "uuid": CHILD_TEMPLATE_UUID}
    handle_uuids = (
        CHILD_VALUE_TARGET_UUID,
        CHILD_VALUE_SOURCE_UUID,
        CHILD_READY_TARGET_UUID,
        CHILD_READY_SOURCE_UUID,
    )
    workflow_handles = [
        {
            **handle,
            "uuid": handle_uuid,
            "workflow_node_template_uuid": CHILD_TEMPLATE_UUID,
        }
        for handle, handle_uuid in zip(projected.handles, handle_uuids, strict=True)
    ]
    catalog = AuthoringCatalogSnapshot.from_entities(
        [_action_template(), workflow_template],
        [*_action_handles(), *workflow_handles],
    )
    provider = MemorySnapshotProvider({CHILD_WORKFLOW_UUID: snapshot})
    authoring = CompositeAuthoring(
        snapshot_provider=provider,
        catalog=catalog,
        resolver=source_catalog,
    )
    return authoring, provider, catalog, source_catalog


def _world() -> tuple[CompositeAuthoring, MemorySnapshotProvider]:
    """装配纯内存目录、只读快照端口和组合创作接口。"""

    authoring, provider, _catalog, _source_catalog = _world_components()
    return authoring, provider


def test_direct_invocation_returns_hierarchical_expansion_mappings_and_pin() -> None:
    """直接调用生成真实调用节点、确定性内部节点、边界映射和冻结 pin。"""

    authoring, provider = _world()
    expansion = authoring.compile_invocation(
        parent_workflow_uuid=PARENT_WORKFLOW_UUID,
        invocation_uuid=INVOCATION_UUID,
        module="c1_published_lab.workflows.child",
        symbol="prepare_sample",
        keyword_arguments={"value": 7.5},
    )

    assert expansion.diagnostics == ()
    assert expansion.invocation_node is not None
    assert expansion.invocation_node["uuid"] == INVOCATION_UUID
    assert expansion.invocation_node["workflow_node_template_uuid"] == (
        CHILD_TEMPLATE_UUID
    )
    assert expansion.invocation_node["param"] == {"value": 7.5}
    assert [node["uuid"] for node in expansion.nodes] == [
        EXPANDED_CHILD_NODE_UUID
    ]
    assert expansion.nodes[0]["parent_uuid"] == INVOCATION_UUID
    assert expansion.target_mappings == {
        CHILD_VALUE_TARGET_UUID: (
            {
                "workflow_node_uuid": EXPANDED_CHILD_NODE_UUID,
                "target_handle_uuid": ACTION_VALUE_TARGET_UUID,
            },
        )
    }
    assert expansion.source_mappings == {
        CHILD_VALUE_SOURCE_UUID: {
            "kind": "node_output",
            "workflow_node_uuid": EXPANDED_CHILD_NODE_UUID,
            "source_handle_uuid": ACTION_VALUE_SOURCE_UUID,
        }
    }
    assert expansion.structural_mappings == {
        "entry_targets": (
            {
                "workflow_node_uuid": EXPANDED_CHILD_NODE_UUID,
                "target_handle_uuid": ACTION_READY_TARGET_UUID,
            },
        ),
        "completion_sources": (
            {
                "workflow_node_uuid": EXPANDED_CHILD_NODE_UUID,
                "source_handle_uuid": ACTION_READY_SOURCE_UUID,
            },
        ),
    }
    assert expansion.contract_pin == {
        "child_workflow_uuid": CHILD_WORKFLOW_UUID,
        "child_workflow_revision": 7,
        "child_applied_source_hash": APPLIED_SOURCE_HASH,
        "contract_digest": CONTRACT_DIGEST,
        "composition_allow_transparent": False,
    }
    assert provider.read_count == 1
def test_two_invocations_share_templates_but_not_expanded_node_identity() -> None:
    """重复调用共享目录模板，但每次调用拥有不同展开节点身份。"""

    authoring, _provider = _world()
    first = authoring.compile_invocation(
        parent_workflow_uuid=PARENT_WORKFLOW_UUID,
        invocation_uuid=INVOCATION_UUID,
        module="c1_published_lab.workflows.child",
        symbol="prepare_sample",
        keyword_arguments={"value": 1},
    )
    second = authoring.compile_invocation(
        parent_workflow_uuid=PARENT_WORKFLOW_UUID,
        invocation_uuid=OTHER_INVOCATION_UUID,
        module="c1_published_lab.workflows.child",
        symbol="prepare_sample",
        keyword_arguments={"value": 1},
    )

    assert first.nodes[0]["uuid"] == EXPANDED_CHILD_NODE_UUID
    assert second.nodes[0]["uuid"] != first.nodes[0]["uuid"]
    assert second.nodes[0]["uuid"] == expanded_node_uuid(
        OTHER_INVOCATION_UUID,
        CHILD_NODE_UUID,
    )
    assert {item["uuid"] for item in first.node_templates} == {
        item["uuid"] for item in second.node_templates
    }


def test_recursive_or_uncovered_invocation_fails_without_snapshot_write_port() -> None:
    """递归引用和缺失必填边界参数只返回诊断，端口保持只读。"""

    authoring, provider = _world()
    recursive = authoring.compile_invocation(
        parent_workflow_uuid=CHILD_WORKFLOW_UUID,
        invocation_uuid=INVOCATION_UUID,
        module="c1_published_lab.workflows.child",
        symbol="prepare_sample",
        keyword_arguments={"value": 1},
    )
    uncovered = authoring.compile_invocation(
        parent_workflow_uuid=PARENT_WORKFLOW_UUID,
        invocation_uuid=INVOCATION_UUID,
        module="c1_published_lab.workflows.child",
        symbol="prepare_sample",
        keyword_arguments={},
    )

    assert recursive.invocation_node is None
    assert recursive.diagnostics[0]["code"] == "composite_recursive_reference"
    assert uncovered.invocation_node is None
    assert uncovered.diagnostics[0]["code"] == (
        "composite_boundary_mapping_invalid"
    )
    assert provider.read_count == 1


def test_composite_uuid_and_root_edge_vectors_remain_byte_stable() -> None:
    """C1 冻结的节点与父工作流边 UUID 向量保持字节级稳定。"""

    assert expanded_node_uuid(INVOCATION_UUID, CHILD_NODE_UUID) == (
        EXPANDED_CHILD_NODE_UUID
    )
    assert authoring_edge_uuid(
        workflow_uuid=PARENT_WORKFLOW_UUID,
        source_node_uuid=EXPANDED_CHILD_NODE_UUID,
        source_handle_uuid=ACTION_VALUE_SOURCE_UUID,
        target_node_uuid=EXPANDED_GRANDCHILD_NODE_UUID,
        target_handle_uuid=GRANDCHILD_VALUE_TARGET_UUID,
    ) == EXPANDED_EDGE_UUID
