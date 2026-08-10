"""不可信 wheel 的安全读取、来源重编译与包目录（PackageCatalog）证明。"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import stat
import tempfile
import zipfile
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath

from ..package_catalog import CachedArchiveSource, PackageCatalog
from .errors import PackageBuildError
from .workspace_manifest import validate_workspace_manifest

MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_MEMBER_BYTES = 1024 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 50_000
MAX_COMPRESSION_RATIO = 1_000


def artifact_digest(path: str | Path) -> str:
    """分块计算归档的带前缀 SHA-256 摘要。

    参数：``path`` 是已完成写入的普通文件。
    返回：``sha256:<hex>`` 格式摘要。
    异常：文件不可读时传播原始 IO 异常。
    """

    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def read_verified_wheel_members(
    wheel: str | Path,
    *,
    expected_digest: str,
) -> dict[str, bytes]:
    """验证 wheel 文件身份、ZIP 资源边界与标准 RECORD 后读取成员。

    参数：``wheel`` 是调用者选择的 wheel；``expected_digest`` 是远端或构建阶段
    固定的 Artifact digest。
    返回：按安全 POSIX 成员名索引的普通文件字节。
    异常：路径、摘要、ZIP、成员或 RECORD 无效时抛出 ``PackageBuildError``。
    """

    wheel_path = _verified_wheel_path(wheel, expected_digest)
    try:
        with zipfile.ZipFile(wheel_path) as archive:
            members = _validated_wheel_members(archive)
    except zipfile.BadZipFile as error:
        raise PackageBuildError("wheel 不是合法 ZIP 归档") from error
    _verify_wheel_record(members)
    return members


def read_embedded_package_catalog(
    wheel: str | Path,
    *,
    expected_digest: str,
) -> PackageCatalog:
    """从完整性已验证的 wheel 读取唯一规范包目录。

    参数：``wheel`` 是候选归档；``expected_digest`` 是 Artifact digest。
    返回：通过严格规范 JSON 解码和自身摘要复算的 ``PackageCatalog``。
    异常：归档或内嵌目录缺失、重复、非规范或摘要错误时抛出
    ``PackageBuildError``。
    """

    members = read_verified_wheel_members(wheel, expected_digest=expected_digest)
    catalog_names = [
        name
        for name in members
        if name.endswith("/_generated/package.catalog.json")
        and not name.split("/", 1)[0].endswith((".dist-info", ".data"))
    ]
    if len(catalog_names) != 1:
        raise PackageBuildError(
            f"wheel 内嵌包目录数量不是 1：{len(catalog_names)}"
        )
    try:
        return PackageCatalog.from_canonical_bytes(members[catalog_names[0]])
    except (TypeError, ValueError) as error:
        raise PackageBuildError("wheel 内嵌包目录不是有效规范 JSON") from error


def audit_package_wheel(
    wheel: str | Path,
    catalog: PackageCatalog,
    *,
    expected_digest: str,
    compile_catalog: Callable[[CachedArchiveSource], PackageCatalog],
) -> None:
    """验证 wheel 摘要、闭包、开发清单及实际源码目录 parity。

    参数：``wheel`` 是候选标准 wheel；``catalog`` 是预期规范目录；
    ``expected_digest`` 是 Artifact digest；``compile_catalog`` 是唯一目录编译
    Interface。
    返回：无；全部内容不变量成立时正常返回。
    异常：归档、目录、开发清单或来源重编译不一致时抛出
    ``PackageBuildError``。
    """

    if not callable(compile_catalog):
        raise TypeError("compile_catalog 必须可调用")
    if not isinstance(catalog, PackageCatalog):
        raise TypeError("catalog 必须是 PackageCatalog")
    members = read_verified_wheel_members(wheel, expected_digest=expected_digest)
    _verify_payload_and_closure(members, catalog)
    embedded_name = f"{catalog.import_package}/_generated/package.catalog.json"
    if members[embedded_name] != catalog.to_canonical_bytes():
        raise PackageBuildError("wheel 内嵌包目录与预期目录不一致")
    validate_workspace_manifest(members, catalog)

    audit_parent = Path(tempfile.gettempdir()).resolve()
    with tempfile.TemporaryDirectory(
        prefix="unilab-package-audit-",
        dir=audit_parent,
    ) as temporary:
        audit_root = Path(temporary) / "workspace"
        _reconstruct_workspace(members, catalog, audit_root)
        source = CachedArchiveSource._from_verified_workspace(
            audit_root,
            archive_path=wheel,
            artifact_digest=expected_digest,
        )
        audited_catalog = compile_catalog(source)
    if audited_catalog.to_canonical_bytes() != catalog.to_canonical_bytes():
        raise PackageBuildError("wheel 来源重编译目录与预期目录不一致")


def verify_downloaded_package_wheel(
    wheel: str | Path,
    *,
    expected_digest: str,
    compile_catalog: Callable[[CachedArchiveSource], PackageCatalog],
) -> PackageCatalog:
    """从下载 wheel 读取目录并用真实归档来源重新证明。

    参数：``wheel`` 是下载临时文件；``expected_digest`` 是 Backend 描述的摘要；
    ``compile_catalog`` 是与工作区检查共用的编译 Interface。
    返回：内嵌与重编译完全一致的 ``PackageCatalog``。
    异常：任一摘要、结构或 parity 失败时抛出 ``PackageBuildError``。
    """

    catalog = read_embedded_package_catalog(wheel, expected_digest=expected_digest)
    audit_package_wheel(
        wheel,
        catalog,
        expected_digest=expected_digest,
        compile_catalog=compile_catalog,
    )
    return catalog


def _verified_wheel_path(wheel: str | Path, expected_digest: str) -> Path:
    """验证调用者 wheel 路径并返回规范普通文件位置。

    参数：``wheel`` 是未解析路径；``expected_digest`` 是期望摘要。
    返回：无符号链接的规范绝对路径。
    异常：路径、大小或摘要无效时抛出 ``PackageBuildError``。
    """

    selected = Path(wheel).expanduser()
    try:
        metadata = selected.lstat()
    except OSError as error:
        raise PackageBuildError(f"wheel 不存在或不可访问：{selected}") from error
    if stat.S_ISLNK(metadata.st_mode) or selected.is_symlink():
        raise PackageBuildError(f"wheel 路径不得是符号链接：{selected}")
    resolved = selected.resolve()
    if not resolved.is_file():
        raise PackageBuildError(f"wheel 不存在或不是普通文件：{selected}")
    if resolved.stat().st_size > MAX_ARCHIVE_BYTES:
        raise PackageBuildError("wheel 超过归档大小上限")
    actual = artifact_digest(resolved)
    if not expected_digest or actual != expected_digest:
        raise PackageBuildError(
            f"wheel 摘要不匹配：{actual} != {expected_digest or '-'}"
        )
    return resolved


def _validated_wheel_members(archive: zipfile.ZipFile) -> dict[str, bytes]:
    """关闭式验证 wheel 成员安全并读取普通文件内容。

    参数：``archive`` 是已打开的候选 wheel。
    返回：普通文件成员映射。
    异常：重复、加密、链接、路径或压缩资源超限时抛出
    ``PackageBuildError``。
    """

    infos = tuple(archive.infolist())
    if len(infos) > MAX_ARCHIVE_MEMBERS:
        raise PackageBuildError("wheel 成员数量超过上限")
    names: set[str] = set()
    total_size = 0
    members: dict[str, bytes] = {}
    for item in infos:
        _safe_member_name(item.filename.rstrip("/"))
        if item.filename in names:
            raise PackageBuildError(f"wheel 包含重复成员：{item.filename}")
        names.add(item.filename)
        mode = item.external_attr >> 16
        if item.flag_bits & 0x1:
            raise PackageBuildError(f"wheel 包含加密成员：{item.filename}")
        if stat.S_ISLNK(mode):
            raise PackageBuildError(f"wheel 包含符号链接成员：{item.filename}")
        if item.file_size > MAX_MEMBER_BYTES:
            raise PackageBuildError(f"wheel 成员超过大小上限：{item.filename}")
        total_size += item.file_size
        if total_size > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise PackageBuildError("wheel 解压后总大小超过上限")
        if (item.file_size > 0 and item.compress_size == 0) or (
            item.compress_size > 0
            and item.file_size / item.compress_size > MAX_COMPRESSION_RATIO
        ):
            raise PackageBuildError(f"wheel 成员压缩比超过上限：{item.filename}")
        if not item.is_dir():
            members[item.filename] = archive.read(item)
    return members


def _safe_member_name(member_name: str) -> PurePosixPath:
    """验证 wheel 成员名是规范 POSIX 相对路径。

    参数：``member_name`` 是 ZIP 中声明的逻辑路径。
    返回：完成检查的 ``PurePosixPath``。
    异常：空、绝对、反斜杠或父目录语义抛出 ``PackageBuildError``。
    """

    if not member_name or "\\" in member_name:
        raise PackageBuildError("wheel 成员路径必须是非空 POSIX 相对路径")
    logical = PurePosixPath(member_name)
    if logical.is_absolute() or any(
        part in {"", ".", ".."} for part in logical.parts
    ):
        raise PackageBuildError(f"wheel 成员路径非法：{member_name}")
    return logical


def _verify_wheel_record(members: Mapping[str, bytes]) -> None:
    """验证 wheel RECORD 完整覆盖成员并匹配摘要与大小。

    参数：``members`` 是安全读取后的普通成员。
    返回：无。
    异常：RECORD 缺失、重复或不匹配时抛出 ``PackageBuildError``。
    """

    record_names = [name for name in members if name.endswith(".dist-info/RECORD")]
    if len(record_names) != 1:
        raise PackageBuildError(f"wheel RECORD 数量不是 1：{len(record_names)}")
    record_name = record_names[0]
    try:
        parsed_rows = list(
            csv.reader(io.StringIO(members[record_name].decode("utf-8"), newline=""))
        )
    except (UnicodeError, csv.Error) as error:
        raise PackageBuildError("wheel RECORD 不是合法 UTF-8 CSV") from error
    if any(len(row) != 3 for row in parsed_rows if row):
        raise PackageBuildError("wheel RECORD 字段数量无效")
    rows = {row[0]: row for row in parsed_rows if row}
    if len(rows) != len([row for row in parsed_rows if row]):
        raise PackageBuildError("wheel RECORD 包含重复成员")
    if set(rows) != set(members):
        raise PackageBuildError("wheel RECORD 未完整覆盖普通成员")
    for name, payload in members.items():
        row = rows[name]
        if name == record_name:
            if row[1:] != ["", ""]:
                raise PackageBuildError("wheel RECORD 自身摘要或大小必须为空")
            continue
        expected_hash = base64.urlsafe_b64encode(
            hashlib.sha256(payload).digest()
        ).decode("ascii").rstrip("=")
        if row[1] != f"sha256={expected_hash}" or row[2] != str(len(payload)):
            raise PackageBuildError(f"wheel RECORD 摘要或大小不匹配：{name}")


def _verify_payload_and_closure(
    members: Mapping[str, bytes],
    catalog: PackageCatalog,
) -> None:
    """验证 wheel 唯一导入根及目录源码与资产闭包。

    参数：``members`` 是安全 wheel 内容；``catalog`` 是预期目录。
    返回：无。
    异常：额外顶层载荷或闭包缺失时抛出 ``PackageBuildError``。
    """

    payload_roots = {
        PurePosixPath(name).parts[0]
        for name in members
        if not PurePosixPath(name).parts[0].endswith((".dist-info", ".data"))
    }
    if payload_roots != {catalog.import_package}:
        raise PackageBuildError(
            "wheel 必须只有规范顶层导入包；实际为：" + ", ".join(sorted(payload_roots))
        )
    required = {
        *(item.declaring_file for item in catalog.definitions.devices),
        *(item.declaring_file for item in catalog.definitions.resources),
        *(item.declaring_file for item in catalog.definitions.workflows),
        *(item.logical_path for item in catalog.assets),
        f"{catalog.import_package}/_generated/package.catalog.json",
        f"{catalog.import_package}/_generated/pyproject.toml",
    }
    if catalog.definitions.workflows:
        required.add(f"{catalog.import_package}/_generated/package.yaml")
    missing = sorted(required - set(members))
    if missing:
        raise PackageBuildError("wheel 缺失包目录闭包：" + ", ".join(missing))


def _reconstruct_workspace(
    members: Mapping[str, bytes],
    catalog: PackageCatalog,
    target_root: Path,
) -> None:
    """仅从已验证 wheel 成员重建目录编译工作区。

    参数：``members`` 是完整安全成员；``catalog`` 给出导入包；``target_root`` 是
    隔离临时目录。
    返回：无；写出包源码、根声明和启动证据。
    异常：临时目录不可写时传播原始 IO 异常。
    """

    target_root.mkdir(parents=True)
    package_prefix = f"{catalog.import_package}/"
    generated_prefix = f"{catalog.import_package}/_generated"
    for name, payload in members.items():
        if not name.startswith(package_prefix) or name.startswith(
            f"{generated_prefix}/"
        ):
            continue
        target = target_root.joinpath(*PurePosixPath(name).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    target_root.joinpath("pyproject.toml").write_bytes(
        members[f"{generated_prefix}/pyproject.toml"]
    )
    package_manifest = members.get(f"{generated_prefix}/package.yaml")
    if package_manifest is not None:
        target_root.joinpath("package.yaml").write_bytes(package_manifest)
    evidence_prefix = f"{generated_prefix}/workspace/"
    for name, payload in members.items():
        if not name.startswith(evidence_prefix):
            continue
        relative = name.removeprefix(evidence_prefix)
        target = target_root.joinpath(*PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)


__all__ = [
    "MAX_ARCHIVE_BYTES",
    "artifact_digest",
    "audit_package_wheel",
    "read_embedded_package_catalog",
    "read_verified_wheel_members",
    "verify_downloaded_package_wheel",
]
