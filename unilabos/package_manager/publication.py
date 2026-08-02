"""Catalog-derived backend publication envelope and injected transport port."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from .distribution import BuildArtifact


class PublicationPort(Protocol):
    def upload_artifact(self, path: str) -> tuple[str, str]: ...

    def publish_resources(
        self,
        resources: list[dict[str, Any]],
        package_info: dict[str, Any],
    ) -> Any: ...


class HttpClientPublicationAdapter:
    """Adapt the existing generic HTTP client without importing app globals."""

    def __init__(self, http_client: Any) -> None:
        self._http_client = http_client

    def upload_artifact(self, path: str) -> tuple[str, str]:
        return self._http_client.upload_file_to_oss(path, scene="models")

    def publish_resources(
        self,
        resources: list[dict[str, Any]],
        package_info: dict[str, Any],
    ) -> Any:
        return self._http_client.upload_package_resources(resources, package_info)


def publish_build(
    artifact: BuildArtifact,
    port: PublicationPort,
    *,
    download_url: str = "",
) -> dict[str, Any]:
    """Publish one audited wheel; never rescan its workspace or Registry."""

    object_key = ""
    if not download_url:
        download_url, object_key = port.upload_artifact(str(artifact.wheel))
    if not download_url and not object_key:
        raise RuntimeError("artifact upload 未返回 download_url/object_key")
    package_info = package_info_from_build(
        artifact,
        download_url=download_url,
        object_key=object_key,
    )
    resources = resource_payloads_from_build(artifact, package_info)
    response = port.publish_resources(resources, package_info)
    status = getattr(response, "status_code", None)
    if status not in (None, 200, 201):
        raise RuntimeError(
            f"发布 ResourceTemplate 失败: {status} {getattr(response, 'text', '')}"
        )
    return {
        "artifact": str(artifact.wheel),
        "download_url": download_url,
        "package_info": package_info,
        "resources": resources,
        "response_status": status,
    }


def package_info_from_build(
    artifact: BuildArtifact,
    *,
    download_url: str,
    object_key: str = "",
) -> dict[str, Any]:
    catalog = artifact.catalog
    result: dict[str, Any] = {
        "name": catalog.distribution.name,
        "version": catalog.distribution.version,
        "normalized_name": catalog.distribution.normalized_name,
        "class_namespace": catalog.namespace,
        "module_prefix": "community",
        "source_type": "community",
        "install_spec": (
            f"{catalog.distribution.name}=={catalog.distribution.version}"
        ),
        "dependencies": list(catalog.distribution.dependencies),
        "content_digest": catalog.content_digest,
        "catalog_digest": catalog.catalog_digest,
        "artifact_digest": artifact.artifact_digest,
        # Compatibility field: it is transport identity, never content identity.
        "sha256": artifact.artifact_digest,
        "download_url": download_url,
    }
    if object_key:
        result["oss_object_key"] = object_key
    return result


def resource_payloads_from_build(
    artifact: BuildArtifact,
    package_info: dict[str, Any],
) -> list[dict[str, Any]]:
    catalog = artifact.catalog
    records = (*catalog.definitions.devices, *catalog.definitions.resources)
    return [
        {
            "id": record.fqid,
            "registry_type": record.kind,
            "version": record.version,
            "displayname": record.displayname,
            "description": record.description,
            "category": list(record.category),
            "manufacturer": record.manufacturer,
            "model": _plain(record.details.get("model")),
            "package_info": package_info,
            "source_registry": _source_registry(record),
            "source_fqid": record.fqid,
            "content_hash": record.content_hash,
        }
        for record in records
    ]


def _source_registry(record: Any) -> dict[str, Any]:
    details = _plain(record.details)
    source: dict[str, Any] = {
        "class": {
            "module": f"{record.module}:{record.symbol}",
            "type": str(details.get("device_type") or "python"),
            "action_value_mappings": {},
            "status_types": {},
        },
        "category": list(record.category),
        "description": record.description,
        "displayname": record.displayname,
        "model": details.get("model"),
        "source_fqid": record.fqid,
        "content_hash": record.content_hash,
    }
    if record.kind == "device":
        source["class"]["action_value_mappings"] = {
            str(action["name"]): _action_mapping(action)
            for action in details.get("actions", [])
        }
        source["class"]["status_types"] = {
            str(status["name"]): str(status.get("return_type") or "Any")
            for status in details.get("status_properties", [])
        }
        source["handles"] = details.get("handles", [])
    else:
        source["class"]["type"] = str(details.get("class_type") or "python")
        source["handles"] = details.get("handles", [])
    return source


def _action_mapping(action: Mapping[str, Any]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    defaults: dict[str, Any] = {}
    for parameter in action.get("parameters", []):
        name = str(parameter["name"])
        properties[name] = {
            "title": name,
            "type": _json_type(str(parameter.get("type") or "Any")),
        }
        if parameter.get("required"):
            required.append(name)
        if "default" in parameter:
            defaults[name] = _plain(parameter["default"])
    goal: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        goal["required"] = required
    result: dict[str, Any] = {
        "type": "UniLabJsonCommand",
        "goal": goal,
        "result": {"type": "object", "properties": {}},
        "feedback": {"type": "object", "properties": {}},
        "description": str(action.get("docstring") or ""),
    }
    if defaults:
        result["goal_default"] = defaults
    return result


def _json_type(annotation: str) -> str:
    return {
        "bool": "boolean",
        "dict": "object",
        "float": "number",
        "int": "integer",
        "list": "array",
        "str": "string",
    }.get(annotation.split("[")[0].split(".")[-1], "object")


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


__all__ = [
    "HttpClientPublicationAdapter",
    "PublicationPort",
    "package_info_from_build",
    "publish_build",
    "resource_payloads_from_build",
]
