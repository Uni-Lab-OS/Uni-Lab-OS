"""设备注册表模板投影（Registry Template Projection）的全图参数合同测试。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from unilabos.registry.action_template_projection import goal_parameter_schema
from unilabos.workflow.graph_validation import GraphValidationError, validate_graph
from unilabos.workflow.models import WorkflowNodeWrite

TEMPLATE_UUID = "10000000-0000-4000-8000-000000000001"
NODE_UUID = "20000000-0000-4000-8000-000000000001"


def _validate_single_node_schema(raw_schema: Any, param: dict[str, Any]) -> None:
    """通过公共全图校验接缝验证一个节点参数 JSON Schema。

    参数说明：``raw_schema`` 是节点模板保存的原始 JSON Schema，``param`` 是
    工作流节点（WorkflowNode）的最终参数。返回：无；参数满足模板合同则正常
    返回。异常：Schema 容器或参数值无效时抛出 ``GraphValidationError``。
    """

    # ``workflow_node`` 模拟引用设备注册表模板投影的单个计算节点。
    workflow_node = WorkflowNodeWrite(
        uuid=NODE_UUID,
        workflow_node_template_uuid=TEMPLATE_UUID,
        name="校验节点参数",
        type="compute",
        param=param,
    )
    validate_graph(
        nodes=[workflow_node],
        edges=[],
        templates={
            TEMPLATE_UUID: {
                "uuid": TEMPLATE_UUID,
                "node_type": "compute",
                "schema": raw_schema,
            }
        },
        handles={},
        effective_params={NODE_UUID: param},
        workflow_meta_data={},
        node_meta_data={NODE_UUID: {}},
    )


def test_projected_mapping_schema_crosses_public_graph_validation() -> None:
    """投影生成的字典参数 Schema 应直接通过公共全图校验且保持输入不变。"""

    # ``projected_schema`` 使用设备注册表模板投影生产路径的同一编译函数生成。
    projected_schema = goal_parameter_schema(
        {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "object",
                    "properties": {"volume": {"type": "number"}},
                    "required": ["volume"],
                    "additionalProperties": False,
                }
            },
        }
    )
    original_schema = deepcopy(projected_schema)

    _validate_single_node_schema(projected_schema, {"volume": 1.5})

    assert projected_schema == original_schema


@pytest.mark.parametrize(
    "raw_schema",
    [
        pytest.param(
            '{"type":"object","properties":{"volume":{"type":"number"}}}',
            id="json-object-string",
        ),
        pytest.param(True, id="boolean-true"),
        pytest.param("true", id="json-boolean-string"),
    ],
)
def test_compatible_serialized_and_boolean_schemas_remain_valid(
    raw_schema: Any,
) -> None:
    """合法 JSON 字符串和放行布尔 Schema 应保持既有兼容行为。

    参数说明：``raw_schema`` 是一种合法的既有节点模板 Schema 表示。返回：无；
    断言公共全图校验继续接受该表示，不因支持投影字典而改变 wire 兼容性。
    """

    _validate_single_node_schema(raw_schema, {"volume": 1.5})


@pytest.mark.parametrize(
    "raw_schema",
    [
        pytest.param([{"type": "object"}], id="array-root"),
        pytest.param("{'type': 'object'}", id="python-repr-string"),
        pytest.param("{", id="malformed-json-string"),
    ],
)
def test_invalid_schema_representations_fail_closed(raw_schema: Any) -> None:
    """非法 Schema 对象或字符串不得绕过公共全图参数校验。

    参数说明：``raw_schema`` 是不符合节点模板 JSON Schema 合同的表示。返回：
    无；断言校验稳定抛出 ``GraphValidationError``，不会把 Python repr 当作 JSON。
    """

    with pytest.raises(GraphValidationError, match="节点参数 JSON Schema"):
        _validate_single_node_schema(raw_schema, {"volume": 1.5})


@pytest.mark.parametrize(
    "raw_schema",
    [
        pytest.param(False, id="boolean-false"),
        pytest.param("false", id="json-boolean-false-string"),
    ],
)
def test_rejecting_boolean_schemas_keep_their_json_schema_semantics(
    raw_schema: Any,
) -> None:
    """拒绝布尔 Schema 应继续拒绝任意节点参数。

    参数说明：``raw_schema`` 是对象值或 JSON 字符串形式的拒绝布尔 Schema。
    返回：无；断言公共全图校验按 JSON Schema 语义关闭式失败。
    """

    with pytest.raises(GraphValidationError, match="不满足 JSON Schema"):
        _validate_single_node_schema(raw_schema, {"volume": 1.5})
