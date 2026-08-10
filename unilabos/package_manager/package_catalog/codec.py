"""规范包目录（PackageCatalog）的严格 JSON 解码器。"""

from __future__ import annotations

import json
from typing import Any

from .model import (
    PackageAsset,
    PackageCatalog,
    PackageDefinition,
    PackageDefinitionCatalog,
    PackageDistributionIdentity,
)


def catalog_from_canonical_bytes(payload: bytes) -> PackageCatalog:
    """严格解码、重建并复算一个规范包目录。

    参数：``payload`` 是 RFC 8785 规范 JSON 字节。
    返回：重新创建且目录摘要匹配的 ``PackageCatalog``。
    异常：重复键、未知字段、类型、规范编码或摘要无效时抛出
    ``TypeError``/``ValueError``。
    """

    if not isinstance(payload, bytes):
        raise TypeError("包目录规范文档必须是 bytes")
    try:
        document = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("包目录不是合法 UTF-8 JSON") from error
    root = _object(
        document,
        "catalog",
        {
            "assets",
            "catalog_digest",
            "content_digest",
            "definitions",
            "distribution",
            "import_package",
            "namespace",
            "schema_version",
        },
    )
    if root["schema_version"] != "1":
        raise ValueError("包目录 schema_version 不受支持")
    distribution_data = _object(
        root["distribution"],
        "distribution",
        {
            "dependencies",
            "description",
            "homepage",
            "license",
            "name",
            "normalized_name",
            "requires_python",
            "version",
        },
    )
    distribution = PackageDistributionIdentity(
        name=_text(distribution_data["name"], "distribution.name"),
        normalized_name=_text(
            distribution_data["normalized_name"], "distribution.normalized_name"
        ),
        version=_text(distribution_data["version"], "distribution.version"),
        description=_text(
            distribution_data["description"], "distribution.description", empty=True
        ),
        license=_text(distribution_data["license"], "distribution.license", empty=True),
        homepage=_text(
            distribution_data["homepage"], "distribution.homepage", empty=True
        ),
        requires_python=_text(
            distribution_data["requires_python"],
            "distribution.requires_python",
            empty=True,
        ),
        dependencies=tuple(
            _text(item, "distribution.dependencies[]")
            for item in _array(
                distribution_data["dependencies"], "distribution.dependencies"
            )
        ),
    )
    definitions_data = _object(
        root["definitions"],
        "definitions",
        {"devices", "resources", "workflows"},
    )
    definitions = PackageDefinitionCatalog(
        devices=_definitions(definitions_data["devices"], "device"),
        resources=_definitions(definitions_data["resources"], "resource"),
        workflows=_definitions(definitions_data["workflows"], "workflow"),
    )
    assets = tuple(
        _asset(item) for item in _array(root["assets"], "catalog.assets")
    )
    catalog = PackageCatalog.create(
        distribution=distribution,
        import_package=_text(root["import_package"], "catalog.import_package"),
        namespace=_text(root["namespace"], "catalog.namespace"),
        definitions=definitions,
        assets=assets,
        content_digest=_digest(root["content_digest"], "catalog.content_digest"),
    )
    if catalog.catalog_digest != _digest(
        root["catalog_digest"], "catalog.catalog_digest"
    ):
        raise ValueError("包目录 catalog_digest 复算不一致")
    if catalog.to_canonical_bytes() != payload:
        raise ValueError("包目录 JSON 不是 RFC 8785 规范编码")
    return catalog


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """把 JSON 对象成员转为字典并拒绝重复键。

    参数：``pairs`` 是 JSON 解码器提供的有序键值对。
    返回：没有重复成员的新字典。
    异常：发现重复键时抛出 ``ValueError``。
    """

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"包目录 JSON 包含重复键：{key}")
        result[key] = value
    return result


def _object(value: Any, label: str, keys: set[str]) -> dict[str, Any]:
    """验证一个 JSON 对象具有精确字段集合。

    参数：``value`` 是未知值；``label`` 是字段路径；``keys`` 是允许且必需字段。
    返回：原对象。
    异常：类型或字段集合不匹配时抛出 ``TypeError``/``ValueError``。
    """

    if not isinstance(value, dict):
        raise TypeError(f"{label} 必须是对象")
    if set(value) != keys:
        raise ValueError(f"{label} 字段集合无效")
    return value


def _array(value: Any, label: str) -> list[Any]:
    """验证一个 JSON 数组。

    参数：``value`` 是未知值；``label`` 是字段路径。
    返回：原列表。
    异常：不是列表时抛出 ``TypeError``。
    """

    if not isinstance(value, list):
        raise TypeError(f"{label} 必须是数组")
    return value


def _text(value: Any, label: str, *, empty: bool = False) -> str:
    """验证一个普通文本字段。

    参数：``value`` 是未知值；``label`` 是字段路径；``empty`` 是否允许空字符串。
    返回：原字符串。
    异常：类型或空值不合规时抛出 ``TypeError``/``ValueError``。
    """

    if not isinstance(value, str):
        raise TypeError(f"{label} 必须是字符串")
    if not empty and not value:
        raise ValueError(f"{label} 不能为空")
    return value


def _digest(value: Any, label: str) -> str:
    """验证带前缀 SHA-256 字段。

    参数：``value`` 是未知值；``label`` 是字段路径。
    返回：原摘要字符串。
    异常：格式无效时抛出 ``ValueError``。
    """

    text = _text(value, label)
    if len(text) != 71 or not text.startswith("sha256:"):
        raise ValueError(f"{label} 不是 sha256 摘要")
    try:
        int(text[7:], 16)
    except ValueError as error:
        raise ValueError(f"{label} 不是 sha256 摘要") from error
    return text


def _definitions(value: Any, kind: str) -> tuple[PackageDefinition, ...]:
    """解码一种规范定义集合。

    参数：``value`` 是定义数组；``kind`` 是预期定义种类。
    返回：不可变定义元组。
    异常：任一定义字段或种类无效时抛出 ``TypeError``/``ValueError``。
    """

    results: list[PackageDefinition] = []
    for raw in _array(value, f"definitions.{kind}"):
        item = _object(
            raw,
            f"definitions.{kind}[]",
            {
                "content_hash",
                "declaring_file",
                "description",
                "details",
                "fqid",
                "id",
                "kind",
                "module",
                "symbol",
                "title",
                "version",
            },
        )
        if item["kind"] != kind:
            raise ValueError("包目录定义种类不一致")
        if not isinstance(item["details"], dict):
            raise TypeError("包目录 definition.details 必须是对象")
        results.append(
            PackageDefinition(
                kind=kind,  # type: ignore[arg-type]
                id=_text(item["id"], "definition.id"),
                fqid=_text(item["fqid"], "definition.fqid"),
                module=_text(item["module"], "definition.module"),
                symbol=_text(item["symbol"], "definition.symbol"),
                declaring_file=_text(
                    item["declaring_file"], "definition.declaring_file"
                ),
                content_hash=_digest(
                    item["content_hash"], "definition.content_hash"
                ),
                version=_text(item["version"], "definition.version"),
                title=_text(item["title"], "definition.title", empty=True),
                description=_text(
                    item["description"], "definition.description", empty=True
                ),
                details=item["details"],
            )
        )
    return tuple(results)


def _asset(value: Any) -> PackageAsset:
    """解码一个静态资产记录。

    参数：``value`` 是未知 JSON 值。
    返回：验证后的 ``PackageAsset``。
    异常：字段或大小无效时抛出 ``TypeError``/``ValueError``。
    """

    item = _object(value, "catalog.assets[]", {"digest", "logical_path", "size"})
    size = item["size"]
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise TypeError("catalog.assets[].size 必须是非负整数")
    return PackageAsset(
        logical_path=_text(item["logical_path"], "asset.logical_path"),
        digest=_digest(item["digest"], "asset.digest"),
        size=size,
    )


__all__ = ["catalog_from_canonical_bytes"]
