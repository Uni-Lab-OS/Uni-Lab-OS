"""资源模板（ResourceTemplate）源码身份的本地模板投影编码。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from unilabos.workflow.models import validate_uuid
from unilabos.workflow.source_identity import (
    PythonSourceIdentityError,
    canonical_python_source_identity,
)

_PROJECTION_KEY = "resource_template_identity_projection"
_PROJECTION_VERSION = 1


class ResourceTemplateIdentityProjectionError(ValueError):
    """资源模板源码身份投影缺失、重复或被持久数据污染。"""


def embed_resource_template_identities(
    node_templates: Sequence[Mapping[str, Any]],
    identities: Mapping[str, str],
) -> list[dict[str, Any]]:
    """把同代资源模板身份写入唯一物料来源框架模板元数据。

    参数说明：``node_templates`` 是尚未持久化的完整节点模板代际，
    ``identities`` 把 ``source_fqid`` 映射到资源模板 UUID。返回：完全分离且
    带版本化 OS 私有投影的新节点列表；框架缺失或映射非法时抛出
    ``ResourceTemplateIdentityProjectionError``。
    """

    normalized = _normalize_identities(identities)
    nodes = [deepcopy(dict(node)) for node in node_templates]
    if not normalized:
        return nodes
    framework_nodes = [node for node in nodes if _is_material_source(node)]
    if len(framework_nodes) != 1:
        raise ResourceTemplateIdentityProjectionError(
            "资源模板身份必须依附唯一物料来源（MaterialSource）框架模板"
        )
    framework = framework_nodes[0]
    meta_data = _mapping_copy(framework.get("meta_data"), label="框架模板元数据")
    unilab = _mapping_copy(meta_data.get("unilab"), label="Uni-Lab 模板元数据")
    unilab[_PROJECTION_KEY] = {
        "version": _PROJECTION_VERSION,
        "source_fqid_to_uuid": normalized,
    }
    meta_data["unilab"] = unilab
    framework["meta_data"] = meta_data
    return nodes


def extract_resource_template_identities(
    node_templates: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    """从已持久化模板代际恢复资源模板源码身份。

    参数说明：``node_templates`` 是 SQLite 返回的当前活动节点模板。返回：规范
    且按 ``source_fqid`` 排序的新映射；历史代际未携带投影时返回空映射，重复、
    版本未知或结构非法时抛出 ``ResourceTemplateIdentityProjectionError``。
    """

    projections: list[Any] = []
    for node in node_templates:
        meta_data = node.get("meta_data")
        unilab = meta_data.get("unilab") if isinstance(meta_data, Mapping) else None
        if isinstance(unilab, Mapping) and _PROJECTION_KEY in unilab:
            projections.append(unilab[_PROJECTION_KEY])
    if not projections:
        return {}
    if len(projections) != 1:
        raise ResourceTemplateIdentityProjectionError(
            "活动模板代际包含重复资源模板身份投影"
        )
    projection = projections[0]
    if (
        not isinstance(projection, Mapping)
        or set(projection) != {"version", "source_fqid_to_uuid"}
        or projection.get("version") != _PROJECTION_VERSION
    ):
        raise ResourceTemplateIdentityProjectionError(
            "资源模板身份投影版本或结构非法"
        )
    return _normalize_identities(projection.get("source_fqid_to_uuid"))


def _normalize_identities(raw_identities: Any) -> dict[str, str]:
    """规范并验证双向一一对应的源码身份映射。

    参数说明：``raw_identities`` 是可疑持久值或新编译映射。返回：键排序的新
    字典；空身份、非法 UUID 或多个源码身份绑定同一 UUID 时抛出投影错误。
    """

    if not isinstance(raw_identities, Mapping):
        raise ResourceTemplateIdentityProjectionError("资源模板身份投影必须是对象")
    normalized: dict[str, str] = {}
    symbols_by_uuid: dict[str, str] = {}
    for raw_symbol, raw_uuid in sorted(
        raw_identities.items(),
        key=lambda item: str(item[0]),
    ):
        try:
            symbol = canonical_python_source_identity(raw_symbol)
        except PythonSourceIdentityError as error:
            raise ResourceTemplateIdentityProjectionError(
                "资源模板 source_fqid 不是可信 Python 源码身份"
            ) from error
        try:
            template_uuid = validate_uuid(raw_uuid)
        except (TypeError, ValueError):
            raise ResourceTemplateIdentityProjectionError(
                "资源模板源码身份映射到非法 UUID"
            ) from None
        previous_symbol = symbols_by_uuid.get(template_uuid)
        if previous_symbol is not None and previous_symbol != symbol:
            raise ResourceTemplateIdentityProjectionError(
                "资源模板 UUID 不得绑定多个 source_fqid"
            )
        normalized[symbol] = template_uuid
        symbols_by_uuid[template_uuid] = symbol
    return normalized


def _mapping_copy(raw_value: Any, *, label: str) -> dict[str, Any]:
    """分离复制一个可选元数据对象。

    参数说明：``raw_value`` 为 ``None`` 或映射，``label`` 用于中文诊断。返回：
    普通新字典；非映射值抛出 ``ResourceTemplateIdentityProjectionError``。
    """

    if raw_value is None:
        return {}
    if not isinstance(raw_value, Mapping):
        raise ResourceTemplateIdentityProjectionError(f"{label}必须是对象")
    return deepcopy(dict(raw_value))


def _is_material_source(node: Mapping[str, Any]) -> bool:
    """判断节点模板是否为物料来源框架。

    参数说明：``node`` 是节点模板候选。返回：类身份和动作名同时命中稳定框架
    合同时为 ``True``，否则为 ``False``。
    """

    return (
        node.get("class") == "unilabos.workflow.authoring:material_source"
        and node.get("name") == "material_source"
    )


__all__ = [
    "ResourceTemplateIdentityProjectionError",
    "embed_resource_template_identities",
    "extract_resource_template_identities",
]
