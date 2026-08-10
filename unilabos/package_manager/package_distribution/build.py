"""软件包构建（Package Build）的暂存、标准 wheel 与来源自审计。"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ..package_catalog import PackageCatalog, WorkspaceSource
from ..package_catalog.project_metadata import (
    parse_project_metadata,
    project_to_legacy_dict,
)
from .errors import PackageBuildError
from .inspection import CatalogCompiler
from .legacy_projection import build_package_info, build_resources_from_registry
from .wheel import artifact_digest, audit_package_wheel
from .workspace_manifest import build_workspace_manifest_member

# 暂存排除集合禁止把本地缓存、运行数据和旧构建产物带入发布 wheel。
_STAGING_EXCLUDES = frozenset(
    {
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".unilabos",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "unilabos_data",
        "venv",
    }
)
@dataclass(frozen=True, slots=True)
class PackageBuildArtifact:
    """一次通过来源重编译审计的完整软件包发布代际。"""

    # ``wheel`` 是可以交给安装器或云端广场的唯一二进制发布归档。
    wheel: Path
    # ``artifact_digest`` 是 wheel 完整字节的稳定 SHA-256 身份。
    artifact_digest: str
    # ``catalog`` 是从暂存源码编译并由 wheel 来源重新证明的包目录。
    catalog: PackageCatalog
    # 三个路径是与同一 wheel 和目录绑定的可读发布投影。
    catalog_path: Path
    package_info_path: Path
    resources_path: Path
    # ``package_info`` 与 ``resources`` 是现有云端广场接口消费的兼容 DTO。
    package_info: Mapping[str, Any]
    resources: tuple[Mapping[str, Any], ...]

    def publication_input(self) -> dict[str, Any]:
        """生成云端发布 Adapter 消费的独立可变输入。

        参数：无。
        返回：以已审计 wheel 作为 ``archive_path`` 的发布字典；兼容 DTO 均复制，
        调用者修改返回值不会改变构建产物对象。
        异常：无；字段已在构建成功前完成验证。
        """

        # ``package_info`` 是本次发布独占的软件包身份容器。
        package_info = dict(self.package_info)
        # ``resources`` 是本次发布独占的模板投影集合，并统一引用同一包身份。
        resources = [dict(item) for item in self.resources]
        for resource in resources:
            resource["package_info"] = package_info
        return {
            "archive_path": str(self.wheel),
            "package_info": package_info,
            "resources": resources,
        }


def build_workspace_package(
    workspace: str | Path,
    output_dir: str | Path,
    *,
    compile_catalog: CatalogCompiler,
) -> PackageBuildArtifact:
    """在临时暂存树构建 wheel，并从 wheel 来源重编译后发布产物。

    参数：``workspace`` 是作者显式选择的软件包工作区（Package Workspace）；
    ``output_dir`` 是最终产物目录；``compile_catalog`` 是组合根注入的唯一包目录
    （PackageCatalog）编译 Interface。
    返回：包含已审计 wheel、摘要、目录和云端兼容投影的不可变构建产物。
    异常：路径、标准构建、wheel 安全、闭包或 parity 无效时抛出
    ``PackageBuildError``/目录编译异常；失败不会把候选 wheel 发布到目标目录。
    """

    if not callable(compile_catalog):
        raise TypeError("compile_catalog 必须可调用")
    # ``workspace_root`` 是本次构建唯一授权的作者源码边界。
    workspace_root = Path(workspace).expanduser().resolve()
    if not workspace_root.is_dir():
        raise PackageBuildError(f"软件包工作区不存在：{workspace_root}")
    # ``artifact_root`` 是审计通过后才写入的最终发布目录。
    artifact_root = Path(output_dir).expanduser().resolve()
    artifact_root.mkdir(parents=True, exist_ok=True)

    # ``artifact_root`` 已规范化；把暂存代际放在该边界内，避免 macOS 的
    # ``/var`` → ``/private/var`` 系统别名被 WorkspaceSource 误判为调用者链接。
    with tempfile.TemporaryDirectory(
        prefix="unilab-package-build-",
        dir=artifact_root,
    ) as temporary:
        # ``temporary_root`` 隔离暂存源码、标准构建输出和 wheel 审计工作区。
        temporary_root = Path(temporary)
        staging_root = temporary_root / "workspace"
        wheel_output = temporary_root / "wheel"
        _copy_workspace(workspace_root, staging_root)
        wheel_output.mkdir()

        # ``staging_source`` 是目录编译和构建共同观察的同一固定暂存来源。
        staging_source = WorkspaceSource(staging_root)
        # ``catalog`` 是本轮构建唯一规范包目录，后续投影和审计不得重新解释源码。
        catalog = compile_catalog(staging_source)
        generated_members = _write_generated_catalog(staging_source, catalog)
        # ``candidate_wheel`` 尚未发布，只有完整自审计通过后才复制到目标目录。
        candidate_wheel = _build_standard_wheel(staging_root, wheel_output)
        manifest_name, manifest_payload = build_workspace_manifest_member(
            candidate_wheel,
            staging_source,
            catalog,
            generated_members=generated_members,
        )
        generated_members[manifest_name] = manifest_payload
        _inject_wheel_members(candidate_wheel, generated_members)
        # ``candidate_digest`` 绑定重写 RECORD 后的最终候选 wheel 字节。
        candidate_digest = artifact_digest(candidate_wheel)
        audit_package_wheel(
            candidate_wheel,
            catalog,
            expected_digest=candidate_digest,
            compile_catalog=compile_catalog,
        )
        # ``target_wheel`` 是本版本正式就绪标志；准备新投影前必须先隐藏旧标志。
        target_wheel = artifact_root / candidate_wheel.name
        if target_wheel.exists() or target_wheel.is_symlink():
            # ``previous_marker`` 只在临时构建代际中保留旧标志，失败后不再误报就绪。
            previous_marker = temporary_root / "previous-wheel" / target_wheel.name
            previous_marker.parent.mkdir()
            target_wheel.replace(previous_marker)
        # ``generation_root`` 是全部发布投影先完整准备的临时代际边界。
        generation_root = temporary_root / "generation"
        generation_root.mkdir()
        # 以下投影只从已通过 wheel 来源重编译的同一目录与摘要生成。
        package_info, resources = _publication_projections(
            catalog,
            staging_project_bytes=generated_members[
                f"{catalog.import_package}/_generated/pyproject.toml"
            ],
            artifact_digest=candidate_digest,
        )
        prepared_catalog = generation_root / "package.catalog.json"
        prepared_package_info = generation_root / "package_info.json"
        prepared_resources = generation_root / "resources.json"
        _write_output_file(prepared_catalog, catalog.to_canonical_bytes())
        _write_output_file(
            prepared_package_info,
            json.dumps(package_info, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        _write_output_file(
            prepared_resources,
            json.dumps(resources, ensure_ascii=False, indent=2).encode("utf-8"),
        )

        # 三个正式投影必须先就绪，wheel 最后提交并作为本代际就绪标志。
        catalog_path = artifact_root / prepared_catalog.name
        package_info_path = artifact_root / prepared_package_info.name
        resources_path = artifact_root / prepared_resources.name
        _publish_file(prepared_catalog, catalog_path)
        _publish_file(prepared_package_info, package_info_path)
        _publish_file(prepared_resources, resources_path)
        # 全部投影成功后才提交新的 ``target_wheel``。
        _publish_file(candidate_wheel, target_wheel)

    return PackageBuildArtifact(
        wheel=target_wheel,
        artifact_digest=candidate_digest,
        catalog=catalog,
        catalog_path=catalog_path,
        package_info_path=package_info_path,
        resources_path=resources_path,
        package_info=package_info,
        resources=tuple(resources),
    )


def _copy_workspace(source: Path, target: Path) -> None:
    """复制作者工作区到隔离暂存树并排除本地运行产物。

    参数：``source`` 是已验证源码根；``target`` 是尚不存在的临时目录。
    返回：无；保留符号链接身份供后续安全编译关闭式拒绝。
    异常：读取或复制失败时传播文件系统异常。
    """

    def ignore(_directory: str, names: list[str]) -> set[str]:
        """返回当前目录中禁止进入暂存树的成员名。

        参数：``_directory`` 是复制库提供但本规则不需要的目录；``names`` 是候选名。
        返回：与固定排除集合相交的名字。
        异常：无。
        """

        return {name for name in names if name in _STAGING_EXCLUDES}

    shutil.copytree(source, target, ignore=ignore, symlinks=True)


def _write_generated_catalog(
    source: WorkspaceSource,
    catalog: PackageCatalog,
) -> dict[str, bytes]:
    """把规范目录及重编译声明嵌入临时暂存树。

    参数：``source`` 是暂存来源；``catalog`` 是同一来源编译的规范目录。
    返回：必须在最终 wheel 中保持精确字节的逻辑成员映射。
    异常：暂存目录不可写或根声明不可读时传播原始异常。
    """

    # ``generated_root`` 是包内唯一的构建生成事实目录，不写回作者源码。
    generated_root = source.root / catalog.import_package / "_generated"
    generated_root.mkdir(parents=True, exist_ok=True)
    # ``generated_members`` 同时用于暂存写入和 wheel RECORD 安全注入。
    project_bytes = source.read_bytes("pyproject.toml")
    generated_members = {
        f"{catalog.import_package}/_generated/package.catalog.json": (
            catalog.to_canonical_bytes()
        ),
        f"{catalog.import_package}/_generated/pyproject.toml": project_bytes,
    }
    if source.has_file("package.yaml"):
        generated_members[f"{catalog.import_package}/_generated/package.yaml"] = (
            source.read_bytes("package.yaml")
        )
    # ``project`` 给出 clean-wheel 重编译仍需存在的显式工作区启动输入。
    project = parse_project_metadata(project_bytes)
    for startup_file in (project.startup_graph, project.startup_config):
        if startup_file is None:
            continue
        # ``logical_startup_file`` 把绝对或相对声明统一约束回暂存来源边界。
        selected_startup_file = Path(startup_file).expanduser()
        if selected_startup_file.is_absolute():
            try:
                logical_startup_file = selected_startup_file.relative_to(
                    source.root
                ).as_posix()
            except ValueError as error:
                raise PackageBuildError("工作区启动文件必须位于软件包根内") from error
        else:
            logical_startup_file = PurePosixPath(startup_file).as_posix()
        generated_members[
            f"{catalog.import_package}/_generated/workspace/{logical_startup_file}"
        ] = source.read_bytes(logical_startup_file)
    for logical_path, payload in generated_members.items():
        # ``generated_file`` 始终落在已验证暂存包的 ``_generated`` 内。
        generated_file = source.root.joinpath(*PurePosixPath(logical_path).parts)
        generated_file.parent.mkdir(parents=True, exist_ok=True)
        generated_file.write_bytes(payload)
    return generated_members


def _build_standard_wheel(staging_root: Path, wheel_output: Path) -> Path:
    """通过当前解释器的标准 pip wheel 前端构建一个无依赖 wheel。

    参数：``staging_root`` 是已经嵌入目录的暂存源码；``wheel_output`` 是临时输出。
    返回：构建产生的唯一 wheel 路径。
    异常：构建工具失败或产生零个/多个 wheel 时抛出 ``PackageBuildError``。
    """

    # ``command`` 使用项目声明的标准构建后端，但不解析或下载运行依赖。
    command = [
        sys.executable,
        "-m",
        "pip",
        "wheel",
        "--no-deps",
        "--no-build-isolation",
        "--wheel-dir",
        str(wheel_output),
        str(staging_root),
    ]
    # ``result`` 收集完整诊断，避免子进程输出混入结构化 CLI 结果。
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise PackageBuildError(f"标准 wheel 构建失败：{detail}")
    # ``wheels`` 必须只有当前工作区的一个构建结果。
    wheels = tuple(sorted(wheel_output.glob("*.whl")))
    if len(wheels) != 1:
        raise PackageBuildError(
            f"标准构建必须产生一个 wheel，实际产生 {len(wheels)} 个"
        )
    return wheels[0]


def _inject_wheel_members(wheel: Path, injected: Mapping[str, bytes]) -> None:
    """确保生成事实进入 wheel，并重建完整标准 RECORD。

    参数：``wheel`` 是标准构建候选；``injected`` 是必须精确嵌入的逻辑成员。
    返回：无；原子替换同一路径 wheel。
    异常：ZIP 或 RECORD 结构无效时抛出 ``PackageBuildError``。
    """

    try:
        with zipfile.ZipFile(wheel) as archive:
            # ``original_infos`` 保留非签名成员元数据，生成成员与 RECORD 将被替换。
            original_infos = {
                item.filename: item
                for item in archive.infolist()
                if not item.is_dir()
                and not item.filename.endswith(("/RECORD.jws", "/RECORD.p7s"))
            }
            # ``members`` 是重写后 wheel 的完整普通文件内容。
            members = {
                name: archive.read(item) for name, item in original_infos.items()
            }
    except zipfile.BadZipFile as error:
        raise PackageBuildError("标准构建产物不是合法 wheel ZIP") from error
    members.update(injected)
    # ``record_names`` 必须唯一，避免更新错误发行元数据。
    record_names = [name for name in members if name.endswith(".dist-info/RECORD")]
    if len(record_names) != 1:
        raise PackageBuildError(f"wheel RECORD 数量不是 1：{len(record_names)}")
    record_name = record_names[0]
    members[record_name] = _wheel_record(members, record_name)
    replacement = wheel.with_suffix(".rewrite.whl")
    with zipfile.ZipFile(replacement, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in sorted(members.items()):
            # ``original_info`` 尽可能保留标准后端生成的权限和时间元数据。
            original_info = original_infos.get(name)
            if original_info is None or name in injected or name == record_name:
                archive.writestr(name, payload)
            else:
                archive.writestr(original_info, payload)
    replacement.replace(wheel)


def _wheel_record(members: Mapping[str, bytes], record_name: str) -> bytes:
    """生成与全部 wheel 普通成员一致的标准 RECORD 字节。

    参数：``members`` 是成员内容映射；``record_name`` 是唯一 RECORD 身份。
    返回：CSV 编码记录，其中 RECORD 自身摘要与大小留空。
    异常：成员名不能由 CSV 编码时传播标准库异常。
    """

    # ``stream`` 与 ``writer`` 共同生成固定换行的标准记录内容。
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    for name, payload in sorted(members.items()):
        if name == record_name:
            writer.writerow((name, "", ""))
            continue
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).decode()
        writer.writerow((name, f"sha256={digest.rstrip('=')}", len(payload)))
    return stream.getvalue().encode("utf-8")


def _publication_projections(
    catalog: PackageCatalog,
    *,
    staging_project_bytes: bytes,
    artifact_digest: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """从已审计目录和 wheel 摘要生成现有云端广场兼容投影。

    参数：``catalog`` 是已证明的规范目录；``staging_project_bytes`` 是同次项目声明；
    ``artifact_digest`` 是最终 wheel 摘要。
    返回：软件包信息和资源模板 DTO 列表。
    异常：项目或注册条目不能形成既有接口 DTO 时传播原始校验异常。
    """

    # ``project`` 是统一项目元数据到遗留发布字段的唯一投影。
    project = project_to_legacy_dict(parse_project_metadata(staging_project_bytes))
    # ``package_info`` 把云端安装身份绑定到已审计 wheel，而不是源码 tar。
    package_info = build_package_info(
        project,
        catalog.namespace,
        artifact_digest,
    )
    package_info["artifact_digest"] = artifact_digest
    package_info["catalog_digest"] = catalog.catalog_digest
    package_info["content_digest"] = catalog.content_digest
    # ``catalog_document`` 解冻注册条目，避免兼容投影误把只读映射当成缺失字典。
    catalog_document = catalog.to_dict()
    registry_entries: dict[str, dict[str, Any]] = {}
    for definition_kind in ("devices", "resources"):
        for item in catalog_document["definitions"][definition_kind]:
            # ``registry_entry`` 重复保存遗留 Backend 会持久化的源码身份证据；
            # 规范定义身份仍由 PackageCatalog 权威拥有。
            registry_entry = dict(item["details"]["registry_entry"])
            registry_entry["id"] = item["id"]
            registry_entry["source_fqid"] = f"{item['module']}:{item['symbol']}"
            registry_entry["content_hash"] = item["content_hash"]
            registry_entry["package_definition_fqid"] = item["fqid"]
            registry_entries[item["id"]] = registry_entry
    resources = build_resources_from_registry(registry_entries, package_info)
    return package_info, resources


def _publish_file(source: Path, target: Path) -> None:
    """在目标目录内先复制临时文件，再原子替换正式产物。

    参数：``source`` 是已审计候选；``target`` 是最终路径。
    返回：无。
    异常：复制、同步或替换失败时传播原始文件系统异常。
    """

    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
        delete=False,
    ) as temporary_file:
        # ``temporary_path`` 与目标同文件系统，保证最后替换操作原子。
        temporary_path = Path(temporary_file.name)
        with source.open("rb") as source_file:
            shutil.copyfileobj(source_file, temporary_file)
    temporary_path.replace(target)


def _write_output_file(target: Path, payload: bytes) -> None:
    """在目标目录原子写入一个构建投影文件。

    参数：``target`` 是最终文件；``payload`` 是完整 UTF-8 JSON 或规范目录字节。
    返回：无。
    异常：目标目录不可写时传播原始文件系统异常。
    """

    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
        delete=False,
    ) as temporary_file:
        temporary_file.write(payload)
        # ``temporary_path`` 与目标同文件系统，保证成功结果不出现半写文件。
        temporary_path = Path(temporary_file.name)
    temporary_path.replace(target)


__all__ = [
    "PackageBuildArtifact",
    "PackageBuildError",
    "audit_package_wheel",
    "build_workspace_package",
]
