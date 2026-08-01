"""将显式 Package Source 静态编译为确定性 PackageCatalog。"""

from __future__ import annotations

import ast
import hashlib
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

import tomllib

from unilabos.workflow.source_discovery import (
    SourceDeclarationError,
    load_editable_package_manifest,
)

from .assets import collect_model_assets
from .catalog import (
    DefinitionCatalog,
    DefinitionRecord,
    DistributionIdentity,
    PackageCatalog,
    PackageCompileError,
    PackageDiagnostic,
)
from .sources import (
    CachedArchiveSource,
    InstalledDistributionSource,
    PackageSource,
    WorkspaceSource,
)

_DEFINITION_ID = re.compile(r"^[A-Za-z0-9_]+$")
_REGISTRY_DECORATORS = "unilabos.registry.decorators"
_SUBSCRIBE_DECORATORS = "unilabos.utils.decorator"
_WORKFLOW_DECORATORS = "unilabos.workflow.authoring"


def normalize_distribution_name(name: str) -> str:
    """把 distribution name 归一化为 A 方案 import package 名。"""

    return re.sub(r"[-_.]+", "_", name.strip().lower())


def compile_package_source(source: PackageSource) -> PackageCatalog:
    if isinstance(source, WorkspaceSource):
        return _compile_workspace(source)
    if isinstance(source, (InstalledDistributionSource, CachedArchiveSource)):
        return _compile_embedded(source)
    raise TypeError(f"不支持的 PackageSource: {type(source).__name__}")


def _compile_embedded(
    source: InstalledDistributionSource | CachedArchiveSource,
) -> PackageCatalog:
    try:
        payload = source.embedded_catalog_bytes()
    except ValueError as exc:
        code = (
            "ARTIFACT_DIGEST_MISMATCH"
            if "artifact digest mismatch" in str(exc)
            else "CATALOG_INVALID"
        )
        raise _embedded_error(code, str(exc)) from exc
    try:
        catalog = PackageCatalog.from_canonical_bytes(payload)
    except ValueError as exc:
        raise _embedded_error("CATALOG_INVALID", str(exc)) from exc

    expected_catalog_path = f"{catalog.import_package}/_generated/package.catalog.json"
    if expected_catalog_path not in source.members():
        raise _embedded_error(
            "CATALOG_PATH_INVALID",
            f"embedded Catalog 必须位于 {expected_catalog_path}",
        )
    if catalog.distribution.normalized_name != catalog.import_package or (
        catalog.namespace != f"community.{catalog.import_package}"
    ):
        raise _embedded_error(
            "CATALOG_IDENTITY_MISMATCH",
            "embedded Catalog 的 distribution/import package/namespace 不一致",
        )
    if isinstance(source, InstalledDistributionSource):
        try:
            installed_name = str(source._distribution().metadata["Name"] or "")
        except ValueError as exc:
            raise _embedded_error("DISTRIBUTION_INVALID", str(exc)) from exc
        if normalize_distribution_name(installed_name) != catalog.import_package:
            raise _embedded_error(
                "DISTRIBUTION_IDENTITY_MISMATCH",
                f"installed distribution {installed_name!r} 与 Catalog 不一致",
            )

    members = _validate_embedded_payload(source, catalog)

    checked_declarations: set[str] = set()
    for record in (
        *catalog.definitions.devices,
        *catalog.definitions.resources,
        *catalog.definitions.workflows,
    ):
        if record.declaring_file in checked_declarations:
            continue
        checked_declarations.add(record.declaring_file)
        try:
            content = source.read_bytes(record.declaring_file)
        except ValueError as exc:
            raise _embedded_error("DECLARATION_MISSING", str(exc)) from exc
        actual = "sha256:" + hashlib.sha256(content).hexdigest()
        if actual != record.content_hash:
            raise _embedded_error(
                "DECLARATION_DIGEST_MISMATCH",
                f"{record.declaring_file}: {actual} != {record.content_hash}",
            )

    for asset in catalog.assets:
        try:
            content = source.read_bytes(asset.logical_path)
        except ValueError as exc:
            raise _embedded_error("ASSET_MISSING", str(exc)) from exc
        actual = "sha256:" + hashlib.sha256(content).hexdigest()
        if actual != asset.digest or len(content) != asset.size:
            raise _embedded_error(
                "ASSET_DIGEST_MISMATCH",
                f"{asset.logical_path}: content 与 Catalog 不一致",
            )

    actual_content_digest = _embedded_content_digest(source, catalog)
    if actual_content_digest != catalog.content_digest:
        raise _embedded_error(
            "CONTENT_DIGEST_MISMATCH",
            f"{actual_content_digest} != {catalog.content_digest}",
        )

    rebuilt = _recompile_embedded_source(source, catalog, members)
    if rebuilt.to_canonical_bytes() != catalog.to_canonical_bytes():
        raise _embedded_error(
            "CATALOG_SOURCE_MISMATCH",
            "embedded Catalog 与 artifact 内源码重新编译结果不一致",
        )
    return catalog


def _validate_embedded_payload(
    source: InstalledDistributionSource | CachedArchiveSource,
    catalog: PackageCatalog,
) -> tuple[str, ...]:
    """验证 artifact closure，并返回唯一的常规文件成员。"""

    try:
        raw_members = source.members()
    except ValueError as exc:
        raise _embedded_error("ARTIFACT_PAYLOAD_INVALID", str(exc)) from exc
    if len(raw_members) != len(set(raw_members)):
        raise _embedded_error(
            "ARTIFACT_PAYLOAD_INVALID",
            "artifact 包含重复成员路径",
        )

    files: list[str] = []
    roots: set[str] = set()
    for name in raw_members:
        logical = PurePosixPath(name)
        if (
            not name
            or logical.is_absolute()
            or ".." in logical.parts
            or "\\" in name
            or not logical.parts
        ):
            raise _embedded_error(
                "ARTIFACT_PAYLOAD_INVALID",
                f"artifact 成员路径非法: {name!r}",
            )
        root = logical.parts[0]
        roots.add(root)
        if not name.endswith("/"):
            files.append(name)

    unexpected = sorted(
        root
        for root in roots
        if root != catalog.import_package
        and not root.endswith(".dist-info")
        and not root.endswith(".data")
    )
    if unexpected:
        raise _embedded_error(
            "ARTIFACT_PAYLOAD_INVALID",
            "artifact 包含额外顶层 payload: " + ", ".join(unexpected),
        )
    return tuple(sorted(files))


def _recompile_embedded_source(
    source: InstalledDistributionSource | CachedArchiveSource,
    catalog: PackageCatalog,
    members: tuple[str, ...],
) -> PackageCatalog:
    """只从 artifact 内源码重建临时 Workspace，再走唯一编译入口。"""

    generated_root = f"{catalog.import_package}/_generated/"
    package_root = f"{catalog.import_package}/"
    pyproject_member = f"{generated_root}pyproject.toml"
    manifest_member = f"{generated_root}package.yaml"
    try:
        pyproject = source.read_bytes(pyproject_member)
    except ValueError as exc:
        raise _embedded_error("PYPROJECT_SNAPSHOT_MISSING", str(exc)) from exc

    with tempfile.TemporaryDirectory(prefix="unilab-package-recompile-") as temporary:
        root = Path(temporary)
        (root / "pyproject.toml").write_bytes(pyproject)
        if manifest_member in members:
            try:
                (root / "package.yaml").write_bytes(source.read_bytes(manifest_member))
            except ValueError as exc:
                raise _embedded_error("MANIFEST_SNAPSHOT_INVALID", str(exc)) from exc
        for name in members:
            if not name.startswith(package_root) or name.startswith(generated_root):
                continue
            target = root.joinpath(*PurePosixPath(name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                target.write_bytes(source.read_bytes(name))
            except ValueError as exc:
                raise _embedded_error("ARTIFACT_PAYLOAD_INVALID", str(exc)) from exc
        return _compile_workspace(WorkspaceSource(root))


def _embedded_content_digest(
    source: InstalledDistributionSource | CachedArchiveSource,
    catalog: PackageCatalog,
) -> str:
    pyproject_member = f"{catalog.import_package}/_generated/pyproject.toml"
    try:
        pyproject = source.read_bytes(pyproject_member)
    except ValueError as exc:
        raise _embedded_error("PYPROJECT_SNAPSHOT_MISSING", str(exc)) from exc
    python_files = sorted(
        name
        for name in source.members()
        if name.startswith(f"{catalog.import_package}/")
        and name.endswith(".py")
        and "/_generated/" not in name
        and "/__pycache__/" not in name
    )
    content_paths = sorted(
        [*python_files, *(asset.logical_path for asset in catalog.assets)]
    )
    digest = hashlib.sha256()
    digest.update(b"pyproject.toml\0")
    digest.update(pyproject)
    digest.update(b"\0")
    for logical_path in content_paths:
        digest.update(logical_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(source.read_bytes(logical_path))
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _embedded_error(code: str, message: str) -> PackageCompileError:
    return PackageCompileError(
        [
            PackageDiagnostic(
                code=code,
                severity="error",
                message=message,
            )
        ]
    )


def _compile_workspace(source: WorkspaceSource) -> PackageCatalog:
    root = source.root.resolve()
    diagnostics: list[PackageDiagnostic] = []
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        raise PackageCompileError(
            [
                PackageDiagnostic(
                    code="PYPROJECT_MISSING",
                    severity="error",
                    message="Package Workspace 根目录缺少 pyproject.toml",
                    path="pyproject.toml",
                )
            ]
        )

    try:
        document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise PackageCompileError(
            [
                PackageDiagnostic(
                    code="PYPROJECT_INVALID",
                    severity="error",
                    message=str(exc),
                    path="pyproject.toml",
                )
            ]
        ) from exc

    project = document.get("project")
    if not isinstance(project, dict) or not isinstance(project.get("name"), str):
        raise PackageCompileError(
            [
                PackageDiagnostic(
                    code="PROJECT_NAME_MISSING",
                    severity="error",
                    message="pyproject.toml [project].name 必须是非空字符串",
                    path="pyproject.toml",
                )
            ]
        )

    distribution_name = project["name"].strip()
    import_package = normalize_distribution_name(distribution_name)
    package_root = root / import_package
    version = project.get("version")
    if not isinstance(version, str) or not version.strip():
        diagnostics.append(
            PackageDiagnostic(
                code="PROJECT_VERSION_MISSING",
                severity="error",
                message="pyproject.toml [project].version 必须是非空字符串",
                path="pyproject.toml",
            )
        )
    requires_python = project.get("requires-python", "")
    if not isinstance(requires_python, str):
        diagnostics.append(
            PackageDiagnostic(
                code="PROJECT_REQUIRES_PYTHON_INVALID",
                severity="error",
                message="pyproject.toml [project].requires-python 必须是字符串",
                path="pyproject.toml",
            )
        )
    dependencies = project.get("dependencies", [])
    if not isinstance(dependencies, list) or any(
        not isinstance(item, str) or not item.strip() for item in dependencies
    ):
        diagnostics.append(
            PackageDiagnostic(
                code="PROJECT_DEPENDENCIES_INVALID",
                severity="error",
                message="pyproject.toml [project].dependencies 必须是字符串数组",
                path="pyproject.toml",
            )
        )
    if not import_package or not import_package.isidentifier():
        diagnostics.append(
            PackageDiagnostic(
                code="IMPORT_PACKAGE_NAME_INVALID",
                severity="error",
                message=(
                    "distribution 无法归一化为合法 import package: "
                    f"{distribution_name!r}"
                ),
                path="pyproject.toml",
            )
        )
    elif package_root.is_symlink():
        diagnostics.append(
            PackageDiagnostic(
                code="IMPORT_PACKAGE_SYMLINK_UNSAFE",
                severity="error",
                message=f"import package 不得是 symlink: {import_package}",
                path=import_package,
            )
        )
    elif not (package_root / "__init__.py").is_file():
        diagnostics.append(
            PackageDiagnostic(
                code="IMPORT_PACKAGE_MISSING",
                severity="error",
                message=f"必须存在唯一常规 import package: {import_package}",
                path=import_package,
            )
        )
    top_level_packages = sorted(
        path.name
        for path in root.iterdir()
        if path.is_dir()
        and not path.name.startswith(".")
        and (path / "__init__.py").is_file()
        and path.name != import_package
    )
    if top_level_packages:
        diagnostics.append(
            PackageDiagnostic(
                code="TOP_LEVEL_PACKAGE_AMBIGUOUS",
                severity="error",
                message=(
                    "Package Workspace 只能包含目标顶层 import package；额外发现: "
                    + ", ".join(top_level_packages)
                ),
                path=top_level_packages[0],
            )
        )
    if diagnostics:
        raise PackageCompileError(diagnostics)

    python_candidates = sorted(
        path
        for path in package_root.rglob("*.py")
        if path.is_file()
        and "__pycache__" not in path.parts
        and "_generated" not in path.parts
    )
    python_files: list[Path] = []
    for path in python_candidates:
        relative = path.relative_to(root).as_posix()
        package_relative = path.relative_to(package_root)
        parents = package_relative.parents
        symlink_parent = any(
            (package_root / parent).is_symlink()
            for parent in parents
            if parent != Path(".")
        )
        if path.is_symlink() or symlink_parent:
            diagnostics.append(
                PackageDiagnostic(
                    code="PYTHON_SYMLINK_UNSAFE",
                    severity="error",
                    message=f"Python definition source 不得是 symlink: {relative}",
                    path=relative,
                )
            )
            continue
        python_files.append(path)
    namespace = f"community.{import_package}"
    devices: list[DefinitionRecord] = []
    resources: list[DefinitionRecord] = []
    workflows: list[DefinitionRecord] = []
    declared_workflows: dict[str, str] = {}
    manifest_path = root / "package.yaml"
    if manifest_path.exists():
        try:
            manifest = load_editable_package_manifest(root)
        except SourceDeclarationError as exc:
            diagnostics.append(
                PackageDiagnostic(
                    code="WORKFLOW_MANIFEST_INVALID",
                    severity="error",
                    message=str(exc),
                    path="package.yaml",
                )
            )
        else:
            if manifest.package_id != import_package:
                diagnostics.append(
                    PackageDiagnostic(
                        code="WORKFLOW_PACKAGE_ID_MISMATCH",
                        severity="error",
                        message=(
                            "package.yaml package.name 必须等于 import package: "
                            f"{import_package}"
                        ),
                        path="package.yaml",
                    )
                )
            declared_workflows = {
                f"{import_package}/{item.relative_path}": item.workflow_uuid
                for item in manifest.workflows
            }
    discovered_workflow_paths: set[str] = set()

    for path in python_files:
        relative = path.relative_to(root).as_posix()
        try:
            source_text = path.read_text(encoding="utf-8")
            tree = ast.parse(source_text, filename=relative)
        except (OSError, UnicodeError, SyntaxError) as exc:
            diagnostics.append(
                PackageDiagnostic(
                    code="PYTHON_SOURCE_INVALID",
                    severity="error",
                    message=str(exc),
                    path=relative,
                    line=getattr(exc, "lineno", None),
                )
            )
            continue

        module = _module_name(path, root)
        imports = _import_map(tree)
        workflow_device_handles = _workflow_device_handles(tree, imports)
        file_digest = (
            "sha256:" + hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        )
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                decorator = _find_decorator(
                    node, imports, _REGISTRY_DECORATORS, "device"
                )
                if decorator is not None:
                    args = _decorator_args(decorator)
                    _validate_definition_metadata(
                        args, relative, node.lineno, diagnostics
                    )
                    ids = _definition_ids(args, relative, node.lineno, diagnostics)
                    for definition_id in ids:
                        devices.append(
                            _device_record(
                                node=node,
                                args=_device_args_for_id(args, definition_id),
                                definition_id=definition_id,
                                namespace=namespace,
                                module=module,
                                relative=relative,
                                file_digest=file_digest,
                                imports=imports,
                            )
                        )

                decorator = _find_decorator(
                    node, imports, _REGISTRY_DECORATORS, "resource"
                )
                if decorator is not None:
                    args = _decorator_args(decorator)
                    _validate_definition_metadata(
                        args, relative, node.lineno, diagnostics
                    )
                    definition_id = _single_definition_id(
                        args, relative, node.lineno, diagnostics
                    )
                    if definition_id:
                        resources.append(
                            _resource_record(
                                node=node,
                                args=args,
                                definition_id=definition_id,
                                namespace=namespace,
                                module=module,
                                relative=relative,
                                file_digest=file_digest,
                            )
                        )

            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                resource_decorator = _find_decorator(
                    node, imports, _REGISTRY_DECORATORS, "resource"
                )
                if resource_decorator is not None:
                    args = _decorator_args(resource_decorator)
                    _validate_definition_metadata(
                        args, relative, node.lineno, diagnostics
                    )
                    definition_id = _single_definition_id(
                        args, relative, node.lineno, diagnostics
                    )
                    if definition_id:
                        resources.append(
                            _resource_record(
                                node=node,
                                args=args,
                                definition_id=definition_id,
                                namespace=namespace,
                                module=module,
                                relative=relative,
                                file_digest=file_digest,
                            )
                        )

                workflow_decorator = _find_decorator(
                    node, imports, _WORKFLOW_DECORATORS, "workflow_definition"
                )
                if workflow_decorator is not None:
                    args = _decorator_args(workflow_decorator)
                    _validate_definition_metadata(
                        args, relative, node.lineno, diagnostics
                    )
                    declared_uuid = declared_workflows.get(relative)
                    # 目录中的 decorator 不是持久 source identity。只有
                    # package.yaml 显式登记的 Draft 才进入 PackageCatalog。
                    if declared_uuid is None:
                        continue
                    workflow_uuid = args.get("workflow_uuid")
                    if workflow_uuid != declared_uuid:
                        diagnostics.append(
                            PackageDiagnostic(
                                code="WORKFLOW_UUID_MISMATCH",
                                severity="error",
                                message=(
                                    "@workflow_definition.workflow_uuid 必须与 "
                                    "package.yaml 声明一致"
                                ),
                                path=relative,
                                line=node.lineno,
                            )
                        )
                    else:
                        discovered_workflow_paths.add(relative)
                        workflows.append(
                            _workflow_record(
                                node=node,
                                args=args,
                                definition_id=node.name,
                                workflow_uuid=declared_uuid,
                                namespace=namespace,
                                import_package=import_package,
                                module=module,
                                relative=relative,
                                file_digest=file_digest,
                                device_handles=workflow_device_handles,
                            )
                        )

    all_records, assets, asset_diagnostics, asset_paths = collect_model_assets(
        root, (*devices, *resources, *workflows)
    )
    diagnostics.extend(asset_diagnostics)
    devices = [record for record in all_records if record.kind == "device"]
    resources = [record for record in all_records if record.kind == "resource"]
    workflows = [record for record in all_records if record.kind == "workflow"]
    _check_duplicates(tuple(all_records), diagnostics)
    _validate_workflow_refs(devices, workflows, diagnostics)
    for relative, workflow_uuid in sorted(declared_workflows.items()):
        if relative not in discovered_workflow_paths:
            diagnostics.append(
                PackageDiagnostic(
                    code="WORKFLOW_SOURCE_DECLARATION_MISSING",
                    severity="error",
                    message=(
                        f"已登记 Workflow {workflow_uuid} 的源码缺少匹配的 "
                        "@workflow_definition"
                    ),
                    path=relative,
                )
            )
    if any(item.severity == "error" for item in diagnostics):
        raise PackageCompileError(diagnostics)

    content_digest = _workspace_content_digest(
        root, pyproject, [*python_files, *asset_paths]
    )
    return PackageCatalog.create(
        distribution=DistributionIdentity(
            name=distribution_name,
            normalized_name=import_package,
            version=str(version),
            requires_python=str(requires_python),
            dependencies=tuple(str(item) for item in dependencies),
        ),
        import_package=import_package,
        namespace=namespace,
        definitions=DefinitionCatalog(
            devices=tuple(devices),
            resources=tuple(resources),
            workflows=tuple(workflows),
        ),
        assets=assets,
        content_digest=content_digest,
        diagnostics=tuple(diagnostics),
    )


def _module_name(path: Path, root: Path) -> str:
    relative = path.relative_to(root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _import_map(tree: ast.Module) -> dict[str, str]:
    imports: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                imports[alias.asname or alias.name] = f"{module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports[alias.asname or alias.name.split(".")[0]] = alias.name
    return imports


def _expression_path(node: ast.expr, imports: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return imports.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = _expression_path(node.value, imports)
        return f"{base}.{node.attr}"
    return ""


def _find_decorator(
    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
    imports: dict[str, str],
    module: str,
    name: str,
) -> ast.Call | None:
    expected = f"{module}.{name}"
    for decorator in node.decorator_list:
        if (
            isinstance(decorator, ast.Call)
            and _expression_path(decorator.func, imports) == expected
        ):
            return decorator
    return None


_DYNAMIC = object()


def _static_value(node: ast.expr) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        values = [_static_value(item) for item in node.elts]
        return _DYNAMIC if _DYNAMIC in values else values
    if isinstance(node, ast.Dict):
        result: dict[str, Any] = {}
        for key_node, value_node in zip(node.keys, node.values):
            if key_node is None:
                return _DYNAMIC
            key = _static_value(key_node)
            value = _static_value(value_node)
            if not isinstance(key, str) or value is _DYNAMIC:
                return _DYNAMIC
            result[key] = value
        return result
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        value = _static_value(node.operand)
        if isinstance(value, (int, float)):
            return -value
    if isinstance(node, ast.Name):
        return {"$name": node.id}
    if isinstance(node, ast.Attribute):
        return {"$name": ast.unparse(node)}
    if isinstance(node, ast.Call):
        return {
            "$call": ast.unparse(node.func),
            "args": [_static_json_or_ast(item) for item in node.args],
            "kwargs": {
                keyword.arg: _static_json_or_ast(keyword.value)
                for keyword in node.keywords
                if keyword.arg is not None
            },
        }
    return _DYNAMIC


def _static_json_or_ast(node: ast.expr) -> Any:
    value = _static_value(node)
    if value is _DYNAMIC:
        return {"$ast": ast.dump(node, include_attributes=False)}
    return value


def _decorator_args(node: ast.Call) -> dict[str, Any]:
    return {
        keyword.arg: _static_json_or_ast(keyword.value)
        for keyword in node.keywords
        if keyword.arg is not None
    }


def _validate_definition_metadata(
    args: dict[str, Any],
    path: str,
    line: int,
    diagnostics: list[PackageDiagnostic],
) -> None:
    """Definition identity 与展示元数据必须能由 AST 完整决定。"""

    invalid: list[str] = []
    for name in (
        "category",
        "class_type",
        "description",
        "device_type",
        "display_name",
        "displayname",
        "icon",
        "manufacturer",
        "metadata",
        "model",
        "version",
    ):
        if name in args and _contains_dynamic_expression(args[name]):
            invalid.append(name)
    category = args.get("category")
    if "category" in args and (
        not isinstance(category, list)
        or any(not isinstance(item, str) for item in category)
    ):
        invalid.append("category")
    for name in (
        "class_type",
        "description",
        "device_type",
        "display_name",
        "displayname",
        "icon",
        "manufacturer",
        "version",
    ):
        if name in args and not isinstance(args[name], str):
            invalid.append(name)
    for name in ("metadata", "model"):
        if name in args and not isinstance(args[name], dict):
            invalid.append(name)
    id_meta = args.get("id_meta")
    if "id_meta" in args and not isinstance(id_meta, dict):
        invalid.append("id_meta")
    elif isinstance(id_meta, dict):
        for definition_id, override in id_meta.items():
            if not isinstance(definition_id, str) or not isinstance(override, dict):
                invalid.append("id_meta")
                continue
            for name in (
                "category",
                "class_type",
                "description",
                "device_type",
                "display_name",
                "displayname",
                "icon",
                "manufacturer",
                "metadata",
                "model",
                "version",
            ):
                if name in override and _contains_dynamic_expression(override[name]):
                    invalid.append(f"id_meta.{definition_id}.{name}")
    if invalid:
        diagnostics.append(
            PackageDiagnostic(
                code="DEFINITION_METADATA_DYNAMIC",
                severity="error",
                message=(
                    "definition metadata 必须是静态且类型合法: "
                    + ", ".join(sorted(set(invalid)))
                ),
                path=path,
                line=line,
            )
        )


def _contains_dynamic_expression(value: Any) -> bool:
    if isinstance(value, list):
        return any(_contains_dynamic_expression(item) for item in value)
    if not isinstance(value, dict):
        return False
    if any(name in value for name in ("$ast", "$call", "$name")):
        return True
    return any(_contains_dynamic_expression(item) for item in value.values())


def _definition_ids(
    args: dict[str, Any],
    path: str,
    line: int,
    diagnostics: list[PackageDiagnostic],
) -> list[str]:
    if "ids" in args:
        raw_ids = args["ids"]
        if (
            isinstance(raw_ids, list)
            and raw_ids
            and all(
                isinstance(item, str) and _DEFINITION_ID.fullmatch(item)
                for item in raw_ids
            )
        ):
            return raw_ids
    else:
        raw_id = args.get("id") or args.get("device_id")
        if isinstance(raw_id, str) and _DEFINITION_ID.fullmatch(raw_id):
            return [raw_id]
    diagnostics.append(
        PackageDiagnostic(
            code="DEVICE_ID_DYNAMIC",
            severity="error",
            message="device id/ids 必须是静态且仅含英文、数字、下划线的字符串",
            path=path,
            line=line,
        )
    )
    return []


def _device_args_for_id(args: dict[str, Any], definition_id: str) -> dict[str, Any]:
    id_meta = args.get("id_meta")
    if not isinstance(id_meta, dict):
        return args
    override = id_meta.get(definition_id)
    if not isinstance(override, dict):
        return args
    return {**args, **override}


def _single_definition_id(
    args: dict[str, Any],
    path: str,
    line: int,
    diagnostics: list[PackageDiagnostic],
) -> str | None:
    raw_id = args.get("id") or args.get("resource_id")
    if isinstance(raw_id, str) and _DEFINITION_ID.fullmatch(raw_id):
        return raw_id
    diagnostics.append(
        PackageDiagnostic(
            code="RESOURCE_ID_DYNAMIC",
            severity="error",
            message="resource id 必须是静态且仅含英文、数字、下划线的字符串",
            path=path,
            line=line,
        )
    )
    return None


def _common_record_fields(args: dict[str, Any], definition_id: str) -> dict[str, Any]:
    category = args.get("category")
    return {
        "version": str(args.get("version") or "1.0.0"),
        "displayname": str(
            args.get("displayname") or args.get("display_name") or definition_id
        ),
        "description": str(args.get("description") or ""),
        "category": tuple(item for item in category if isinstance(item, str))
        if isinstance(category, list)
        else (),
        "manufacturer": str(args.get("manufacturer") or ""),
    }


def _parameter_records(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[dict[str, Any]]:
    positional = list(node.args.posonlyargs) + list(node.args.args)
    defaults: list[ast.expr | None] = [None] * (
        len(positional) - len(node.args.defaults)
    ) + list(node.args.defaults)
    result: list[dict[str, Any]] = []
    for argument, default in zip(positional, defaults):
        if argument.arg in {"self", "cls"}:
            continue
        item: dict[str, Any] = {
            "name": argument.arg,
            "required": default is None,
            "type": ast.unparse(argument.annotation) if argument.annotation else "Any",
        }
        if default is not None:
            item["default"] = _static_json_or_ast(default)
        result.append(item)
    for argument, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
        item = {
            "name": argument.arg,
            "required": default is None,
            "type": ast.unparse(argument.annotation) if argument.annotation else "Any",
        }
        if default is not None:
            item["default"] = _static_json_or_ast(default)
        result.append(item)
    return result


def _action_records(
    node: ast.ClassDef, imports: dict[str, str]
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for item in node.body:
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorator = _find_decorator(item, imports, _REGISTRY_DECORATORS, "action")
        if decorator is None:
            continue
        actions.append(
            {
                "decorator": _decorator_args(decorator),
                "docstring": ast.get_docstring(item) or "",
                "is_async": isinstance(item, ast.AsyncFunctionDef),
                "name": item.name,
                "parameters": _parameter_records(item),
                "return_type": ast.unparse(item.returns) if item.returns else "Any",
            }
        )
    return sorted(actions, key=lambda item: item["name"])


def _status_records(
    node: ast.ClassDef, imports: dict[str, str]
) -> list[dict[str, Any]]:
    statuses: list[dict[str, Any]] = []
    for item in node.body:
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        is_property = any(
            not isinstance(decorator, ast.Call)
            and _expression_path(decorator, imports) == "property"
            for decorator in item.decorator_list
        )
        topic = _find_decorator(item, imports, _REGISTRY_DECORATORS, "topic_config")
        if not is_property and topic is None:
            continue
        name = (
            item.name[4:]
            if not is_property and item.name.startswith("get_")
            else item.name
        )
        statuses.append(
            {
                "is_property": is_property,
                "name": name,
                "return_type": ast.unparse(item.returns) if item.returns else "Any",
                "topic_config": _decorator_args(topic) if topic is not None else {},
            }
        )
    return sorted(statuses, key=lambda item: item["name"])


def _subscription_records(
    node: ast.ClassDef, imports: dict[str, str]
) -> list[dict[str, Any]]:
    subscriptions: list[dict[str, Any]] = []
    for item in node.body:
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorator = _find_decorator(item, imports, _SUBSCRIBE_DECORATORS, "subscribe")
        if decorator is None:
            continue
        subscriptions.append(
            {
                "callback": item.name,
                "config": _decorator_args(decorator),
                "parameters": _parameter_records(item),
            }
        )
    return sorted(subscriptions, key=lambda item: item["callback"])


def _device_record(
    *,
    node: ast.ClassDef,
    args: dict[str, Any],
    definition_id: str,
    namespace: str,
    module: str,
    relative: str,
    file_digest: str,
    imports: dict[str, str],
) -> DefinitionRecord:
    init_node = next(
        (
            item
            for item in node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            and item.name == "__init__"
        ),
        None,
    )
    details = {
        "actions": _action_records(node, imports),
        "device_type": args.get("device_type", "python"),
        "handles": args.get("handles", []),
        "hardware_interface": args.get("hardware_interface"),
        "icon": args.get("icon", ""),
        "imports": imports,
        "init_docstring": ast.get_docstring(init_node) if init_node else "",
        "init_parameters": _parameter_records(init_node) if init_node else [],
        "model": args.get("model"),
        "metadata": args.get("metadata", {}),
        "status_properties": _status_records(node, imports),
        "subscriptions": _subscription_records(node, imports),
    }
    return DefinitionRecord(
        kind="device",
        id=definition_id,
        fqid=f"{namespace}.{definition_id}",
        module=module,
        symbol=node.name,
        declaring_file=relative,
        content_hash=file_digest,
        details=details,
        **_common_record_fields(args, definition_id),
    )


def _resource_record(
    *,
    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
    args: dict[str, Any],
    definition_id: str,
    namespace: str,
    module: str,
    relative: str,
    file_digest: str,
) -> DefinitionRecord:
    init_node: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    if isinstance(node, ast.ClassDef):
        init_node = next(
            (
                item
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and item.name == "__init__"
            ),
            None,
        )
    else:
        init_node = node
    details = {
        "class_type": args.get("class_type", "pylabrobot"),
        "factory_kind": "class" if isinstance(node, ast.ClassDef) else "function",
        "handles": args.get("handles", []),
        "icon": args.get("icon", ""),
        "model": args.get("model"),
        "metadata": args.get("metadata", {}),
        "parameters": _parameter_records(init_node) if init_node else [],
    }
    return DefinitionRecord(
        kind="resource",
        id=definition_id,
        fqid=f"{namespace}.{definition_id}",
        module=module,
        symbol=node.name,
        declaring_file=relative,
        content_hash=file_digest,
        details=details,
        **_common_record_fields(args, definition_id),
    )


def _workflow_record(
    *,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    args: dict[str, Any],
    definition_id: str,
    workflow_uuid: str,
    namespace: str,
    import_package: str,
    module: str,
    relative: str,
    file_digest: str,
    device_handles: dict[str, str],
) -> DefinitionRecord:
    details = {
        "action_refs": _workflow_action_refs(node, device_handles),
        "parameters": _parameter_records(node),
        "source_uri": (
            f"package://{import_package}/{relative.removeprefix(import_package + '/')}"
        ),
        "workflow_uuid": workflow_uuid,
        "source_identity": {
            "content_hash": file_digest,
            "module": module,
            "namespace": namespace,
            "symbol": node.name,
            "workflow_uuid": workflow_uuid,
        },
    }
    return DefinitionRecord(
        kind="workflow",
        id=definition_id,
        fqid=f"{namespace}.{definition_id}",
        module=module,
        symbol=node.name,
        declaring_file=relative,
        content_hash=file_digest,
        details=details,
        **_common_record_fields(args, definition_id),
    )


def _workflow_device_handles(
    tree: ast.Module, imports: dict[str, str]
) -> dict[str, str]:
    handles: dict[str, str] = {}
    expected = f"{_WORKFLOW_DECORATORS}.device"
    for statement in tree.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        target: ast.expr | None
        value: ast.expr | None
        if isinstance(statement, ast.Assign):
            target = statement.targets[0] if len(statement.targets) == 1 else None
            value = statement.value
        else:
            target = statement.target
            value = statement.value
        if (
            not isinstance(target, ast.Name)
            or not isinstance(value, ast.Call)
            or _expression_path(value.func, imports) != expected
        ):
            continue
        raw_device_id: Any = _DYNAMIC
        if len(value.args) == 1 and not value.keywords:
            raw_device_id = _static_value(value.args[0])
        elif not value.args:
            for keyword in value.keywords:
                if keyword.arg == "device_id":
                    raw_device_id = _static_value(keyword.value)
        if isinstance(raw_device_id, str) and raw_device_id:
            handles[target.id] = raw_device_id
    return handles


def _workflow_action_refs(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    device_handles: dict[str, str],
) -> list[dict[str, Any]]:
    refs: set[tuple[str, str, int]] = set()
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and isinstance(child.func.value, ast.Name)
            and child.func.value.id in device_handles
        ):
            refs.add(
                (
                    device_handles[child.func.value.id],
                    child.func.attr,
                    child.lineno,
                )
            )
    return [
        {"action": action, "device": device, "line": line}
        for device, action, line in sorted(refs)
    ]


def _validate_workflow_refs(
    devices: list[DefinitionRecord],
    workflows: list[DefinitionRecord],
    diagnostics: list[PackageDiagnostic],
) -> None:
    actions_by_device = {
        device.id: {
            str(action["name"])
            for action in device.details.get("actions", ())
            if isinstance(action, dict) or hasattr(action, "get")
        }
        for device in devices
    }
    for workflow in workflows:
        for reference in workflow.details.get("action_refs", ()):
            device_id = str(reference["device"])
            action = str(reference["action"])
            known_actions = actions_by_device.get(device_id)
            if known_actions is None or action in known_actions:
                continue
            diagnostics.append(
                PackageDiagnostic(
                    code="WORKFLOW_ACTION_UNKNOWN",
                    severity="error",
                    message=(
                        f"workflow {workflow.id} 引用了 {device_id}.{action}，"
                        "但同包 device 未声明该 action"
                    ),
                    path=workflow.declaring_file,
                    line=int(reference["line"]),
                )
            )


def _check_duplicates(
    records: tuple[DefinitionRecord, ...], diagnostics: list[PackageDiagnostic]
) -> None:
    seen: dict[str, DefinitionRecord] = {}
    for record in records:
        existing = seen.get(record.fqid)
        if existing is None:
            seen[record.fqid] = record
            continue
        diagnostics.append(
            PackageDiagnostic(
                code="DEFINITION_ID_DUPLICATE",
                severity="error",
                message=(
                    f"{record.fqid} 同时声明于 {existing.declaring_file} 与 "
                    f"{record.declaring_file}"
                ),
                path=record.declaring_file,
            )
        )


def _workspace_content_digest(
    root: Path, pyproject: Path, content_files: list[Path]
) -> str:
    digest = hashlib.sha256()
    for path in [
        pyproject,
        *sorted(content_files, key=lambda item: item.relative_to(root).as_posix()),
    ]:
        logical_path = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(logical_path)
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


__all__ = ["compile_package_source", "normalize_distribution_name"]
