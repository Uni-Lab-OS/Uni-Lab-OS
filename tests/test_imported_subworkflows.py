from pathlib import Path

from unilabos.workflow.from_python_script import compile_python_script
from unilabos.workflow.source_library import WorkflowSourceLibrary
from unilabos.workflow.to_python_script import generate_python_revision

ACTION_CATALOG = {
    "reader.read": {
        "inputs": {"key": {"type": "string"}},
        "outputs": {"value": {"type": "string"}},
    },
    "sink.consume": {
        "inputs": {"value": {"type": "string"}},
        "outputs": {},
    },
}

CHILD_SOURCE = """\
from unilabos.workflow.authoring import device, workflow_definition, workflow_output

reader = device("reader")

@workflow_definition(workflow_id="read_value", revision="child-r1")
def read_value(*, key: str = "sample") -> str:
    result = reader.read(key=key)
    return workflow_output(value=result.value)
"""

ROOT_SOURCE = """\
from unilabos.workflow.authoring import device, workflow_definition
from lab.workflows import read_value

sink = device("sink")

@workflow_definition(workflow_id="root", revision="root-r1")
def root(*, key: str = "sample") -> None:
    value = read_value(key=key)
    sink.consume(value=value)
"""


def _resolver(module: str, symbol: str) -> str | None:
    if (module, symbol) == ("lab.workflows", "read_value"):
        return CHILD_SOURCE
    return None


def test_imported_workflow_compiles_to_collapsible_group_and_roundtrips() -> None:
    revision = compile_python_script(
        ROOT_SOURCE,
        action_catalog=ACTION_CATALOG,
        workflow_source_resolver=_resolver,
    )

    assert [item.node_type for item in revision.invocations] == [
        "group",
        "action",
        "action",
    ]
    group = revision.invocations[0]
    assert group.control["name"] == "subworkflow::read_value"
    assert group.control["callable"]["module"] == "lab.workflows"
    assert group.control["callable"]["outputs"]["value"]["target"] == "value"
    scope = next(
        entry.compiled_node_ids
        for entry in revision.source_map.entries
        if entry.node_id == group.node_id
    )
    assert scope == [
        revision.invocations[0].node_id,
        revision.invocations[1].node_id,
    ]

    generated = generate_python_revision(
        {
            "base_revision_id": revision.revision_id,
            "canonical_ir": revision.model_dump(mode="json"),
            "source_uri": "root.py",
        },
        action_catalog=ACTION_CATALOG,
        workflow_source_resolver=_resolver,
    )
    assert generated["diagnostics"] == []
    source = generated["candidate"]["python_source"]
    assert "from lab.workflows import read_value" in source
    assert "value = read_value(key=key)" in source
    assert "reader.read(" not in source
    assert "with group(" not in source

    recompiled = compile_python_script(
        source,
        action_catalog=ACTION_CATALOG,
        workflow_source_resolver=_resolver,
    )
    assert recompiled.content_hash == revision.content_hash


def test_source_library_indexes_decorated_functions_without_importing(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "read_value.py"
    source_path.write_text(CHILD_SOURCE, encoding="utf-8")
    library = WorkflowSourceLibrary([("lab.workflows", tmp_path)])

    assert library.resolve("lab.workflows", "read_value") == CHILD_SOURCE
    assert library.resolve("other.workflows", "read_value") is None
