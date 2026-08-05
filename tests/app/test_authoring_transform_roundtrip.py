"""可信工作流创作纯转换真实引擎往返与持久纯度测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from tests.workflow.test_authoring_engine import (
    WORKFLOW_UUID,
    _applied_graph,
    _engine,
    _source,
)
from unilabos.app.workflow_api import create_workflow_app
from unilabos.app.workflow_authoring_transform import create_authoring_transform_app
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore

SOURCE_URI = "package://lab/workflows/f05-roundtrip.py"


def _post(
    client: TestClient,
    path: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    """提交一次纯转换并取得闭合成功数据。

    参数：``client`` 是隔离 HTTP 客户端，``path`` 是转换路由，``body`` 是请求 DTO。
    返回：业务码为零的 ``data``；任何业务错误由断言立即暴露。
    """

    response = client.post(path, json=body)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["code"] == 0, payload
    return payload["data"]


def _compile_body(
    *,
    source: str,
    graph: dict[str, Any],
    revision: int = 7,
) -> dict[str, Any]:
    """构造真实引擎编译请求。

    参数：``source`` 是工作流源码（Workflow Source），``graph`` 是应用图基线，
    ``revision`` 是对应正整数修订。返回：闭合编译 DTO。
    """

    return {
        "workflow_uuid": WORKFLOW_UUID,
        "revision": revision,
        "python_source": source,
        "source_uri": SOURCE_URI,
        "applied_graph": graph,
    }


def _generate_body(
    graph: dict[str, Any],
    *,
    revision: int = 7,
) -> dict[str, Any]:
    """构造真实引擎 Python 生成请求。

    参数：``graph`` 是待规范化图，``revision`` 是对应修订。返回：闭合生成 DTO。
    """

    return {
        "workflow_uuid": WORKFLOW_UUID,
        "revision": revision,
        "graph": graph,
        "source_uri": SOURCE_URI,
    }


def _validate_body(
    graph: dict[str, Any],
    source: str,
    *,
    revision: int = 7,
) -> dict[str, Any]:
    """构造真实引擎共同校验请求。

    参数：``graph`` 与 ``source`` 应描述同一工作流（Workflow），``revision`` 是修订。
    返回：闭合校验 DTO。
    """

    return {**_generate_body(graph, revision=revision), "python_source": source}


def test_real_engine_compile_generate_validate_reaches_a_fixed_point() -> None:
    """真实引擎必须经编译、生成、再编译和校验达到同一语义固定点。

    参数：无。返回：无；断言生成与校验保持图不变且只产生空源码变更集。
    """

    engine = _engine()
    with TestClient(create_authoring_transform_app(engine)) as client:
        compiled = _post(
            client,
            "/api/v1/authoring/compile",
            _compile_body(source=_source(), graph=_applied_graph()),
        )
        generated = _post(
            client,
            "/api/v1/authoring/generate-python",
            _generate_body(compiled["graph"]),
        )
        recompiled = _post(
            client,
            "/api/v1/authoring/compile",
            _compile_body(
                source=generated["normalized_python_source"],
                graph=compiled["graph"],
            ),
        )
        validated = _post(
            client,
            "/api/v1/authoring/validate",
            _validate_body(
                compiled["graph"],
                generated["normalized_python_source"],
            ),
        )

    assert generated["graph"] == compiled["graph"]
    assert validated["graph"] == compiled["graph"]
    assert recompiled["graph"] == compiled["graph"]
    assert generated["changeset"] == validated["changeset"]
    assert generated["changeset"]["kind"] == "source_only"
    assert all(
        not value
        for key, value in generated["changeset"].items()
        if key not in {"kind", "reserved_metadata_changed"}
    )


def test_transform_routes_do_not_write_workflow_history(
    tmp_path: Path,
) -> None:
    """挂入真实工作流应用后，纯转换请求不得改变任何工作流持久表。

    参数：``tmp_path`` 提供隔离的 ``workflow_history.db``。返回：无；断言逐表行数不变。
    """

    database_path = tmp_path / "workflow_history.db"
    store = WorkflowStore(database_path)
    engine = _engine()
    service = WorkflowService(store, compiler=engine)
    service.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="Pure transform authority",
        tags=[],
        description=None,
        meta_data={},
    )
    # ``persistent_tables`` 覆盖定义、创作、任务和作业事实，防止纯转换暗发候选身份。
    persistent_tables = (
        "workflow",
        "workflow_node",
        "workflow_edge",
        "workflow_authoring",
        "workflow_task",
        "workflow_node_job",
    )
    before = {table: store.count_rows(table) for table in persistent_tables}
    graph = service.get_graph(WORKFLOW_UUID)
    revision = graph["workflow"]["revision"]

    try:
        with TestClient(
            create_workflow_app(service, authoring_transform=engine)
        ) as client:
            result = _post(
                client,
                "/api/v1/authoring/compile",
                _compile_body(source=_source(), graph=graph, revision=revision),
            )
        assert result["graph"] is not None
        assert {table: store.count_rows(table) for table in persistent_tables} == before
    finally:
        service.close()


def _utf16_column(line: str, codepoint_offset: int) -> int:
    """独立计算一基 UTF-16 列号。

    参数：``line`` 是单行文本，``codepoint_offset`` 是 Python 字符偏移。返回列号。
    """

    return len(line[:codepoint_offset].encode("utf-16-le")) // 2 + 1


def test_real_http_diagnostic_uses_utf16_for_chinese_emoji_and_tab() -> None:
    """中文、emoji 和 Tab 前缀后的诊断范围必须使用一基 UTF-16 坐标。

    参数：无。返回：无；断言 ``unknown()`` 的起止列与独立固定向量一致。
    """

    original_action = '''    # unilab:node_uuid=20000000-0000-4000-8000-000000000001
    prepared = reactor.prepare(sample=sample, cycles=cycles)'''
    source = _source().replace(
        original_action,
        '    "中😀\t"; invalid = unknown()',
        1,
    )
    line_number = next(
        index
        for index, line in enumerate(source.splitlines(), 1)
        if "unknown()" in line
    )
    line = source.splitlines()[line_number - 1]
    start = line.index("unknown()")

    with TestClient(create_authoring_transform_app(_engine())) as client:
        result = _post(
            client,
            "/api/v1/authoring/compile",
            _compile_body(source=source, graph=_applied_graph()),
        )

    diagnostic = result["diagnostics"][0]
    assert diagnostic["code"] == "invalid_action_call"
    assert diagnostic["source_range"] == {
        "start_line": line_number,
        "start_column": _utf16_column(line, start),
        "end_line": line_number,
        "end_column": _utf16_column(line, start + len("unknown()")),
    }


def test_real_http_syntax_position_uses_utf16_end_exclusive() -> None:
    """语法错误位置必须保留中文、emoji 和 Tab 固定向量的 UTF-16 列号。

    参数：无。返回：无；断言真实 HTTP 诊断端点位置为第 17 列。
    """

    with TestClient(create_authoring_transform_app(_engine())) as client:
        result = _post(
            client,
            "/api/v1/authoring/compile",
            _compile_body(
                source='value = "中😀\t"; )\n',
                graph=_applied_graph(),
            ),
        )

    source_range = result["diagnostics"][0]["source_range"]
    assert source_range == {
        "start_line": 1,
        "start_column": 17,
        "end_line": 1,
        "end_column": 17,
    }
