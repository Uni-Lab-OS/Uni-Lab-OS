"""受管设备包写入、更新、移除和恢复本地设备图的 P2 合同测试。"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest

from unilabos.app.main import parse_args
from unilabos.package_manager import WorkspaceSource, compile_package_source
from unilabos.package_manager.cli import run_package_command
from unilabos.package_manager.community import (
    CommunityPackageError,
    resolve_graph_packages,
)
from unilabos.package_manager.consumers import register_package_catalog
from unilabos.package_manager.device_package import download_device_package
from unilabos.package_manager.device_provisioning import (
    DeviceProvisioningError,
    remove_device_instance,
    restore_device_graph,
    stage_device_instance,
    update_device_instance,
)
from unilabos.package_manager.device_secrets import resolve_device_configuration
from unilabos.package_manager.distribution import BuildArtifact, build_workspace_wheel
from unilabos.registry.registry import Registry

_TEMPLATE_UUID = "d8f0fe85-d34a-4eb7-965a-5af0e2cf6939"
_DEFINITION_FQID = "community.provisioning_lab.pump"
_INSTANCE_UUID = "5e021578-a973-4576-a750-4cc8af44108c"


class _CopyDownloadPort:
    """把本地测试 wheel 复制为模拟云端下载结果。"""

    def __init__(self, wheel: Path) -> None:
        """保存本测试允许发布到受管缓存的唯一 wheel。"""

        self._wheel = wheel

    def download(self, url: str, destination: Path) -> None:
        """忽略模拟 URL 并复制已构建 Artifact 到下载临时文件。"""

        del url
        destination.write_bytes(self._wheel.read_bytes())


def _build_device_artifact(
    root: Path,
    *,
    version: str = "2.4.0",
    init_signature: str = "endpoint: str, retries: int = 3",
) -> BuildArtifact:
    """按指定初始化合同构建最小设备 wheel。

    ``root`` 是隔离构建目录，``version`` 是发布版本，``init_signature`` 是设备
    构造函数参数源码。返回带摘要的测试 Artifact。
    """

    workspace = root / "workspace"
    package = workspace / "provisioning_lab"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "device.py").write_text(
        """
from unilabos.registry.decorators import device

@device(id="pump", category=["test"])
class Pump:
    def __init__(self, {init_signature}):
        self.endpoint = endpoint
""".strip().format(init_signature=init_signature),
        encoding="utf-8",
    )
    (workspace / "pyproject.toml").write_text(
        """
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "provisioning-lab"
version = "{version}"

[tool.setuptools.packages.find]
include = ["provisioning_lab*"]
""".strip().format(version=version),
        encoding="utf-8",
    )
    return build_workspace_wheel(workspace, root / "dist")


def _cache_device_package(
    tmp_path: Path,
    *,
    init_signature: str = "endpoint: str, retries: int = 3",
) -> tuple[str, Path]:
    """下载指定初始化合同的 Artifact 并返回缓存身份与受管目录。

    ``tmp_path`` 是测试隔离目录，``init_signature`` 是目标设备构造合同。返回
    ``cache_key`` 与当前测试的 OS 受管工作目录。
    """

    artifact = _build_device_artifact(tmp_path, init_signature=init_signature)
    working_dir = tmp_path / "runtime"
    result = download_device_package(
        template_uuid=_TEMPLATE_UUID,
        definition_fqid=_DEFINITION_FQID,
        artifact_digest=artifact.artifact_digest,
        backend_base_url="https://backend.example/api/v1",
        working_dir=str(working_dir),
        port=_CopyDownloadPort(artifact.wheel),
    )
    return result.cache_key, working_dir


def _write_graph(tmp_path: Path) -> tuple[Path, bytes]:
    """写入包含扩展根字段和既有节点的合法 node-link 测试图。"""

    graph_path = tmp_path / "device-graph.json"
    graph = {
        "directed": False,
        "multigraph": False,
        "graph": {"fixture": "preserved"},
        "nodes": [
            {
                "id": "existing-device",
                "uuid": "0beec49d-da68-4647-8ed4-0ab952d033b9",
                "name": "Existing device",
                "children": [],
                "parent": None,
                "type": "device",
                "class": "fake_device",
                "position": {"x": 10, "y": 20, "z": 0},
                "config": {},
                "data": {},
                "extra": {},
            }
        ],
        "links": [],
    }
    graph_path.write_text(
        json.dumps(graph, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return graph_path, graph_path.read_bytes()


def _stage_kwargs(
    graph_path: Path,
    working_dir: Path,
    cache_key: str,
) -> dict[str, object]:
    """生成新增测试共用的稳定实例身份与用户配置。"""

    return {
        "graph_path": graph_path,
        "working_dir": working_dir,
        "cache_key": cache_key,
        "definition_fqid": _DEFINITION_FQID,
        "instance_id": "local_pump_1",
        "instance_uuid": _INSTANCE_UUID,
        "display_name": "Local Pump 1",
        "configuration": {"endpoint": "serial:///dev/ttyUSB0"},
    }


def test_stage_device_instance_preserves_graph_and_writes_recoverable_backup(
    tmp_path: Path,
) -> None:
    """新增实例必须补默认配置、保留既有字段并先落下原图备份。"""

    cache_key, working_dir = _cache_device_package(tmp_path)
    graph_path, original = _write_graph(tmp_path)

    result = stage_device_instance(**_stage_kwargs(graph_path, working_dir, cache_key))

    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    added = next(node for node in graph["nodes"] if node["id"] == "local_pump_1")
    assert result.status == "graph_staged"
    assert result.changed is True
    assert result.graph_fingerprint.startswith("sha256:")
    assert Path(str(result.backup_path)).read_bytes() == original
    assert graph["graph"] == {"fixture": "preserved"}
    assert graph["nodes"][0]["id"] == "existing-device"
    assert added["class"] == _DEFINITION_FQID
    assert added["config"] == {
        "endpoint": "serial:///dev/ttyUSB0",
        "retries": 3,
    }
    assert added["extra"]["unilab"]["package_cache_key"] == cache_key


def test_stage_device_instance_rejects_non_ros_instance_id_before_graph_write(
    tmp_path: Path,
) -> None:
    """含横线的实例 ID 必须在写图前失败关闭，避免 Edge 永远无法加载节点。

    ``tmp_path`` 提供隔离设备包缓存和设备图；函数返回 ``None``，并证明失败
    不会创建备份或改变原始设备图字节。
    """

    cache_key, working_dir = _cache_device_package(tmp_path)
    graph_path, original = _write_graph(tmp_path)
    request = _stage_kwargs(graph_path, working_dir, cache_key)
    request["instance_id"] = "szlab_mock-mock_s08_cap_station"

    with pytest.raises(DeviceProvisioningError, match="ROS 2"):
        stage_device_instance(**request)

    assert graph_path.read_bytes() == original
    assert list(tmp_path.glob("*.unilab-backup-*.json")) == []


def test_stage_device_instance_writes_secret_reference_instead_of_password(
    tmp_path: Path,
) -> None:
    """秘密配置必须进入受管存储，设备图只能持久化版本化引用。"""

    cache_key, working_dir = _cache_device_package(
        tmp_path,
        init_signature="endpoint: str, password: str",
    )
    graph_path, _ = _write_graph(tmp_path)
    request = _stage_kwargs(graph_path, working_dir, cache_key)
    request["configuration"] = {
        "endpoint": "serial:///dev/ttyUSB0",
        "password": "device-password",
    }

    first = stage_device_instance(**request)

    graph_bytes = graph_path.read_bytes()
    graph = json.loads(graph_bytes)
    added = next(node for node in graph["nodes"] if node["id"] == "local_pump_1")
    assert b"device-password" not in graph_bytes
    assert added["config"]["password"] == {
        "$unilab_secret": {
            "schema_version": "device-secret-ref/v1",
            "id": added["config"]["password"]["$unilab_secret"]["id"],
        }
    }
    secret_id = added["config"]["password"]["$unilab_secret"]["id"]
    secret_file = working_dir / "device-secrets" / "v1" / secret_id
    assert secret_file.stat().st_mode & 0o777 == 0o600
    assert resolve_device_configuration(
        added["config"],
        working_dir=working_dir,
    ) == {
        "endpoint": "serial:///dev/ttyUSB0",
        "password": "device-password",
    }

    second = stage_device_instance(**request)

    assert first.changed is True
    assert second.changed is False
    assert second.backup_path is None


def test_stage_device_instance_is_idempotent_for_identical_declaration(
    tmp_path: Path,
) -> None:
    """同身份同内容重放不得二次写图或创建新的备份事实。"""

    cache_key, working_dir = _cache_device_package(tmp_path)
    graph_path, _ = _write_graph(tmp_path)
    request = _stage_kwargs(graph_path, working_dir, cache_key)
    first = stage_device_instance(**request)
    staged_bytes = graph_path.read_bytes()

    second = stage_device_instance(**request)

    assert first.changed is True
    assert second.changed is False
    assert second.backup_path is None
    assert second.graph_fingerprint == first.graph_fingerprint
    assert graph_path.read_bytes() == staged_bytes


def test_graph_activation_keeps_the_staged_package_release(
    tmp_path: Path,
) -> None:
    """同 namespace 下载新版本后，既有设备图仍必须加载写图时固定的 wheel。"""

    first_artifact = _build_device_artifact(tmp_path / "first", version="2.4.0")
    working_dir = tmp_path / "runtime"
    first = download_device_package(
        template_uuid=_TEMPLATE_UUID,
        definition_fqid=_DEFINITION_FQID,
        artifact_digest=first_artifact.artifact_digest,
        backend_base_url="https://backend.example/api/v1",
        working_dir=str(working_dir),
        port=_CopyDownloadPort(first_artifact.wheel),
    )
    graph_path, _ = _write_graph(tmp_path)
    stage_device_instance(**_stage_kwargs(graph_path, working_dir, first.cache_key))
    second_artifact = _build_device_artifact(tmp_path / "second", version="2.5.0")
    download_device_package(
        template_uuid=_TEMPLATE_UUID,
        definition_fqid=_DEFINITION_FQID,
        artifact_digest=second_artifact.artifact_digest,
        backend_base_url="https://backend.example/api/v1",
        working_dir=str(working_dir),
        port=_CopyDownloadPort(second_artifact.wheel),
    )

    resolution = resolve_graph_packages(
        json.loads(graph_path.read_text(encoding="utf-8")),
        working_dir=working_dir,
    )

    assert [item.distribution.version for item in resolution.catalogs] == ["2.4.0"]
    assert [source.expected_digest for source in resolution.sources] == [
        first_artifact.artifact_digest
    ]


def test_graph_activation_deduplicates_identical_workspace_and_cached_catalog(
    tmp_path: Path,
) -> None:
    """同内容 Workspace 与设备图固定 wheel 只能向 Registry 提供一个 Catalog。

    ``tmp_path`` 同时承载测试 Workspace 与受管 wheel 缓存。函数返回 ``None``；
    它证明 Graph 仍会校验固定缓存身份，但不会把相同 Catalog 再次交给启动链路。
    """

    cache_key, working_dir = _cache_device_package(tmp_path)
    workspace_catalog = compile_package_source(WorkspaceSource(tmp_path / "workspace"))
    graph = {
        "nodes": [
            {
                "class": _DEFINITION_FQID,
                "extra": {"unilab": {"package_cache_key": cache_key}},
            }
        ]
    }

    resolution = resolve_graph_packages(
        graph,
        working_dir=working_dir,
        available_catalogs=(workspace_catalog,),
    )

    assert resolution.sources == ()
    assert resolution.catalogs == ()
    assert resolution.classes == (_DEFINITION_FQID,)


def test_graph_activation_rejects_conflicting_workspace_and_cached_catalog(
    tmp_path: Path,
) -> None:
    """同 namespace 的 Workspace 与固定 wheel 内容不同时必须失败关闭。

    ``tmp_path`` 提供隔离源码和缓存；测试先固定发布 wheel，再修改 Workspace
    初始化合同。函数返回 ``None``，并验证启动不会在两套驱动定义间猜测来源。
    """

    cache_key, working_dir = _cache_device_package(tmp_path)
    device_source = tmp_path / "workspace" / "provisioning_lab" / "device.py"
    device_source.write_text(
        device_source.read_text(encoding="utf-8").replace(
            "retries: int = 3",
            "retries: int = 5",
        ),
        encoding="utf-8",
    )
    workspace_catalog = compile_package_source(WorkspaceSource(tmp_path / "workspace"))
    graph = {
        "nodes": [
            {
                "class": _DEFINITION_FQID,
                "extra": {"unilab": {"package_cache_key": cache_key}},
            }
        ]
    }

    with pytest.raises(
        CommunityPackageError,
        match="Workspace Catalog 与设备图固定版本不一致",
    ):
        resolve_graph_packages(
            graph,
            working_dir=working_dir,
            available_catalogs=(workspace_catalog,),
        )


def test_register_package_catalog_identical_replay_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registry 必须允许同一 Catalog 重放且不得重建已有定义条目。

    ``tmp_path`` 提供隔离 Workspace，``monkeypatch`` 隔离 Registry 映射。函数
    返回 ``None``；它保护组合根重复传入相同 Catalog 时的最后一道幂等边界。
    """

    _build_device_artifact(tmp_path)
    catalog = compile_package_source(WorkspaceSource(tmp_path / "workspace"))
    registry = Registry()
    monkeypatch.setattr(registry, "device_type_registry", {})
    monkeypatch.setattr(registry, "resource_type_registry", {})

    register_package_catalog(registry, catalog)
    original_entry = registry.device_type_registry[_DEFINITION_FQID]
    register_package_catalog(registry, catalog)

    assert registry.device_type_registry == {_DEFINITION_FQID: original_entry}
    assert registry.device_type_registry[_DEFINITION_FQID] is original_entry


@pytest.mark.parametrize(
    ("configuration", "message"),
    [
        ({}, "缺少必填参数: endpoint"),
        ({"endpoint": 42}, "endpoint 必须是 string"),
        (
            {"endpoint": "serial:///dev/ttyUSB0", "password": "secret"},
            "包含未知参数: password",
        ),
    ],
)
def test_stage_device_instance_rejects_invalid_configuration_without_graph_write(
    tmp_path: Path,
    configuration: dict[str, object],
    message: str,
) -> None:
    """缺失、类型错误或未知配置字段必须失败关闭并保持原图字节不变。"""

    cache_key, working_dir = _cache_device_package(tmp_path)
    graph_path, original = _write_graph(tmp_path)
    request = _stage_kwargs(graph_path, working_dir, cache_key)
    request["configuration"] = configuration

    with pytest.raises(DeviceProvisioningError, match=message):
        stage_device_instance(**request)

    assert graph_path.read_bytes() == original
    assert list(tmp_path.glob("*.unilab-backup-*.json")) == []


def test_update_remove_and_restore_device_instance_are_explicit_and_atomic(
    tmp_path: Path,
) -> None:
    """显式更新、移除与恢复必须保持 UUID，且每次副作用都有可用备份。"""

    cache_key, working_dir = _cache_device_package(tmp_path)
    graph_path, _ = _write_graph(tmp_path)
    request = _stage_kwargs(graph_path, working_dir, cache_key)
    stage_device_instance(**request)
    staged_graph = json.loads(graph_path.read_text(encoding="utf-8"))

    update_request = dict(request)
    update_request["display_name"] = "Renamed Local Pump"
    update_request["configuration"] = {
        "endpoint": "tcp://127.0.0.1:9000",
        "retries": 5,
    }
    updated = update_device_instance(**update_request)

    assert updated.changed is True
    updated_graph = json.loads(graph_path.read_text(encoding="utf-8"))
    updated_node = next(
        node for node in updated_graph["nodes"] if node["id"] == "local_pump_1"
    )
    assert updated_node["uuid"] == _INSTANCE_UUID
    assert updated_node["name"] == "Renamed Local Pump"
    assert updated_node["config"]["retries"] == 5

    removed = remove_device_instance(
        graph_path=graph_path,
        instance_id="local_pump_1",
        instance_uuid=_INSTANCE_UUID,
    )
    assert removed.changed is True
    assert all(
        node["id"] != "local_pump_1"
        for node in json.loads(graph_path.read_text(encoding="utf-8"))["nodes"]
    )

    restored = restore_device_graph(
        graph_path=graph_path,
        backup_path=str(updated.backup_path),
    )
    assert restored.status == "graph_restored"
    assert json.loads(graph_path.read_text(encoding="utf-8")) == staged_graph


def test_package_add_device_cli_explicitly_adopts_uuidless_legacy_instance(
    tmp_path: Path,
) -> None:
    """CLI 只有收到显式接管意图时才能为同定义旧节点补齐稳定 UUID。"""

    cache_key, working_dir = _cache_device_package(tmp_path)
    graph_path, _ = _write_graph(tmp_path)
    # 遗留节点身份为空，但其既有拓扑、运行数据与部署扩展都必须继续有效。
    legacy_graph = json.loads(graph_path.read_text(encoding="utf-8"))
    legacy_graph["nodes"][0]["children"] = ["local_pump_1"]
    legacy_graph["links"].append(
        {"source": "existing-device", "target": "local_pump_1", "type": "owns"}
    )
    legacy_graph["nodes"].append(
        {
            "id": "local_pump_1",
            "name": "Legacy Pump",
            "children": [],
            "parent": "existing-device",
            "type": "device",
            "class": _DEFINITION_FQID,
            "position": {"x": 120, "y": 340, "z": 0},
            "config": {"endpoint": "serial:///dev/legacy", "retries": 2},
            "data": {"status": "Idle"},
            "extra": {"legacy_marker": "preserve-me"},
        }
    )
    graph_path.write_text(
        json.dumps(legacy_graph, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    legacy_bytes = graph_path.read_bytes()

    with pytest.raises(DeviceProvisioningError, match="同名设备实例缺少 UUID"):
        stage_device_instance(**_stage_kwargs(graph_path, working_dir, cache_key))
    assert graph_path.read_bytes() == legacy_bytes

    parser = parse_args()
    args = vars(
        parser.parse_args(
            [
                "--working_dir",
                str(working_dir),
                "package",
                "add-device",
                "--cache-key",
                cache_key,
                "--definition-fqid",
                _DEFINITION_FQID,
                "--instance-id",
                "local_pump_1",
                "--instance-uuid",
                _INSTANCE_UUID,
                "--adopt-existing",
                "--graph",
                str(graph_path),
                "--config-stdin",
                "--json",
            ]
        )
    )
    output = StringIO()

    result = run_package_command(
        args,
        working_dir=working_dir,
        input_stream=StringIO(
            json.dumps(
                {
                    "display_name": "Local Pump 1",
                    "configuration": {"endpoint": "serial:///dev/ttyUSB0"},
                }
            )
        ),
        stream=output,
    )

    assert json.loads(output.getvalue()) == result.to_dict()
    assert json.loads(output.getvalue())["status"] == "graph_staged"
    adopted_graph = json.loads(graph_path.read_text(encoding="utf-8"))
    adopted = next(
        node for node in adopted_graph["nodes"] if node["id"] == "local_pump_1"
    )
    assert adopted["uuid"] == _INSTANCE_UUID
    assert adopted["parent"] == "existing-device"
    assert adopted["position"] == {"x": 120, "y": 340, "z": 0}
    assert adopted["data"] == {"status": "Idle"}
    assert adopted["extra"]["legacy_marker"] == "preserve-me"
    assert adopted["extra"]["unilab"]["package_cache_key"] == cache_key
    assert adopted_graph["nodes"][0]["children"] == ["local_pump_1"]
    assert adopted_graph["links"][-1] == {
        "source": "existing-device",
        "target": "local_pump_1",
        "type": "owns",
    }
    adopted_bytes = graph_path.read_bytes()
    replayed = stage_device_instance(
        **_stage_kwargs(graph_path, working_dir, cache_key)
    )
    assert replayed.changed is False
    assert replayed.backup_path is None
    assert graph_path.read_bytes() == adopted_bytes

    conflicting_request = _stage_kwargs(graph_path, working_dir, cache_key)
    conflicting_request["instance_uuid"] = "10b3dffd-5f98-46b8-ac0d-354254793ec4"
    with pytest.raises(DeviceProvisioningError, match="UUID 与请求不一致"):
        stage_device_instance(**conflicting_request, adopt_existing=True)
    assert graph_path.read_bytes() == adopted_bytes


def test_stage_device_instance_reuses_semantically_identical_backup(
    tmp_path: Path,
) -> None:
    """同一设备图仅改变 JSON 排版后重试时必须复用既有语义备份。

    ``tmp_path`` 提供隔离设备包缓存、设备图与备份目录。测试返回 ``None``；
    它证明备份身份由完整设备图语义决定，而不是由缩进、空白或键顺序决定，
    同时确保重试不会覆盖第一次接入前的可恢复备份。
    """

    cache_key, working_dir = _cache_device_package(tmp_path)
    graph_path, original_bytes = _write_graph(tmp_path)
    # 初始备份保存第一次接入前的权威设备图字节，后续重试不得覆盖它。
    first_result = stage_device_instance(
        **_stage_kwargs(graph_path, working_dir, cache_key)
    )
    restore_device_graph(
        graph_path=graph_path,
        backup_path=str(first_result.backup_path),
    )
    original_graph = json.loads(original_bytes)
    graph_path.write_text(
        json.dumps(original_graph, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    retried_result = stage_device_instance(
        **_stage_kwargs(graph_path, working_dir, cache_key)
    )

    assert retried_result.changed is True
    assert retried_result.backup_path == first_result.backup_path
    assert Path(str(first_result.backup_path)).read_bytes() == original_bytes


def test_stage_device_instance_rejects_different_graph_at_existing_backup_path(
    tmp_path: Path,
) -> None:
    """既有备份内容属于不同设备图时必须拒绝接入并保持当前图不变。

    ``tmp_path`` 提供隔离设备包缓存、设备图与备份目录。测试返回 ``None``；
    它模拟同名备份被替换为另一张合法设备图，证明语义复用不会覆盖冲突备份，
    也不会在冲突后部分写入当前设备图。
    """

    cache_key, working_dir = _cache_device_package(tmp_path)
    graph_path, _ = _write_graph(tmp_path)
    first_result = stage_device_instance(
        **_stage_kwargs(graph_path, working_dir, cache_key)
    )
    restore_device_graph(
        graph_path=graph_path,
        backup_path=str(first_result.backup_path),
    )
    current_graph_bytes = graph_path.read_bytes()
    # 冲突备份是合法 JSON，但不再代表当前接入操作变更前的设备图事实。
    conflicting_backup = Path(str(first_result.backup_path))
    conflicting_backup.write_text(
        json.dumps({"nodes": [{"id": "other-device"}], "links": []}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(DeviceProvisioningError, match="属于不同设备图"):
        stage_device_instance(**_stage_kwargs(graph_path, working_dir, cache_key))

    assert graph_path.read_bytes() == current_graph_bytes
