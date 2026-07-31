"""Round 02C independent-review blocking regressions."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import pytest

from unilabos.workflow.catalog import (
    CatalogAuthority,
    NodeTemplateImport,
    TemplateCatalog,
    TemplateCatalogMismatch,
)
from unilabos.workflow.store import WorkflowStore

LOCAL_AUTHORITY = CatalogAuthority(authority_id="os-local", kind="local")
BACKEND_AUTHORITY = CatalogAuthority(authority_id="backend-main", kind="backend")

RESOURCE_TEMPLATE_UUID = "10000000-0000-4000-8000-000000000001"
NODE_TEMPLATE_UUID = "20000000-0000-4000-8000-000000000001"
HANDLE_TEMPLATE_UUID = "30000000-0000-4000-8000-000000000001"


def _node(
    name: str,
    *,
    template_uuid: str | None = None,
    handles: list[Mapping[str, object]] | None = None,
) -> NodeTemplateImport:
    template: dict[str, object] = {
        "resource_template_uuid": RESOURCE_TEMPLATE_UUID,
        "name": name,
        "display_name": name,
        "meta_data": {},
        "goal": {},
        "goal_default": {},
        "feedback": {},
        "result": {},
        "type": "action",
        "node_type": "compute",
    }
    if template_uuid is not None:
        template["uuid"] = template_uuid
    return NodeTemplateImport(template=template, handles=handles or [])


def _handle(
    handle_key: str,
    *,
    handle_uuid: str | None = None,
) -> dict[str, object]:
    handle: dict[str, object] = {
        "handle_key": handle_key,
        "io_type": "target",
        "display_name": handle_key,
        "type": "ResourceSlot",
        "required": True,
        "meta_data": {},
    }
    if handle_uuid is not None:
        handle["uuid"] = handle_uuid
    return handle


def _backend_aggregate() -> list[NodeTemplateImport]:
    return [
        _node(
            "heat",
            template_uuid=NODE_TEMPLATE_UUID,
            handles=[
                _handle("material", handle_uuid=HANDLE_TEMPLATE_UUID),
            ],
        )
    ]


@contextmanager
def _open_catalog(
    database_path: Path,
) -> Iterator[tuple[WorkflowStore, TemplateCatalog]]:
    store = WorkflowStore(database_path)
    try:
        yield store, TemplateCatalog(store)
    finally:
        store.close()


class _TracedWorkflowStore(WorkflowStore):
    """Capture executed transaction SQL without changing Catalog behavior."""

    def __init__(self, database_path: Path):
        super().__init__(database_path)
        self.statements: list[str] = []

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with super().transaction() as connection:
            connection.set_trace_callback(self.statements.append)
            try:
                yield connection
            finally:
                connection.set_trace_callback(None)


def _normalized_sql(statement: str) -> str:
    return " ".join(statement.lower().split())


def _template_uuid_statements(statements: list[str]) -> list[tuple[str, str, str]]:
    observed: list[tuple[str, str, str]] = []
    for raw_statement in statements:
        statement = _normalized_sql(raw_statement)
        verb = statement.partition(" ")[0]
        if verb not in {"select", "update"} or " where " not in statement:
            continue
        where_clause = statement.partition(" where ")[2]
        if re.search(r"\buuid\b", where_clause) is None:
            continue
        for table in (
            "workflow_node_template",
            "workflow_handle_template",
        ):
            if re.search(rf"\b{table}\b", statement):
                observed.append((table, verb, where_clause))
                break
    return observed


def test_business_identity_uses_lower_trim_not_unicode_casefold(
    tmp_path: Path,
) -> None:
    aggregate = [
        _node(
            "Straße",
            handles=[_handle("Maße"), _handle("MASSE")],
        ),
        _node("STRASSE"),
    ]

    with _open_catalog(tmp_path / "workflow.db") as (_store, catalog):
        snapshot = catalog.replace(LOCAL_AUTHORITY, aggregate)

    assert {node["name"] for node in snapshot.node_templates} == {
        "Straße",
        "STRASSE",
    }
    assert len({node["uuid"] for node in snapshot.node_templates}) == 2
    assert {handle["handle_key"] for handle in snapshot.handle_templates} == {
        "Maße",
        "MASSE",
    }
    assert len({handle["uuid"] for handle in snapshot.handle_templates}) == 2


def test_template_uuid_persistence_sql_is_explicitly_authority_scoped(
    tmp_path: Path,
) -> None:
    store = _TracedWorkflowStore(tmp_path / "workflow.db")
    try:
        catalog = TemplateCatalog(store)
        catalog.replace(BACKEND_AUTHORITY, _backend_aggregate())
        catalog.replace(BACKEND_AUTHORITY, _backend_aggregate())
    finally:
        store.close()

    observed = _template_uuid_statements(store.statements)
    statement_kinds = {(table, verb) for table, verb, _where in observed}
    unscoped = [
        (table, verb, where)
        for table, verb, where in observed
        if re.search(r"\bauthority_id\b", where) is None
    ]

    assert statement_kinds.issuperset(
        {
            ("workflow_node_template", "select"),
            ("workflow_node_template", "update"),
            ("workflow_handle_template", "select"),
            ("workflow_handle_template", "update"),
        }
    )
    assert unscoped == []


def test_snapshot_maps_corrupt_scalar_blob_to_stable_catalog_mismatch(
    tmp_path: Path,
) -> None:
    with _open_catalog(tmp_path / "workflow.db") as (store, catalog):
        snapshot = catalog.replace(LOCAL_AUTHORITY, [_node("heat")])
        node_uuid = snapshot.node_templates[0]["uuid"]
        with store.transaction() as connection:
            connection.execute(
                """
                UPDATE workflow_node_template
                SET display_name = ?
                WHERE authority_id = ? AND uuid = ?
                """,
                (sqlite3.Binary(b"\xff"), LOCAL_AUTHORITY.authority_id, node_uuid),
            )

        with (
            pytest.raises(TemplateCatalogMismatch) as caught,
            catalog.snapshot(LOCAL_AUTHORITY),
        ):
            pass

    assert caught.value.code == "template_catalog_mismatch"
    assert caught.value.path.startswith("/node_templates")
    assert str(caught.value) == "template_catalog_mismatch"
    assert "sqlite" not in caught.value.path.lower()
    assert "bytes" not in caught.value.path.lower()
    assert "/home/" not in caught.value.path
