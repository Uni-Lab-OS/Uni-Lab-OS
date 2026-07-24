"""Generic declarative workstation Profile loading and source importing."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
import re
from typing import Any, Mapping

import yaml
from pydantic import ValidationError

from unilabos.registry.action_catalog import scan_decorated_device_package

from unilabos.workflow.canonical import (
    ActionInvocation,
    ControlEdge,
    ResourceHold,
    SourceMap,
    SourceMapEntry,
    WorkflowRevision,
    WorkflowSourceArtifact,
)


class ProfileValidationError(ValueError):
    """A Profile contains an unresolved or malformed cross-reference."""


_PROFILE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_PROFILE_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "profile_id",
        "description",
        "device_spec",
        "default_device_binding",
        "resource_topology",
        "driver_config",
        "recipe_import_mapping",
        "recipe_import_resource_holds",
        "workflow_importers",
    }
)


@dataclass(frozen=True)
class LoadedProfile:
    profile_id: str
    action_catalog: dict[str, dict[str, Any]]
    driver_binding: dict[str, str]
    driver_config: dict[str, Any]
    resources: dict[str, dict[str, Any]]
    legacy_stage_map: dict[str, str]
    legacy_resource_holds: tuple[dict[str, Any], ...] = field(
        default_factory=tuple
    )
    workflow_importers: tuple[dict[str, str], ...] = field(default_factory=tuple)

    def import_workflow_source(
        self,
        payload: Mapping[str, Any],
        *,
        parameters: Mapping[str, Any] | None = None,
        resolver: Any = None,
        source_artifact: WorkflowSourceArtifact | Mapping[str, Any] | None = None,
    ) -> WorkflowRevision:
        """Import a Profile-declared versioned workflow source."""

        schema = str(payload.get("schema") or "")
        kind = str(payload.get("kind") or "")
        registration = next(
            (
                item
                for item in self.workflow_importers
                if item["schema"] == schema and item["kind"] == kind
            ),
            None,
        )
        if registration is None:
            raise ProfileValidationError(
                f"profile has no workflow importer for {schema or '-'} / {kind or '-'}"
            )
        codec = registration["codec"]
        if codec == "structured_operation_tree_v1":
            from unilabos.workflow.operation_tree import (
                OperationTreeCompileError,
                compile_operation_tree,
            )

            try:
                revision = compile_operation_tree(
                    payload,
                    parameters=parameters,
                    resolver=resolver,
                )
            except OperationTreeCompileError as exc:
                raise ProfileValidationError(str(exc)) from exc
            if source_artifact is None:
                return revision
            try:
                artifact = WorkflowSourceArtifact.model_validate(source_artifact)
            except ValidationError as exc:
                raise ProfileValidationError(
                    f"workflow source artifact is invalid: {exc}"
                ) from exc
            return revision.model_copy(update={"source_artifact": artifact})
        if codec == "python_ast_v1":
            if source_artifact is None:
                raise ProfileValidationError(
                    "python workflow importer requires a source artifact"
                )
            try:
                artifact = WorkflowSourceArtifact.model_validate(source_artifact)
            except ValidationError as exc:
                raise ProfileValidationError(
                    f"workflow source artifact is invalid: {exc}"
                ) from exc
            source = payload.get("source")
            if not isinstance(source, str) or not source.strip():
                raise ProfileValidationError(
                    "python workflow payload requires non-empty source"
                )
            if artifact.format != "python" or artifact.text != source:
                raise ProfileValidationError(
                    "python workflow source must match its Python source artifact"
                )
            from unilabos.workflow.from_python_script import (
                PythonWorkflowCompileError,
                compile_python_script,
            )

            try:
                return compile_python_script(
                    source,
                    action_catalog=self.action_catalog,
                    source_artifact=artifact,
                )
            except PythonWorkflowCompileError as exc:
                raise ProfileValidationError(str(exc)) from exc
        raise ProfileValidationError(f"workflow importer codec is not installed: {codec}")

    def import_legacy_source(
        self,
        payload: Mapping[str, Any],
        *,
        parameters: Mapping[str, Any] | None = None,
    ) -> WorkflowRevision:
        """Import a mapped stage list without knowing any device family."""

        stages = payload.get("stages")
        if not isinstance(stages, list) or not stages:
            raise ProfileValidationError("legacy source requires non-empty stages")
        invocations: list[ActionInvocation] = []
        edges: list[ControlEdge] = []
        source_entries: list[SourceMapEntry] = []
        stage_nodes: dict[str, list[str]] = {}
        run_parameters = dict(parameters or {})
        for source_index, stage in enumerate(stages):
            if not isinstance(stage, Mapping):
                raise ProfileValidationError("legacy stage must be an object")
            if not bool(stage.get("enabled", True)):
                continue
            stage_name = str(stage.get("name") or "")
            action_ref = self.legacy_stage_map.get(stage_name)
            if action_ref is None:
                raise ProfileValidationError(
                    f"legacy stage has no action mapping: {stage_name or '-'}"
                )
            node_id = f"{stage_name}-{source_index + 1}"
            stage_nodes.setdefault(stage_name, []).append(node_id)
            values = dict(stage.get("params") or {})
            bindings = {
                name: {"kind": "literal", "value": value}
                for name, value in values.items()
            }
            action_inputs = self.action_catalog.get(action_ref, {}).get("inputs", {})
            accepted_inputs = (
                set(action_inputs) if isinstance(action_inputs, Mapping) else set()
            )
            for name in set(run_parameters) & accepted_inputs:
                bindings.setdefault(
                    name,
                    {"kind": "runtime_parameter", "parameter": name},
                )
            invocations.append(
                ActionInvocation(
                    node_id=node_id,
                    action_ref=action_ref,
                    input_bindings=bindings,
                )
            )
            source_entries.append(
                SourceMapEntry(
                    node_id=node_id,
                    source_step_index=source_index,
                    compiled_node_ids=[node_id],
                )
            )
            if len(invocations) > 1:
                edges.append(
                    ControlEdge(
                        source=invocations[-2].node_id,
                        target=node_id,
                    )
                )
        if not invocations:
            raise ProfileValidationError("legacy source has no enabled stages")
        workflow_id = str(payload.get("name") or self.profile_id)
        resource_holds: list[ResourceHold] = []
        for raw_hold in self.legacy_resource_holds:
            acquire_stage = str(raw_hold["acquire"].get("stage") or "")
            release_stage = str(raw_hold["release"].get("stage") or "")
            has_acquire = bool(stage_nodes.get(acquire_stage))
            has_release = bool(stage_nodes.get(release_stage))
            # A conditional workflow may keep a later stage while disabling
            # the stage that would acquire this hold.  In that case the hold
            # never exists and its release boundary is irrelevant.  The
            # inverse is unsafe: an acquired hold without a release must fail
            # closed instead of leaking into the run.
            if not has_acquire:
                continue
            if not has_release:
                raise ProfileValidationError(
                    f"resource hold {raw_hold['hold_id']} has no enabled release boundary"
                )
            acquire = self._resolve_stage_occurrence(
                stage_nodes,
                raw_hold["acquire"],
                hold_id=str(raw_hold["hold_id"]),
            )
            release = self._resolve_stage_occurrence(
                stage_nodes,
                raw_hold["release"],
                hold_id=str(raw_hold["hold_id"]),
            )
            resource_holds.append(
                ResourceHold(
                    hold_id=str(raw_hold["hold_id"]),
                    resource_ref=str(raw_hold["resource_ref"]),
                    scope=str(raw_hold["scope"]),
                    acquire_node_id=acquire,
                    release_node_id=release,
                )
            )
        return WorkflowRevision(
            revision_id=f"import-{self.profile_id}-{workflow_id}",
            workflow_id=workflow_id,
            invocations=invocations,
            control_edges=edges,
            resource_holds=resource_holds,
            source_map=SourceMap(entries=source_entries),
        )

    @staticmethod
    def _resolve_stage_occurrence(
        stage_nodes: Mapping[str, list[str]],
        selector: Mapping[str, str],
        *,
        hold_id: str,
    ) -> str:
        stage = str(selector.get("stage") or "")
        occurrence = str(selector.get("occurrence") or "")
        matches = stage_nodes.get(stage, [])
        if not matches:
            raise ProfileValidationError(
                f"resource hold {hold_id} stage is absent: {stage or '-'}"
            )
        return matches[0] if occurrence == "first" else matches[-1]


class ProfileLoader:
    """Load YAML into registries only after all references pass preflight."""

    def __init__(self, *, driver_catalog: Mapping[str, Any]) -> None:
        self._driver_catalog = driver_catalog

    def load(self, path: str | Path) -> LoadedProfile:
        profile_path = Path(path)
        profile = self._load_yaml(profile_path)
        unknown_fields = sorted(set(profile) - _PROFILE_TOP_LEVEL_FIELDS)
        if unknown_fields:
            raise ProfileValidationError(
                f"unknown ProfileV1 field: {unknown_fields[0]}"
            )
        schema_version = profile.get("schema_version")
        if isinstance(schema_version, bool) or schema_version != 1:
            raise ProfileValidationError("ProfileV1 schema_version must be integer 1")
        raw_profile_id = profile.get("profile_id")
        if not isinstance(raw_profile_id, str):
            raise ProfileValidationError("profile_id must be a string")
        profile_id = raw_profile_id
        if not _PROFILE_ID_PATTERN.fullmatch(profile_id):
            raise ProfileValidationError(
                "profile_id must contain only letters, numbers, dot, dash, or underscore"
            )
        if "description" in profile and not isinstance(profile["description"], str):
            raise ProfileValidationError("description must be a string")

        binding = profile.get("default_device_binding")
        if not isinstance(binding, Mapping):
            raise ProfileValidationError("default device binding is required")
        unknown_binding = sorted(
            set(binding) - {"device_id", "driver_key", "connection_ref"}
        )
        if unknown_binding:
            raise ProfileValidationError(
                f"unknown device binding field: {unknown_binding[0]}"
            )
        driver_binding: dict[str, str] = {}
        for name in ("device_id", "driver_key", "connection_ref"):
            value = binding.get(name)
            if not isinstance(value, str):
                raise ProfileValidationError(f"device binding {name} must be a string")
            driver_binding[name] = value
        missing_binding = [
            name for name, value in driver_binding.items() if not value
        ]
        if missing_binding:
            raise ProfileValidationError(
                f"device binding requires {missing_binding[0]}"
            )
        if driver_binding["driver_key"] not in self._driver_catalog:
            raise ProfileValidationError(
                f"driver key is not registered: {driver_binding['driver_key']}"
            )

        spec_ref = profile.get("device_spec")
        if not isinstance(spec_ref, str) or not spec_ref:
            raise ProfileValidationError("device_spec is required")
        spec = self._load_yaml((profile_path.parent / spec_ref).resolve())
        if spec.get("schema_version") != 2:
            raise ProfileValidationError("device spec schema_version must be integer 2")
        device = spec.get("device")
        if (
            not isinstance(device, Mapping)
            or not isinstance(device.get("id"), str)
            or not device.get("id")
        ):
            raise ProfileValidationError("device spec is missing device.id")
        device_id = device["id"]
        if device_id != driver_binding["device_id"]:
            raise ProfileValidationError("device binding does not match device spec")

        resources = self._load_resources(profile.get("resource_topology"))
        action_catalog = self._load_actions(
            device_id=device_id,
            raw_actions=spec.get("actions"),
            resources=resources,
        )
        # A self-contained Profile package may expose additional physical
        # namespaces as a sibling Python module named after the profile. Their
        # @device/@action declarations are the registry-owned source of truth.
        decorated_root = profile_path.parent / profile_id
        if decorated_root.is_dir():
            decorated_actions = scan_decorated_device_package(decorated_root)
            duplicates = sorted(set(action_catalog) & set(decorated_actions))
            if duplicates:
                raise ProfileValidationError(
                    f"decorated action duplicates device spec: {duplicates[0]}"
                )
            action_catalog.update(decorated_actions)
        raw_driver_config = profile.get("driver_config")
        if not isinstance(raw_driver_config, Mapping):
            raise ProfileValidationError("driver_config is required")
        macros = raw_driver_config.get("macros")
        if not isinstance(macros, Mapping):
            raise ProfileValidationError("driver_config.macros is required")
        self._validate_macros(macros)
        raw_actions = spec.get("actions")
        assert isinstance(raw_actions, list)
        action_ids = {
            str(action.get("id") or "")
            for action in raw_actions
            if isinstance(action, Mapping)
        }
        macro_action_ids = {
            str(action.get("id") or "")
            for action in raw_actions
            if isinstance(action, Mapping)
            and str(action.get("execution_kind") or "") == "device_macro"
        }
        missing_macros = sorted(macro_action_ids - set(macros))
        if missing_macros:
            raise ProfileValidationError(
                f"device_macro action has no driver macro: {missing_macros[0]}"
            )
        unknown_macros = sorted(set(macros) - action_ids)
        if unknown_macros:
            raise ProfileValidationError(
                f"driver macro has no action contract: {unknown_macros[0]}"
            )
        raw_stage_map = profile.get("recipe_import_mapping")
        if raw_stage_map is None:
            raw_stage_map = {}
        if not isinstance(raw_stage_map, Mapping):
            raise ProfileValidationError("recipe_import_mapping must be an object")
        stage_map = {
            str(name): str(action_ref)
            for name, action_ref in raw_stage_map.items()
        }
        for stage_name, action_ref in stage_map.items():
            if action_ref not in action_catalog:
                raise ProfileValidationError(
                    f"stage {stage_name} references unknown action {action_ref}"
                )
        resource_holds = self._load_recipe_resource_holds(
            profile.get("recipe_import_resource_holds"),
            resources=resources,
            stage_map=stage_map,
        )
        workflow_importers = self._load_workflow_importers(
            profile.get("workflow_importers")
        )

        return LoadedProfile(
            profile_id=profile_id,
            action_catalog=action_catalog,
            driver_binding=driver_binding,
            driver_config=dict(raw_driver_config),
            resources=resources,
            legacy_stage_map=stage_map,
            legacy_resource_holds=tuple(resource_holds),
            workflow_importers=tuple(workflow_importers),
        )

    @staticmethod
    def _load_workflow_importers(raw_importers: Any) -> list[dict[str, str]]:
        if raw_importers is None:
            return []
        if not isinstance(raw_importers, list):
            raise ProfileValidationError("workflow_importers must be a list")
        importers: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for raw in raw_importers:
            if not isinstance(raw, Mapping):
                raise ProfileValidationError("workflow importer must be an object")
            unknown = sorted(set(raw) - {"schema", "kind", "codec"})
            if unknown:
                raise ProfileValidationError(
                    f"unknown workflow importer field: {unknown[0]}"
                )
            item = {
                name: str(raw.get(name) or "")
                for name in ("schema", "kind", "codec")
            }
            if not all(item.values()):
                raise ProfileValidationError(
                    "workflow importer requires schema, kind, and codec"
                )
            key = (item["schema"], item["kind"])
            if key in seen:
                raise ProfileValidationError(
                    f"duplicate workflow importer: {key[0]} / {key[1]}"
                )
            seen.add(key)
            importers.append(item)
        return importers

    @staticmethod
    def _load_yaml(path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise ProfileValidationError(f"profile file not found: {path}")
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ProfileValidationError(f"YAML root must be an object: {path}")
        return loaded

    @staticmethod
    def _load_resources(raw_topology: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(raw_topology, Mapping):
            raise ProfileValidationError("resource topology is required")
        unknown_topology = sorted(set(raw_topology) - {"resources"})
        if unknown_topology:
            raise ProfileValidationError(
                f"unknown resource topology field: {unknown_topology[0]}"
            )
        raw_resources = raw_topology.get("resources")
        if not isinstance(raw_resources, list):
            raise ProfileValidationError("resource topology requires resources list")
        resources: dict[str, dict[str, Any]] = {}
        for raw in raw_resources:
            if (
                not isinstance(raw, Mapping)
                or not isinstance(raw.get("id"), str)
                or not raw.get("id")
            ):
                raise ProfileValidationError("resource requires id")
            unknown_resource = sorted(
                set(raw) - {"id", "resource_type", "group_id"}
            )
            if unknown_resource:
                raise ProfileValidationError(
                    f"unknown resource field: {unknown_resource[0]}"
                )
            if not isinstance(raw.get("resource_type"), str) or not raw.get(
                "resource_type"
            ):
                raise ProfileValidationError("resource requires resource_type")
            if "group_id" in raw and not isinstance(raw["group_id"], str):
                raise ProfileValidationError("resource group_id must be a string")
            resource_id = raw["id"]
            if resource_id in resources:
                raise ProfileValidationError(f"duplicate resource: {resource_id}")
            resources[resource_id] = {
                str(key): value
                for key, value in raw.items()
                if key != "id"
            }
        return resources

    @staticmethod
    def _validate_macros(macros: Mapping[str, Any]) -> None:
        for macro_name, raw_steps in macros.items():
            if not isinstance(macro_name, str) or not macro_name:
                raise ProfileValidationError("driver macro name must not be empty")
            if not isinstance(raw_steps, list) or not raw_steps:
                raise ProfileValidationError(
                    f"driver macro {macro_name} requires non-empty steps"
                )
            for step in raw_steps:
                if not isinstance(step, Mapping):
                    raise ProfileValidationError(
                        f"driver macro {macro_name} step must be an object"
                    )
                unknown = sorted(set(step) - {"call", "args"})
                if unknown:
                    raise ProfileValidationError(
                        f"unknown macro step field: {unknown[0]}"
                    )
                if not isinstance(step.get("call"), str) or not step["call"]:
                    raise ProfileValidationError("macro step requires call")
                if "args" in step and not isinstance(step["args"], list):
                    raise ProfileValidationError("macro step args must be a list")

    @staticmethod
    def _load_recipe_resource_holds(
        raw_holds: Any,
        *,
        resources: Mapping[str, Mapping[str, Any]],
        stage_map: Mapping[str, str],
    ) -> list[dict[str, Any]]:
        if raw_holds is None:
            return []
        if not isinstance(raw_holds, list):
            raise ProfileValidationError(
                "recipe_import_resource_holds must be a list"
            )
        holds: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in raw_holds:
            if not isinstance(raw, Mapping):
                raise ProfileValidationError("recipe resource hold must be an object")
            expected = {
                "hold_id",
                "resource_ref",
                "scope",
                "acquire",
                "release",
            }
            unknown = sorted(set(raw) - expected)
            if unknown or set(raw) != expected:
                raise ProfileValidationError(
                    "recipe resource hold requires exactly hold_id, resource_ref, "
                    "scope, acquire, and release"
                )
            hold_id = str(raw.get("hold_id") or "")
            resource_ref = str(raw.get("resource_ref") or "")
            scope = str(raw.get("scope") or "")
            if not hold_id or hold_id in seen:
                raise ProfileValidationError(
                    f"duplicate or empty resource hold id: {hold_id or '-'}"
                )
            seen.add(hold_id)
            if resource_ref not in resources:
                raise ProfileValidationError(
                    f"resource hold {hold_id} references unknown resource {resource_ref}"
                )
            if scope not in {"until_handoff", "workflow_block"}:
                raise ProfileValidationError(
                    f"resource hold {hold_id} has unsupported scope {scope}"
                )
            selectors: dict[str, dict[str, str]] = {}
            for boundary in ("acquire", "release"):
                selector = raw.get(boundary)
                if not isinstance(selector, Mapping) or set(selector) != {
                    "stage",
                    "occurrence",
                }:
                    raise ProfileValidationError(
                        f"resource hold {hold_id} {boundary} selector is invalid"
                    )
                stage = str(selector.get("stage") or "")
                occurrence = str(selector.get("occurrence") or "")
                if stage not in stage_map:
                    raise ProfileValidationError(
                        f"resource hold {hold_id} references unmapped stage {stage}"
                    )
                if occurrence not in {"first", "last"}:
                    raise ProfileValidationError(
                        f"resource hold {hold_id} occurrence must be first or last"
                    )
                selectors[boundary] = {
                    "stage": stage,
                    "occurrence": occurrence,
                }
            holds.append(
                {
                    "hold_id": hold_id,
                    "resource_ref": resource_ref,
                    "scope": scope,
                    **selectors,
                }
            )
        return holds

    @staticmethod
    def _parameter_schema(items: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(items, list):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, Mapping) or not item.get("name"):
                raise ProfileValidationError("action parameter requires name")
            result[str(item["name"])] = {
                str(key): value
                for key, value in item.items()
                if key != "name"
            }
        return result

    def _load_actions(
        self,
        *,
        device_id: str,
        raw_actions: Any,
        resources: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        if not isinstance(raw_actions, list) or not raw_actions:
            raise ProfileValidationError("device spec requires actions")
        catalog: dict[str, dict[str, Any]] = {}
        for raw in raw_actions:
            if (
                not isinstance(raw, Mapping)
                or not isinstance(raw.get("id"), str)
                or not raw.get("id")
            ):
                raise ProfileValidationError("action requires id")
            action_ref = f"{device_id}.{raw['id']}"
            claims = list(raw.get("resource_claims") or [])
            effects = list(raw.get("effects") or [])
            for item in [*claims, *effects]:
                if not isinstance(item, Mapping):
                    raise ProfileValidationError(
                        f"action {action_ref} resource entry must be an object"
                    )
                resource_ref = item.get("resource_ref")
                if resource_ref is not None and str(resource_ref) not in resources:
                    raise ProfileValidationError(
                        f"action {action_ref} references unknown resource {resource_ref}"
                    )
            catalog[action_ref] = {
                "inputs": self._parameter_schema(raw.get("params")),
                "outputs": self._parameter_schema(raw.get("results")),
                "execution_kind": raw.get("execution_kind", "atomic"),
                "material": dict(raw.get("material") or {}),
                "resource_claims": claims,
                "effects": effects,
                "timing": dict(raw.get("timing") or {}),
                "recovery": dict(raw.get("recovery") or {}),
            }
        return catalog


def discover_driver_catalog() -> dict[str, Any]:
    """Return built-in generic drivers plus installed entry-point plugins."""

    from unilabos.devices.generic_plc_macro import DeclarativePLCMacroDriver

    drivers: dict[str, Any] = {
        "generic_plc_macro": DeclarativePLCMacroDriver,
    }
    entry_points = metadata.entry_points()
    selected = (
        entry_points.select(group="unilabos.drivers")
        if hasattr(entry_points, "select")
        else entry_points.get("unilabos.drivers", [])
    )
    for entry_point in selected:
        drivers[entry_point.name] = entry_point.load()
    return drivers


def load_profiles(
    paths: list[str | Path],
    *,
    driver_catalog: Mapping[str, Any] | None = None,
) -> dict[str, LoadedProfile]:
    loader = ProfileLoader(
        driver_catalog=driver_catalog or discover_driver_catalog()
    )
    loaded: dict[str, LoadedProfile] = {}
    for path in paths:
        profile = loader.load(path)
        if profile.profile_id in loaded:
            raise ProfileValidationError(
                f"duplicate profile_id: {profile.profile_id}"
            )
        loaded[profile.profile_id] = profile
    return loaded
