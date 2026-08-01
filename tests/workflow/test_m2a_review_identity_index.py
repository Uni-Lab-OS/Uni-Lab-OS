"""M2A review：production composition 显式双向 ResourceTemplate identity。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from unilabos.registry.catalog_consumer import RegistryTemplateProjectionError
from unilabos.workflow.catalog import TemplateCatalog
from unilabos.workflow.composition import (
    compose_workflow_runtime,
    get_workflow_service,
    reset_workflow_service_for_test,
)
from unilabos.workflow.store import WorkflowStore

from .test_m2a_material_source_production_composition import (
    AUTHORITY as PRODUCTION_AUTHORITY,
)
from .test_m2a_material_source_production_composition import (
    PLATE_SOURCE_IDENTITY,
    _registry_snapshot,
    _resource_registry_snapshot,
    _seed_material_authority,
)
from .test_m2a_material_source_production_composition import (
    _compile as _production_compile,
)
from .test_m2a_material_source_production_composition import (
    _create_workflow as _create_production_workflow,
)


class _ExplicitResourceTemplateIdentityIndex:
    """不可调用的显式双向 identity 系统边界。"""

    def __init__(self, by_source: Mapping[str, str]) -> None:
        self._by_source = dict(by_source)
        self._by_uuid = {value: key for key, value in self._by_source.items()}
        self.resolve_calls: list[str] = []
        self.identify_calls: list[str] = []

    def resolve_symbol(self, qualified_name: str) -> str:
        self.resolve_calls.append(qualified_name)
        return self._by_source[qualified_name]

    def identify_uuid(self, resource_template_uuid: str) -> str:
        self.identify_calls.append(resource_template_uuid)
        return self._by_uuid[resource_template_uuid]


def _catalog_state(working_dir: Path) -> dict[str, object]:
    reader = WorkflowStore(working_dir / "workflow.db")
    try:
        with TemplateCatalog(reader).snapshot(PRODUCTION_AUTHORITY) as snapshot:
            return {
                "fingerprint": snapshot.fingerprint,
                "node_templates": snapshot.node_templates,
                "handle_templates": snapshot.handle_templates,
            }
    finally:
        reader.close()


def test_production_composition_uses_explicit_bidirectional_identity_index(
    tmp_path: Path,
) -> None:
    working_dir = tmp_path / "unilabos_data"
    identities = _seed_material_authority(working_dir)
    index = _ExplicitResourceTemplateIdentityIndex(identities.by_source)
    reset_workflow_service_for_test()
    try:
        service = compose_workflow_runtime(
            working_dir,
            authority=PRODUCTION_AUTHORITY,
            registry_snapshot=_registry_snapshot(),
            resource_registry_snapshot=_resource_registry_snapshot(),
            resource_template_identity_resolver=index,  # type: ignore[arg-type]
        )
        applied = _create_production_workflow(service)
        compiled = _production_compile(service, applied)
        assert compiled.valid, compiled.diagnostics
        assert compiled.graph is not None
        assert service.compiler is not None
        generated = service.compiler.generate_python(
            workflow_uuid=applied["workflow"]["uuid"],
            workflow_revision=1,
            graph=compiled.graph,
            source_uri="package://lab/workflows/production_material_source.py",
        )
        assert generated.valid, generated.diagnostics
        assert generated.normalized_python_source is not None
        recompiled = service.compiler.compile(
            workflow_uuid=applied["workflow"]["uuid"],
            workflow_revision=1,
            python_source=generated.normalized_python_source,
            source_uri="package://lab/workflows/production_material_source.py",
            applied_graph=compiled.graph,
        )

        assert recompiled.valid, recompiled.diagnostics
        assert recompiled.graph == compiled.graph
        assert PLATE_SOURCE_IDENTITY in index.resolve_calls
        assert identities.plate_uuid in index.identify_calls
    finally:
        reset_workflow_service_for_test()


def test_host_material_source_rejects_legacy_one_way_identity_before_ready(
    tmp_path: Path,
) -> None:
    working_dir = tmp_path / "unilabos_data"
    identities = _seed_material_authority(working_dir)
    catalog_before = _catalog_state(working_dir)
    reset_workflow_service_for_test()
    try:
        with pytest.raises(RegistryTemplateProjectionError) as caught:
            compose_workflow_runtime(
                working_dir,
                authority=PRODUCTION_AUTHORITY,
                registry_snapshot=_registry_snapshot(),
                resource_registry_snapshot=_resource_registry_snapshot(),
                resource_template_identity_resolver=(
                    lambda source_identity: identities.by_source[source_identity]
                ),
            )

        assert caught.value.code == "template_catalog_mismatch"
        assert get_workflow_service() is None
        assert _catalog_state(working_dir) == catalog_before
    finally:
        reset_workflow_service_for_test()
