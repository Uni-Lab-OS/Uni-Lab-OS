"""把工作区模型声明投影为前端渲染快照与受限公共资产目录。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any
from urllib.parse import quote

from .workspace_startup import WorkspaceStartupPlan

_MODEL_MEDIA_TYPES = {
    ".dae": "model/vnd.collada+xml",
    ".glb": "model/gltf-binary",
    ".gltf": "model/gltf+json",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".json": "application/json",
    ".obj": "model/obj",
    ".png": "image/png",
    ".stl": "model/stl",
    ".urdf": "application/xml",
    ".xacro": "application/xml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
}


@dataclass(frozen=True, slots=True)
class WorkspaceMaterialModelAsset:
    """一次公共模型资产读取结果。"""

    content: bytes
    media_type: str
    etag: str


@dataclass(frozen=True, slots=True)
class WorkspaceMaterialModelCatalog:
    """固定工作区来源、模型绑定和可读取模型目录的启动代际。"""

    startup_plan: WorkspaceStartupPlan
    models_by_template: Mapping[str, Mapping[str, Any]]
    allowed_model_roots: tuple[PurePosixPath, ...]

    def read_asset(self, public_path: str) -> WorkspaceMaterialModelAsset:
        """读取一项已授权模型目录内的公共资产。

        参数：``public_path`` 是模型快照发布的 `/api/v1/material-models/` 路径。
        返回：资产字节、媒体类型和内容摘要。异常：路径不属于当前发行包、越过任一
        声明模型目录或文件缺失时抛出 ``KeyError``；安全来源错误转为同一关闭失败。
        """

        # ``public_prefix`` 把 HTTP 路径重新绑定到本启动代际的唯一发行包身份。
        public_prefix = (
            "/api/v1/material-models/"
            + quote(self.startup_plan.distribution_name, safe="")
            + "/"
        )
        if not isinstance(public_path, str) or not public_path.startswith(
            public_prefix
        ):
            raise KeyError("模型资产未授权")
        logical_text = public_path[len(public_prefix) :]
        try:
            # ``logical_asset`` 是工作区根内的稳定文件身份，不接受 URL 查询或片段。
            logical_asset = PurePosixPath(logical_text)
            if (
                not logical_asset.parts
                or logical_asset.is_absolute()
                or any(part in {"", ".", ".."} for part in logical_asset.parts)
                or "?" in logical_text
                or "#" in logical_text
            ):
                raise ValueError("非法模型资产路径")
            if not any(
                logical_asset == root or logical_asset.is_relative_to(root)
                for root in self.allowed_model_roots
            ):
                raise KeyError("模型资产未授权")
            content = self.startup_plan.source.read_bytes(logical_asset.as_posix())
        except KeyError:
            raise
        except (OSError, RuntimeError, ValueError) as error:
            raise KeyError("模型资产未授权或不存在") from error
        media_type = _MODEL_MEDIA_TYPES.get(
            logical_asset.suffix.casefold(),
            "application/octet-stream",
        )
        return WorkspaceMaterialModelAsset(
            content=content,
            media_type=media_type,
            etag="sha256:" + hashlib.sha256(content).hexdigest(),
        )


def compile_workspace_material_models(
    startup_plan: WorkspaceStartupPlan,
    registry: Any,
) -> WorkspaceMaterialModelCatalog:
    """编译工作区装饰器声明的 3D 模型绑定与公共资产授权根。

    参数：``startup_plan`` 固定唯一工作区来源；``registry`` 提供同代设备和资源
    模板定义。返回：按资源模板身份索引的不可变模型快照及受限资产读取目录。
    异常：声明路径、模型格式、入口、重复身份或 JSON 字段无效时关闭式失败。
    """

    if not isinstance(startup_plan, WorkspaceStartupPlan):
        raise TypeError("startup_plan 必须是 WorkspaceStartupPlan")
    try:
        # ``definitions`` 只能来自注册表（Registry）已经完成的唯一静态扫描。
        definitions = (
            *registry.obtain_registry_device_info(),
            *registry.obtain_registry_resource_info(),
        )
    except AttributeError:
        raise TypeError("registry 必须提供设备和资源定义读取接口") from None

    models_by_template: dict[str, Mapping[str, Any]] = {}
    allowed_roots: set[PurePosixPath] = set()
    for raw_definition in definitions:
        if not isinstance(raw_definition, Mapping):
            raise TypeError("注册表定义必须是对象")
        declaration_file = _workspace_declaration_file(startup_plan, raw_definition)
        if declaration_file is None:
            continue
        model = raw_definition.get("model")
        if not isinstance(model, Mapping):
            continue
        entry = model.get("entry")
        model_format = model.get("format")
        if entry is None and model_format is None:
            continue
        if not isinstance(entry, str) or not entry:
            raise ValueError("工作区模型资产入口必须是非空 POSIX 相对路径")
        if not isinstance(model_format, str) or not model_format:
            raise ValueError("工作区模型格式必须是非空字符串")
        logical_entry = _logical_model_path(startup_plan, declaration_file, entry)
        if not startup_plan.source.has_file(logical_entry):
            raise ValueError("工作区模型入口不存在")
        template_id = _required_text(raw_definition.get("id"), "资源模板 id")
        public_entry = _public_model_path(startup_plan, logical_entry)
        projected_model: dict[str, Any] = {
            "path": public_entry,
            "format": model_format,
            "meshDir": public_entry.rsplit("/", 1)[0],
        }
        for key in (
            "macro",
            "color",
            "position",
            "rotation",
            "scale",
            "model_origin",
        ):
            if model.get(key) is not None:
                projected_model[key] = _json_value(model[key], f"model.{key}")
        if template_id in models_by_template:
            raise ValueError(f"工作区模型资源模板身份重复: {template_id}")
        models_by_template[template_id] = MappingProxyType(projected_model)
        # ``model_root`` 授权 Xacro 入口的同目录依赖，例如 meshes 与 YAML。
        allowed_roots.add(PurePosixPath(logical_entry).parent)

    return WorkspaceMaterialModelCatalog(
        startup_plan=startup_plan,
        models_by_template=MappingProxyType(dict(sorted(models_by_template.items()))),
        allowed_model_roots=tuple(
            sorted(allowed_roots, key=lambda path: path.as_posix())
        ),
    )


def _workspace_declaration_file(
    startup_plan: WorkspaceStartupPlan,
    definition: Mapping[str, Any],
) -> Path | None:
    """取得当前工作区内的装饰器声明文件。

    参数：启动计划与注册表定义。返回：包内绝对路径，外部定义返回 ``None``。
    异常：声称位于包内但越界、缺失或不安全时抛出 ``ValueError``。
    """

    raw_path = definition.get("file_path")
    if not isinstance(raw_path, str) or not raw_path:
        return None
    declaration = Path(raw_path)
    if not declaration.is_absolute():
        return None
    try:
        declaration.relative_to(startup_plan.package_directory)
        logical = declaration.relative_to(startup_plan.source.root).as_posix()
    except ValueError:
        return None
    if not startup_plan.source.has_file(logical):
        raise ValueError("工作区模型声明文件不存在")
    return declaration


def _logical_model_path(
    startup_plan: WorkspaceStartupPlan,
    declaration_file: Path,
    entry: str,
) -> str:
    """解析声明文件相对模型入口为安全工作区逻辑路径。

    参数：启动计划、声明文件与模型入口。返回：工作区相对 POSIX 路径。
    异常：绝对、反斜杠、父目录或导入包越界时抛出 ``ValueError``。
    """

    if "\\" in entry:
        raise ValueError("工作区模型资产入口必须使用 POSIX 路径")
    relative_entry = PurePosixPath(entry)
    if relative_entry.is_absolute() or any(
        part in {"", ".", ".."} for part in relative_entry.parts
    ):
        raise ValueError("工作区模型资产入口不得包含绝对或父目录语义")
    declaration_directory = declaration_file.parent.relative_to(
        startup_plan.source.root
    )
    logical_entry = PurePosixPath(declaration_directory.as_posix()).joinpath(
        relative_entry
    )
    if logical_entry.parts[:1] != (startup_plan.import_package,):
        raise ValueError("工作区模型资产入口必须位于导入包内")
    return logical_entry.as_posix()


def _public_model_path(
    startup_plan: WorkspaceStartupPlan,
    logical_path: str,
) -> str:
    """生成当前发行包内模型资产的公共 HTTP 路径。

    参数：启动计划和安全逻辑路径。返回：百分号编码后的公共路径。异常：无。
    """

    return (
        "/api/v1/material-models/"
        + quote(startup_plan.distribution_name, safe="")
        + "/"
        + quote(logical_path, safe="/")
    )


def _required_text(value: object, field: str) -> str:
    """读取非空字符串。参数：可疑值与字段名。返回：去空白值。异常：非法时失败。"""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")
    return value.strip()


def _json_value(value: object, field: str) -> Any:
    """复制严格 JSON 值。参数：值与字段名。返回：隔离副本。异常：非法时失败。"""

    try:
        return json.loads(
            json.dumps(
                value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            )
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} 必须是严格 JSON 值") from error


__all__ = [
    "WorkspaceMaterialModelAsset",
    "WorkspaceMaterialModelCatalog",
    "compile_workspace_material_models",
]
