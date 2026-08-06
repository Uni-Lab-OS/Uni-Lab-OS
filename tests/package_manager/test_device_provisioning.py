"""受管设备包写入、更新、移除和恢复本地设备图的 P2 合同测试。"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest

from unilabos.app.main import parse_args
from unilabos.package_manager.cli import run_package_command
from unilabos.package_manager.community import resolve_graph_packages
from unilabos.package_manager.device_package import download_device_package
from unilabos.package_manager.device_provisioning import (
    DeviceProvisioningError,
    remove_device_instance,
    restore_device_graph,
    stage_device_instance,
    update_device_instance,
)
from unilabos.package_manager.distribution import BuildArtifact, build_workspace_wheel

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


def _build_device_artifact(root: Path, *, version: str = "2.4.0") -> BuildArtifact:
    """构建带必填 endpoint 和静态 retries 默认值的最小设备 wheel。"""

    workspace = root / "workspace"
    package = workspace / "provisioning_lab"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "device.py").write_text(
        """
from unilabos.registry.decorators import device

@device(id="pump", category=["test"])
class Pump:
    def __init__(self, endpoint: str, retries: int = 3):
        self.endpoint = endpoint
        self.retries = retries
""".strip(),
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


def _cache_device_package(tmp_path: Path) -> tuple[str, Path]:
    """下载测试 Artifact 并返回稳定 cache_key 与受管工作目录。"""

    artifact = _build_device_artifact(tmp_path)
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
        "instance_id": "local-pump-1",
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

    result = stage_device_instance(
        **_stage_kwargs(graph_path, working_dir, cache_key)
    )

    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    added = next(node for node in graph["nodes"] if node["id"] == "local-pump-1")
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
    stage_device_instance(
        **_stage_kwargs(graph_path, working_dir, first.cache_key)
    )
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
        node for node in updated_graph["nodes"] if node["id"] == "local-pump-1"
    )
    assert updated_node["uuid"] == _INSTANCE_UUID
    assert updated_node["name"] == "Renamed Local Pump"
    assert updated_node["config"]["retries"] == 5

    removed = remove_device_instance(
        graph_path=graph_path,
        instance_id="local-pump-1",
        instance_uuid=_INSTANCE_UUID,
    )
    assert removed.changed is True
    assert all(
        node["id"] != "local-pump-1"
        for node in json.loads(graph_path.read_text(encoding="utf-8"))["nodes"]
    )

    restored = restore_device_graph(
        graph_path=graph_path,
        backup_path=str(updated.backup_path),
    )
    assert restored.status == "graph_restored"
    assert json.loads(graph_path.read_text(encoding="utf-8")) == staged_graph


def test_package_add_device_cli_reads_configuration_only_from_stdin(
    tmp_path: Path,
) -> None:
    """CLI 必须从 stdin 读取封闭配置并输出单行 graph_staged JSON。"""

    cache_key, working_dir = _cache_device_package(tmp_path)
    graph_path, _ = _write_graph(tmp_path)
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
                "local-pump-1",
                "--instance-uuid",
                _INSTANCE_UUID,
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
