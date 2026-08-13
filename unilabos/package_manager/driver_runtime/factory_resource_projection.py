"""Project opt-in PyLabRobot factory children before Inventory bootstrap.

The physical graph owns device instances.  A factory may additionally own the
device's default PyLabRobot children (racks, holders, wells, and so on).  This
module constructs such a factory exactly once, projects its complete Resource
tree into the startup graph, and keeps the instance for the later ROS wrapper.
"""

from __future__ import annotations

import threading
import uuid
import inspect
from collections.abc import Callable, Mapping
from typing import Any

from unilabos.package_manager.driver_runtime.python_activation import (
    activate_python_driver,
)
from unilabos.resources.resource_tracker import ResourceTreeSet
from unilabos.utils.import_manager import default_manager


INFER_RESOURCE_TREE_METADATA_KEY = "infer_resource_tree"
_RESOURCE_CLASS_EXTRA_KEY = "unilabos_resource_class"
_prepared_lock = threading.RLock()
_prepared_instances: dict[tuple[str, str], object] = {}


class FactoryResourceProjectionError(RuntimeError):
    """An opted-in factory cannot safely become the startup Resource tree."""


def project_factory_resource_trees(
    registry: Any,
    resource_tree_set: ResourceTreeSet,
    *,
    loader: Callable[[str], Any] = default_manager.get_class,
) -> int:
    """Expand default children from opted-in PyLabRobot device factories.

    Args:
        registry: Live registry generation used by normal driver activation.
        resource_tree_set: Parsed physical graph. It is enriched in place.
        loader: Validated ``module:symbol`` loader, injectable for tests.

    Returns:
        Number of device roots enriched from a factory.

    Raises:
        FactoryResourceProjectionError: The opt-in declaration, graph, factory
            result, or PyLabRobot tree is invalid. No silent fallback is used.
    """

    projected = 0
    # Device instances may be roots or children of a lab/workstation node.
    # Freeze the traversal before attaching inferred descendants so newly
    # projected racks and holders are not revisited in this generation.
    for device_node in tuple(resource_tree_set.all_nodes):
        content = device_node.res_content
        if content.type != "device" or not isinstance(content.klass, str):
            continue
        entry = registry.resolve_definition("device", content.klass)
        metadata = entry.get("metadata", {}) if isinstance(entry, Mapping) else {}
        if not isinstance(metadata, Mapping) or not metadata.get(
            INFER_RESOURCE_TREE_METADATA_KEY, False
        ):
            continue
        if device_node.children:
            raise FactoryResourceProjectionError(
                f"factory_resource_tree_explicit_children: {content.id}"
            )

        activation = activate_python_driver(
            registry,
            content.klass,
            content.config,
            loader=loader,
        )
        if activation.driver_factory is None:
            raise FactoryResourceProjectionError(
                f"factory_resource_tree_factory_missing: {content.id}"
            )
        if activation.driver_type != "pylabrobot":
            raise FactoryResourceProjectionError(
                f"factory_resource_tree_driver_type_invalid: {content.id}"
            )

        try:
            instance = activation.driver_factory(**activation.driver_params)
        except Exception as error:
            raise FactoryResourceProjectionError(
                f"factory_resource_tree_construction_failed: {content.id}"
            ) from error
        if not isinstance(instance, activation.driver_class):
            _dispose(instance)
            raise FactoryResourceProjectionError(
                f"factory_resource_tree_return_type_mismatch: {content.id}"
            )
        try:
            from pylabrobot.resources import Resource

            if not isinstance(instance, Resource):
                raise FactoryResourceProjectionError(
                    f"factory_resource_tree_root_invalid: {content.id}"
                )
            _assign_stable_tree_identity(
                instance,
                root_uuid=content.uuid,
                root_class=activation.definition_identity,
            )
            inferred = ResourceTreeSet.from_plr_resources(
                [instance],
                known_newly_created=True,
            ).root_nodes[0]
        except FactoryResourceProjectionError:
            _dispose(instance)
            raise
        except Exception as error:
            _dispose(instance)
            raise FactoryResourceProjectionError(
                f"factory_resource_tree_serialization_failed: {content.id}"
            ) from error

        # The authored root keeps its stable graph identity, model, placement,
        # and constructor config. Only factory-owned descendants are inferred.
        for child in inferred.children:
            child.res_content.parent = content
            child.res_content.parent_uuid = content.uuid
        device_node.children = inferred.children
        _store_prepared_instance(
            content.uuid,
            activation.definition_identity,
            instance,
        )
        projected += 1
    return projected


def take_prepared_factory_instance(
    runtime_uuid: str,
    definition_identity: str,
    driver_class: type[Any],
) -> object | None:
    """Take the one startup-prepared instance for normal device initialization."""

    key = (runtime_uuid, definition_identity)
    with _prepared_lock:
        instance = _prepared_instances.pop(key, None)
    if instance is not None and not isinstance(instance, driver_class):
        _dispose(instance)
        raise FactoryResourceProjectionError(
            f"prepared_factory_return_type_mismatch: {definition_identity}"
        )
    return instance


def _store_prepared_instance(
    runtime_uuid: str,
    definition_identity: str,
    instance: object,
) -> None:
    key = (runtime_uuid, definition_identity)
    with _prepared_lock:
        previous = _prepared_instances.get(key)
        if previous is not None:
            _dispose(instance)
            raise FactoryResourceProjectionError(
                f"prepared_factory_instance_duplicate: {definition_identity}"
            )
        _prepared_instances[key] = instance


def _assign_stable_tree_identity(
    root: Any,
    *,
    root_uuid: str,
    root_class: str,
) -> None:
    """Assign repeatable UUID5 identities without changing factory topology."""

    root.unilabos_uuid = root_uuid
    root_extra = dict(getattr(root, "unilabos_extra", {}) or {})
    root_extra[_RESOURCE_CLASS_EXTRA_KEY] = root_class
    root.unilabos_extra = root_extra

    def visit(parent: Any, path: tuple[str, ...]) -> None:
        for index, child in enumerate(parent.children):
            segment = f"{index}:{child.name}"
            child_path = (*path, segment)
            child.unilabos_uuid = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    "unilabos:factory-resource-tree:"
                    + root_uuid
                    + ":"
                    + "/".join(child_path),
                )
            )
            visit(child, child_path)

    visit(root, ())


def _dispose(instance: object) -> None:
    for name in ("close", "shutdown", "stop"):
        method = getattr(instance, name, None)
        if callable(method) and not inspect.iscoroutinefunction(method):
            try:
                method()
            except Exception:
                return
            return


__all__ = [
    "FactoryResourceProjectionError",
    "INFER_RESOURCE_TREE_METADATA_KEY",
    "project_factory_resource_trees",
    "take_prepared_factory_instance",
]
