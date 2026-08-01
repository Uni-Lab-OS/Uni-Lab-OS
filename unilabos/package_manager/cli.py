"""Package-manager argparse registration, command dispatch, and presentation."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

import tomllib

from .catalog import PackageCatalog, PackageCompileError
from .compiler import compile_package_source
from .distribution import (
    BuildArtifact,
    PackageDistributionError,
    build_workspace_wheel,
)
from .publication import HttpClientPublicationAdapter, publish_build
from .sources import InstalledDistributionSource, WorkspaceSource


class PackageCommandError(RuntimeError):
    """A package command failed with a user-actionable message."""


def register_package_subcommands(subparsers: Any) -> None:
    package_parser = subparsers.add_parser(
        "package",
        aliases=["pkg"],
        help="Uni-Lab package inspect / build / upload / install",
    )
    actions = package_parser.add_subparsers(
        title="package actions",
        dest="package_action",
    )
    inspect_parser = actions.add_parser(
        "inspect",
        help="Compile and print a read-only PackageCatalog",
    )
    _add_workspace_path(inspect_parser)
    inspect_parser.add_argument(
        "--json",
        dest="package_json",
        action="store_true",
        help="Output canonical PackageCatalog JSON",
    )

    for name, help_text in (
        ("build", "Build and audit a Catalog-embedded wheel"),
        ("upload", "Build, audit, and publish a Catalog-embedded wheel"),
    ):
        command = actions.add_parser(name, help=help_text)
        _add_workspace_path(command)
        command.add_argument(
            "--out",
            type=str,
            default=None,
            help="Wheel output directory (default: sibling dist/)",
        )
        if name == "upload":
            command.add_argument(
                "--download-url",
                default="",
                help="Explicit artifact URL; skips artifact upload",
            )

    install = actions.add_parser(
        "install",
        help="Install a spec, then validate its explicit installed distribution",
    )
    install.add_argument("install_spec", help="pip requirement, path, or VCS URL")
    install.add_argument(
        "--distribution",
        default="",
        help="Installed distribution name (required for ambiguous URL/VCS specs)",
    )


def _add_workspace_path(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--path",
        dest="package_path",
        required=True,
        help="Package Workspace root containing pyproject.toml",
    )


def package_command_needs_http(args: dict[str, Any]) -> bool:
    return args.get("package_action") == "upload"


def run_package_command(
    args: dict[str, Any],
    *,
    http_client: Any = None,
    stream: TextIO | None = None,
) -> Any:
    output = stream or sys.stdout
    action = str(args.get("package_action") or "")
    if not action:
        raise PackageCommandError(
            "缺少 package 子动作，请使用 inspect|build|upload|install"
        )
    try:
        if action == "inspect":
            return inspect_workspace(
                str(args.get("package_path") or ""),
                json_output=bool(args.get("package_json") or args.get("json")),
                stream=output,
            )
        if action in {"build", "upload"}:
            workspace = Path(str(args.get("package_path") or "")).resolve()
            output_dir = (
                Path(str(args["out"])).resolve()
                if args.get("out")
                else workspace.parent / "dist"
            )
            artifact = build_workspace_wheel(workspace, output_dir)
            if action == "build":
                _write_build_summary(artifact, output)
                return artifact
            if http_client is None:
                raise PackageCommandError("package upload 需要已鉴权 HTTP client")
            result = publish_build(
                artifact,
                HttpClientPublicationAdapter(http_client),
                download_url=str(args.get("download_url") or ""),
            )
            output.write(
                f"published {artifact.catalog.distribution.name}@"
                f"{artifact.catalog.distribution.version}\n"
            )
            output.write(f"wheel: {artifact.wheel}\n")
            output.write(f"artifact_digest: {artifact.artifact_digest}\n")
            return result
        if action == "install":
            result = install_and_validate(
                str(args.get("install_spec") or ""),
                distribution=str(args.get("distribution") or ""),
            )
            output.write(
                f"installed and validated {result.distribution.name}@"
                f"{result.distribution.version}\n"
            )
            output.write(f"catalog_digest: {result.catalog_digest}\n")
            return result
        raise PackageCommandError(f"未知 package 子动作: {action}")
    except (PackageCompileError, PackageDistributionError, RuntimeError) as exc:
        if isinstance(exc, PackageCommandError):
            raise
        raise PackageCommandError(str(exc)) from exc


def _write_build_summary(artifact: BuildArtifact, output: TextIO) -> None:
    output.write(
        f"built {artifact.catalog.distribution.name}@"
        f"{artifact.catalog.distribution.version}\n"
    )
    output.write(f"wheel: {artifact.wheel}\n")
    output.write(f"catalog_digest: {artifact.catalog.catalog_digest}\n")
    output.write(f"artifact_digest: {artifact.artifact_digest}\n")


def inspect_workspace(
    path: str | Path,
    *,
    json_output: bool = False,
    stream: TextIO | None = None,
) -> PackageCatalog:
    """只读编译 workspace 并输出 Catalog 或人类摘要。"""

    output = stream or sys.stdout
    catalog = compile_package_source(WorkspaceSource(path))
    if json_output:
        output.write(catalog.to_canonical_bytes().decode("utf-8"))
        output.write("\n")
        return catalog

    output.write(
        f"{catalog.distribution.name}@{catalog.distribution.version} "
        f"({catalog.namespace})\n"
    )
    output.write(
        "definitions: "
        f"{len(catalog.definitions.devices)} devices, "
        f"{len(catalog.definitions.resources)} resources, "
        f"{len(catalog.definitions.workflows)} workflows\n"
    )
    output.write(f"assets: {len(catalog.assets)}\n")
    output.write(f"catalog_digest: {catalog.catalog_digest}\n")
    return catalog


def install_and_validate(
    spec: str,
    *,
    distribution: str = "",
    installer: Callable[[str], str] | None = None,
) -> PackageCatalog:
    """Install exactly one requested spec, then validate an explicit distribution."""

    spec = spec.strip()
    if not spec:
        raise PackageCommandError("install spec 不能为空")
    distribution_name = distribution.strip() or _distribution_from_spec(spec)
    if not distribution_name:
        raise PackageCommandError(
            "无法从 install spec 唯一确定 distribution；请提供 --distribution NAME"
        )
    (installer or _run_pip_install)(spec)
    try:
        return compile_package_source(InstalledDistributionSource(distribution_name))
    except PackageCompileError as exc:
        raise PackageCommandError(
            f"安装可能已完成，但不是可用的 Uni-Lab package source: {exc}"
        ) from exc


def _distribution_from_spec(spec: str) -> str:
    value = spec.strip()
    path_value = value.removeprefix("file:")
    path = Path(path_value).expanduser()
    if path.exists():
        root = path if path.is_dir() else path.parent
        try:
            document = tomllib.loads(
                (root / "pyproject.toml").read_text(encoding="utf-8")
            )
            name = document.get("project", {}).get("name")
            return str(name).strip() if isinstance(name, str) else ""
        except (OSError, tomllib.TOMLDecodeError):
            return ""
    if value.startswith(("git+", "http://", "https://")):
        return ""
    match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", value)
    return match.group(1) if match else ""


def _run_pip_install(spec: str) -> str:
    from unilabos.utils.environment_check import (
        _install_command,
        _installer_candidates,
        _is_chinese_locale,
    )

    last_error = ""
    for installer in _installer_candidates():
        command = _install_command(
            installer,
            spec,
            False,
            _is_chinese_locale(),
        )
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            last_error = str(exc)
            continue
        if result.returncode == 0:
            return "uv" if installer == "uv" else "pip"
        last_error = (result.stderr or result.stdout or "").strip()
    raise PackageCommandError(f"安装失败: {spec}\n{last_error}")


__all__ = [
    "PackageCommandError",
    "inspect_workspace",
    "install_and_validate",
    "package_command_needs_http",
    "register_package_subcommands",
    "run_package_command",
]
