"""工作流创作图的稳定语义比较回归测试。"""

from __future__ import annotations

from copy import deepcopy

from unilabos.workflow.authoring_graph_semantics import (
    candidate_changeset,
    semantic_graph_equal,
)


def test_candidate_changeset_ignores_node_runtime_status() -> None:
    """节点运行态不得把局部作者编辑扩大为整图更新。

    参数：无。返回：无；构造只在 ``status`` 上不同的前后图，断言变更集为
    ``source_only``。异常：若运行态进入作者语义，候选编译与后端补形会分别
    得到不同的更新节点集合，签发最终降级为 ``candidate_invalid``。
    """

    applied = _graph(status="succeeded")
    candidate = deepcopy(applied)
    candidate["nodes"][0].pop("status")

    changeset = candidate_changeset(graph=candidate, applied_graph=applied)

    assert changeset["kind"] == "source_only"
    assert changeset["updated_node_uuids"] == []
    assert semantic_graph_equal(candidate, applied)


def _graph(*, status: str) -> dict[str, object]:
    """构造带后端运行态的最小五集合工作流图。"""

    return {
        "workflow": {
            "uuid": "10000000-0000-4000-8000-000000000001",
            "revision": 1,
            "name": "Runtime status boundary",
            "tags": [],
            "meta_data": {},
        },
        "nodes": [
            {
                "uuid": "20000000-0000-4000-8000-000000000001",
                "name": "prepare",
                "type": "device",
                "status": status,
                "pose": {},
                "param": {},
                "execution_policy": {},
                "disabled": False,
                "minimized": False,
                "meta_data": {},
            }
        ],
        "edges": [],
        "node_templates": [],
        "handle_templates": [],
    }
