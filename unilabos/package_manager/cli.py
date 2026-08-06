"""Package-manager argparse registration, command dispatch, and presentation."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

import tomllib

from .catalog import PackageCatalog, PackageCompileError
from .community import RequestsCommunityDownloadAdapter
from .compiler import compile_package_source
from .distribution import (
    BuildArtifact,
    PackageDistributionError,
    build_workspace_wheel,
)
from .device_package import download_device_package
from .device_provisioning import (
    remove_device_instance,
    restore_device_graph,
    stage_device_instance,
    update_device_instance,
)
from .publication import HttpClientPublicationAdapter, publish_build
from .sources import InstalledDistributionSource, WorkspaceSource


class PackageCommandError(RuntimeError):
    """A package command failed with a user-actionable message."""


def register_package_subcommands(subparsers: Any) -> None:
    """向应用根 parser 注册设备包管理命令，不启动本地设备 Runtime。"""

    package_parser = subparsers.add_parser(
        "package",
        aliases=["pkg"],
        help="Uni-Lab package inspect / build / upload / download / device graph",
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
            command.add_argument(
                "--json",
                dest="package_json",
                action="store_true",
                help="Output the final publication identity as JSON",
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

    download = actions.add_parser(
        "download",
        help="Download and verify one cloud device package into managed cache",
    )
    download.add_argument("--template-uuid", required=True)
    download.add_argument("--definition-fqid", required=True)
    download.add_argument("--artifact-digest", required=True)
    download.add_argument(
        "--json",
        dest="package_json",
        action="store_true",
        help="Output the package cache result and device configuration schema as JSON",
    )

    add_device = actions.add_parser(
        "add-device",
        help="Validate a cached device package and atomically add one Graph instance",
    )
    _add_device_instance_arguments(add_device, require_instance_uuid=False)

    update_device = actions.add_parser(
        "update-device",
        help="Atomically update one existing Graph device configuration",
    )
    _add_device_instance_arguments(update_device, require_instance_uuid=True)

    remove_device = actions.add_parser(
        "remove-device",
        help="Atomically remove one local device instance from a Graph",
    )
    remove_device.add_argument("--graph", required=True)
    remove_device.add_argument("--instance-id", required=True)
    remove_device.add_argument("--instance-uuid", default=None)
    remove_device.add_argument("--json", dest="package_json", action="store_true")

    restore_graph = actions.add_parser(
        "restore-graph",
        help="Atomically restore a trusted device Graph backup",
    )
    restore_graph.add_argument("--graph", required=True)
    restore_graph.add_argument("--backup", required=True)
    restore_graph.add_argument("--json", dest="package_json", action="store_true")


def _add_device_instance_arguments(
    parser: argparse.ArgumentParser,
    *,
    require_instance_uuid: bool,
) -> None:
    """为新增/更新设备实例命令注册共享且封闭的参数集合。

    参数 ``parser`` 是目标子命令解析器，``require_instance_uuid`` 决定更新流程
    是否必须携带稳定实例 UUID。函数无返回值；配置只允许从 stdin 读取，避免
    敏感或复杂 JSON 进入 argv。
    """

    parser.add_argument("--cache-key", required=True)
    parser.add_argument("--definition-fqid", required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument(
        "--instance-uuid",
        required=require_instance_uuid,
        default=None,
    )
    parser.add_argument("--graph", required=True)
    parser.add_argument("--config-stdin", action="store_true", required=True)
    parser.add_argument("--json", dest="package_json", action="store_true")


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
    download_port: Any = None,
    working_dir: str | Path | None = None,
    remote_addr: str | None = None,
    stream: TextIO | None = None,
    input_stream: TextIO | None = None,
) -> Any:
    """执行一个 package 子命令并把最终结果写到调用方提供的输出流。

    ``args`` 是 argparse 投影；可注入 ``http_client``/``download_port``；
    ``working_dir`` 与 ``remote_addr`` 覆盖运行环境；``stream`` 接收最终输出；
    ``input_stream`` 仅承载设备配置 JSON。返回具体命令结果；合同、构建、下载、
    配置或设备图错误统一转换为 :class:`PackageCommandError`。
    """

    output = stream or sys.stdout
    action = str(args.get("package_action") or "")
    if not action:
        raise PackageCommandError(
            "缺少 package 子动作，请使用 inspect|build|upload|download|add-device|"
            "update-device|remove-device|restore-graph|install"
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
            if bool(args.get("package_json") or args.get("json")):
                output.write(
                    json.dumps(
                        {
                            "status": "published",
                            "distribution": artifact.catalog.distribution.name,
                            "version": artifact.catalog.distribution.version,
                            "namespace": artifact.catalog.namespace,
                            "catalog_digest": artifact.catalog.catalog_digest,
                            "artifact_digest": artifact.artifact_digest,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                )
                output.write("\n")
            else:
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
        if action == "download":
            result = download_device_package(
                template_uuid=str(args.get("template_uuid") or ""),
                definition_fqid=str(args.get("definition_fqid") or ""),
                artifact_digest=str(args.get("artifact_digest") or ""),
                backend_base_url=(
                    remote_addr or _download_remote_addr(args.get("addr"))
                ),
                working_dir=str(
                    working_dir or _download_working_dir(args.get("working_dir"))
                ),
                port=download_port or RequestsCommunityDownloadAdapter(),
            )
            if bool(args.get("package_json") or args.get("json")):
                output.write(
                    json.dumps(
                        result.to_dict(),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                )
                output.write("\n")
            else:
                output.write(
                    f"cached {result.distribution}@{result.version} "
                    f"({result.definition_fqid})\n"
                )
                output.write(f"cache_key: {result.cache_key}\n")
                output.write(f"catalog_digest: {result.catalog_digest}\n")
            return result
        if action in {"add-device", "update-device"}:
            configuration_input = input_stream or sys.stdin
            try:
                payload = json.load(configuration_input)
            except (OSError, json.JSONDecodeError) as exc:
                raise PackageCommandError(f"设备配置 stdin 不是合法 JSON: {exc}") from exc
            if not isinstance(payload, dict):
                raise PackageCommandError("设备配置 stdin 根必须是 JSON object")
            display_name = str(payload.get("display_name") or "")
            configuration = payload.get("configuration")
            if not isinstance(configuration, dict):
                raise PackageCommandError("设备配置 stdin 缺少 configuration object")
            common = {
                "graph_path": str(args.get("graph") or ""),
                "working_dir": str(
                    working_dir or _download_working_dir(args.get("working_dir"))
                ),
                "cache_key": str(args.get("cache_key") or ""),
                "definition_fqid": str(args.get("definition_fqid") or ""),
                "instance_id": str(args.get("instance_id") or ""),
                "instance_uuid": (
                    str(args["instance_uuid"])
                    if args.get("instance_uuid")
                    else None
                ),
                "display_name": display_name,
                "configuration": configuration,
            }
            result = (
                update_device_instance(**common)
                if action == "update-device"
                else stage_device_instance(**common)
            )
            _write_graph_mutation_result(result, output, args)
            return result
        if action == "remove-device":
            result = remove_device_instance(
                graph_path=str(args.get("graph") or ""),
                instance_id=str(args.get("instance_id") or ""),
                instance_uuid=(
                    str(args["instance_uuid"])
                    if args.get("instance_uuid")
                    else None
                ),
            )
            _write_graph_mutation_result(result, output, args)
            return result
        if action == "restore-graph":
            result = restore_device_graph(
                graph_path=str(args.get("graph") or ""),
                backup_path=str(args.get("backup") or ""),
            )
            _write_graph_mutation_result(result, output, args)
            return result
        raise PackageCommandError(f"未知 package 子动作: {action}")
    except (PackageCompileError, PackageDistributionError, RuntimeError) as exc:
        if isinstance(exc, PackageCommandError):
            raise
        raise PackageCommandError(str(exc)) from exc


def _write_graph_mutation_result(
    result: Any,
    output: TextIO,
    args: dict[str, Any],
) -> None:
    """按 ``--json`` 选择机器合同或简短中文设备图变更摘要。

    参数 ``result`` 是设备图变更结果，``output`` 是目标输出流，``args`` 是
    CLI 参数投影。函数无返回值，最终 JSON 始终单行输出。
    """

    if bool(args.get("package_json") or args.get("json")):
        output.write(
            json.dumps(
                result.to_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        output.write("\n")
    else:
        output.write(
            f"{result.status} {result.instance_id or 'device-graph'} "
            f"({result.graph_fingerprint})\n"
        )


def _download_working_dir(raw_value: Any) -> Path:
    """按现有显式 working_dir 规则解析非交互设备包缓存目录。"""

    raw = str(raw_value or "").strip()
    if not raw:
        return (Path.cwd() / "unilabos_data").resolve()
    root = Path(raw).expanduser().resolve()
    nested = root / "unilabos_data"
    if root.name != "unilabos_data" and nested.is_dir():
        return nested.resolve()
    return root


def _download_remote_addr(raw_value: Any) -> str:
    """解析 CLI 既有 test/uat/local 地址别名，不加载 local_config.py。"""

    value = str(raw_value or "").strip()
    aliases = {
        "test": "https://leap-lab.test.bohrium.com/api/v1",
        "uat": "https://leap-lab.uat.bohrium.com/api/v1",
        "local": "http://127.0.0.1:48197/api/v1",
    }
    return aliases.get(value, value)


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
