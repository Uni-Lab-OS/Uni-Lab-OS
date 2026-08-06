"""软件包项目元数据的唯一静态解析入口。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import tomllib


@dataclass(frozen=True, slots=True)
class PackageProject:
    """规范化前后身份一致的软件包项目声明。"""

    name: str
    normalized_name: str
    version: str
    description: str
    license: str
    homepage: str
    requires_python: str
    dependencies: tuple[str, ...]
    registry_paths: tuple[str, ...]


def normalize_distribution_name(distribution_name: str) -> str:
    """把发行包名称规范化为唯一 Python 导入包身份。

    参数：``distribution_name`` 是 ``pyproject.toml`` 的 ``project.name``。
    返回：小写并把连字符、点和下划线段统一为下划线的 Python 标识符。
    异常：名称不是非空字符串或不能形成 Python 标识符时抛出 ``ValueError``。
    """

    if not isinstance(distribution_name, str) or not distribution_name.strip():
        raise ValueError("pyproject.toml project.name 必须是非空字符串")
    # ``normalized_name`` 同时决定导入包和社区命名空间，不接受运行时猜测。
    normalized_name = re.sub(r"[-_.]+", "_", distribution_name.strip().lower())
    if not normalized_name.isidentifier():
        raise ValueError("工作区发行包名称不能规范化为 Python 包身份")
    return normalized_name


def parse_project_metadata(raw: bytes) -> PackageProject:
    """静态解析 ``pyproject.toml`` 的封闭项目元数据子集。

    参数：``raw`` 是显式软件包来源读取的原始 TOML 字节。
    返回：供工作区、注册表（Registry）和 ``package inspect`` 共同使用的不可变声明。
    异常：UTF-8、TOML 或项目字段形状无效时抛出 ``ValueError``/``TypeError``。
    """

    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError("工作区 pyproject.toml 无效") from error
    project = document.get("project")
    if not isinstance(project, dict):
        raise TypeError("pyproject.toml 缺少合法 [project]")
    name = project.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("pyproject.toml project.name 必须是非空字符串")
    version_value = project.get("version", "0.0.0")
    if not isinstance(version_value, str) or not version_value.strip():
        raise ValueError("pyproject.toml project.version 必须是非空字符串")
    dependencies_value = project.get("dependencies", [])
    if not isinstance(dependencies_value, list) or any(
        not isinstance(item, str) or not item.strip() for item in dependencies_value
    ):
        raise TypeError("pyproject.toml project.dependencies 必须是非空字符串列表")
    # ``license_text`` 是兼容 PEP 621 字符串和旧表形状的只读展示元数据。
    license_value = project.get("license", "")
    if isinstance(license_value, dict):
        license_text = str(license_value.get("text") or license_value.get("file") or "")
    elif isinstance(license_value, str):
        license_text = license_value
    else:
        raise TypeError("pyproject.toml project.license 必须是字符串或表")
    urls = project.get("urls", {})
    if not isinstance(urls, dict):
        raise TypeError("pyproject.toml project.urls 必须是表")
    homepage = next(
        (
            str(urls[key])
            for key in (
                "Homepage",
                "homepage",
                "Repository",
                "repository",
                "Source",
                "source",
            )
            if isinstance(urls.get(key), str) and urls[key]
        ),
        "",
    )
    description = project.get("description", "")
    requires_python = project.get("requires-python", "")
    if not isinstance(description, str) or not isinstance(requires_python, str):
        raise TypeError("pyproject.toml 项目描述和 Python 版本要求必须是字符串")
    # ``registry_paths`` 是注册表（Registry）与包工具共用的显式目录声明。
    tool = document.get("tool", {})
    if not isinstance(tool, dict):
        raise TypeError("pyproject.toml tool 必须是表")
    unilabos_tool = tool.get("unilabos", {})
    if not isinstance(unilabos_tool, dict):
        raise TypeError("pyproject.toml tool.unilabos 必须是表")
    registry_tool = unilabos_tool.get("registry", {})
    if not isinstance(registry_tool, dict):
        raise TypeError("pyproject.toml tool.unilabos.registry 必须是表")
    registry_paths_value = registry_tool.get("paths", [])
    if not isinstance(registry_paths_value, list) or any(
        not isinstance(item, str) or not item.strip() for item in registry_paths_value
    ):
        raise TypeError("pyproject.toml 注册表路径必须是非空字符串列表")
    return PackageProject(
        name=name.strip(),
        normalized_name=normalize_distribution_name(name),
        version=version_value.strip(),
        description=description,
        license=license_text,
        homepage=homepage,
        requires_python=requires_python,
        dependencies=tuple(sorted(item.strip() for item in dependencies_value)),
        registry_paths=tuple(item.strip() for item in registry_paths_value),
    )


def project_to_legacy_dict(project: PackageProject) -> dict[str, Any]:
    """把统一项目模型投影为遗留包上传代码需要的字段字典。

    参数：``project`` 是已验证的软件包项目声明。
    返回：不含额外权威、仅供现有上传适配器消费的兼容字典。
    异常：参数类型错误时抛出 ``TypeError``。
    """

    if not isinstance(project, PackageProject):
        raise TypeError("project 必须是 PackageProject")
    return {
        "name": project.name,
        "version": project.version,
        "summary": project.description,
        "license": project.license,
        "homepage": project.homepage,
        "dependencies": list(project.dependencies),
    }


__all__ = [
    "PackageProject",
    "normalize_distribution_name",
    "parse_project_metadata",
    "project_to_legacy_dict",
]
