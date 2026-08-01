"""F006 exact-SHA 评审发现的 source 与 activation 边界回归。"""

from __future__ import annotations

import hashlib
import sys
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from unilabos.package_manager import (
    CachedArchiveSource,
    DefinitionCatalog,
    PackageCatalog,
    PackageCompileError,
    WorkspaceSource,
    compile_package_source,
)
from unilabos.package_manager.community import (
    CommunityPackageError,
    resolve_graph_packages,
)
from unilabos.package_manager.consumers import register_package_catalog
from unilabos.package_manager.distribution import build_workspace_wheel
from unilabos.registry.registry import lab_registry


def _write_package(root: Path, source: str) -> None:
    (root / "review_lab").mkdir(parents=True)
    (root / "review_lab" / "__init__.py").write_text("", encoding="utf-8")
    (root / "review_lab" / "device.py").write_text(source, encoding="utf-8")
    (root / "pyproject.toml").write_text(
        """
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "review-lab"
version = "1.0.0"

[tool.setuptools.packages.find]
include = ["review_lab*"]
""".strip(),
        encoding="utf-8",
    )


def _artifact_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _rewrite_wheel(wheel: Path, replacements: dict[str, bytes]) -> None:
    with zipfile.ZipFile(wheel) as archive:
        members = {
            info.filename: archive.read(info)
            for info in archive.infolist()
            if not info.is_dir()
        }
    members.update(replacements)
    replacement = wheel.with_suffix(".replacement.whl")
    with zipfile.ZipFile(replacement, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in sorted(members.items()):
            archive.writestr(name, payload)
    replacement.replace(wheel)


def test_registry_projection_does_not_import_domain_annotation_modules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_package(
        tmp_path,
        """from unilabos.registry.decorators import action, device
from review_lab.types import ConnectionOptions

@device(id="pump", category=["test"])
class Pump:
    def __init__(self, options: ConnectionOptions):
        self.options = options

    @action()
    def run(self, options: ConnectionOptions) -> None:
        pass
""",
    )
    (tmp_path / "review_lab" / "types.py").write_text(
        """raise RuntimeError("domain annotation module imported")

class ConnectionOptions:
    pass
""",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(lab_registry, "device_type_registry", {})
    monkeypatch.setattr(lab_registry, "resource_type_registry", {})

    catalog = compile_package_source(WorkspaceSource(tmp_path))
    register_package_catalog(lab_registry, catalog)

    assert "review_lab.types" not in sys.modules
    assert (
        lab_registry.device_type_registry["community.review_lab.pump"][
            "init_param_schema"
        ]["config"]["properties"]["options"]["type"]
        == "object"
    )


def test_registry_projection_does_not_import_parent_action_modules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_package(
        tmp_path,
        """import builtins
from unilabos.registry.decorators import action, device

builtins._f006_parent_module_imported = True

class Parent:
    def run(self, value: int) -> None:
        pass

class ReviewAction:
    pass

@device(id="pump", category=["test"])
class Pump(Parent):
    @action(action_type=ReviewAction, parent=True)
    def run(self, *args, **kwargs):
        pass
""",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delattr("builtins._f006_parent_module_imported", raising=False)
    monkeypatch.setattr(lab_registry, "device_type_registry", {})
    monkeypatch.setattr(lab_registry, "resource_type_registry", {})

    catalog = compile_package_source(WorkspaceSource(tmp_path))
    register_package_catalog(lab_registry, catalog)

    assert not hasattr(__import__("builtins"), "_f006_parent_module_imported")


def test_cached_wheel_recompiles_source_instead_of_trusting_embedded_contract(
    tmp_path: Path,
) -> None:
    _write_package(
        tmp_path / "workspace",
        """from unilabos.registry.decorators import device

@device(id="pump", category=["test"], displayname="Source name")
class Pump:
    pass
""",
    )
    artifact = build_workspace_wheel(tmp_path / "workspace", tmp_path / "dist")
    catalog = artifact.catalog
    device = replace(catalog.definitions.devices[0], displayname="Embedded lie")
    lying_catalog = PackageCatalog.create(
        distribution=catalog.distribution,
        import_package=catalog.import_package,
        namespace=catalog.namespace,
        definitions=DefinitionCatalog(devices=(device,)),
        assets=catalog.assets,
        content_digest=catalog.content_digest,
        diagnostics=catalog.diagnostics,
    )
    _rewrite_wheel(
        artifact.wheel,
        {
            "review_lab/_generated/package.catalog.json": (
                lying_catalog.to_canonical_bytes()
            )
        },
    )

    with pytest.raises(PackageCompileError) as caught:
        compile_package_source(
            CachedArchiveSource(artifact.wheel, _artifact_digest(artifact.wheel))
        )

    assert {item.code for item in caught.value.diagnostics} == {
        "CATALOG_SOURCE_MISMATCH"
    }


def test_cached_wheel_rejects_an_extra_top_level_payload(tmp_path: Path) -> None:
    _write_package(
        tmp_path / "workspace",
        """from unilabos.registry.decorators import device

@device(id="pump", category=["test"])
class Pump:
    pass
""",
    )
    artifact = build_workspace_wheel(tmp_path / "workspace", tmp_path / "dist")
    _rewrite_wheel(
        artifact.wheel,
        {"unexpected_payload/__init__.py": b"raise RuntimeError('unexpected')\n"},
    )

    with pytest.raises(PackageCompileError) as caught:
        compile_package_source(
            CachedArchiveSource(artifact.wheel, _artifact_digest(artifact.wheel))
        )

    assert {item.code for item in caught.value.diagnostics} == {
        "ARTIFACT_PAYLOAD_INVALID"
    }


class _EscapingCommunityPort:
    def __init__(self, *, namespace: str, version: str) -> None:
        self.namespace = namespace
        self.version = version

    def resolve(
        self,
        classes: list[str],
        current_packages: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        del classes, current_packages
        return [
            {
                "class_namespace": self.namespace,
                "version": self.version,
                "artifact_digest": "sha256:" + "0" * 64,
                "download_url": "https://packages.invalid/review.whl",
            }
        ]

    def download(self, url: str, destination: Path) -> None:
        del url, destination
        raise AssertionError("非法 cache identity 必须在下载前被拒绝")


@pytest.mark.parametrize("escape_field", ["namespace", "version"])
def test_community_cache_identity_cannot_escape_its_cache_root(
    tmp_path: Path,
    escape_field: str,
) -> None:
    outside = tmp_path / "outside"
    namespace = "community.review_lab"
    version = "1.0.0"
    graph_class = "community.review_lab.pump"
    if escape_field == "namespace":
        namespace = f"community.{outside}"
        graph_class = f"{namespace}.pump"
    else:
        version = str(outside)
    graph = {"nodes": [{"class": graph_class}]}

    with pytest.raises(CommunityPackageError):
        resolve_graph_packages(
            graph,
            working_dir=tmp_path / "runtime",
            port=_EscapingCommunityPort(namespace=namespace, version=version),
        )

    assert not outside.exists()


def test_community_cache_root_cannot_be_a_symlink(tmp_path: Path) -> None:
    working_dir = tmp_path / "runtime"
    outside = tmp_path / "outside"
    working_dir.mkdir()
    outside.mkdir()
    (working_dir / "community_packages").symlink_to(outside, target_is_directory=True)

    with pytest.raises(CommunityPackageError):
        resolve_graph_packages(
            {"nodes": [{"class": "community.review_lab.pump"}]},
            working_dir=working_dir,
            port=_EscapingCommunityPort(
                namespace="community.review_lab",
                version="1.0.0",
            ),
        )

    assert list(outside.iterdir()) == []


class _DependencyInjectionPort:
    def __init__(self, wheel: Path, digest: str) -> None:
        self._wheel = wheel
        self._digest = digest

    def resolve(
        self,
        classes: list[str],
        current_packages: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        del classes, current_packages
        return [
            {
                "class_namespace": "community.review_lab",
                "version": "1.0.0",
                "artifact_digest": self._digest,
                "download_url": "https://packages.invalid/review.whl",
                "dependencies": ["untrusted-extra==9"],
            }
        ]

    def download(self, url: str, destination: Path) -> None:
        del url
        destination.write_bytes(self._wheel.read_bytes())


def test_community_dependencies_only_come_from_audited_catalog(
    tmp_path: Path,
) -> None:
    _write_package(
        tmp_path / "workspace",
        """from unilabos.registry.decorators import device

@device(id="pump", category=["test"])
class Pump:
    pass
""",
    )
    artifact = build_workspace_wheel(tmp_path / "workspace", tmp_path / "dist")

    result = resolve_graph_packages(
        {"nodes": [{"class": "community.review_lab.pump"}]},
        working_dir=tmp_path / "runtime",
        port=_DependencyInjectionPort(artifact.wheel, artifact.artifact_digest),
    )

    assert result.dependencies == ()
    assert result.catalogs[0].distribution.dependencies == ()


def test_community_wheel_publish_replaces_a_dangling_symlink_without_following_it(
    tmp_path: Path,
) -> None:
    _write_package(
        tmp_path / "workspace",
        """from unilabos.registry.decorators import device

@device(id="pump", category=["test"])
class Pump:
    pass
""",
    )
    artifact = build_workspace_wheel(tmp_path / "workspace", tmp_path / "dist")
    outside = tmp_path / "outside.whl"
    target = (
        tmp_path
        / "runtime"
        / "community_packages"
        / "review_lab"
        / "1.0.0"
        / "review_lab-1.0.0.whl"
    )
    target.parent.mkdir(parents=True)
    target.symlink_to(outside)

    result = resolve_graph_packages(
        {"nodes": [{"class": "community.review_lab.pump"}]},
        working_dir=tmp_path / "runtime",
        port=_DependencyInjectionPort(artifact.wheel, artifact.artifact_digest),
    )

    assert len(result.catalogs) == 1
    assert not target.is_symlink()
    assert target.is_file()
    assert not outside.exists()


def test_dynamic_definition_metadata_fails_catalog_compilation(tmp_path: Path) -> None:
    _write_package(
        tmp_path,
        """from unilabos.registry.decorators import device

def dynamic_value():
    return "runtime"

@device(
    id="pump",
    category=dynamic_value(),
    displayname=dynamic_value(),
)
class Pump:
    pass
""",
    )

    with pytest.raises(PackageCompileError) as caught:
        compile_package_source(WorkspaceSource(tmp_path))

    assert "DEFINITION_METADATA_DYNAMIC" in {
        item.code for item in caught.value.diagnostics
    }


def test_dynamic_workflow_metadata_fails_catalog_compilation(tmp_path: Path) -> None:
    _write_package(tmp_path, "pass\n")
    workflow_uuid = "d84f6bd8-1e83-493a-946f-28ea4f981bdd"
    (tmp_path / "review_lab" / "workflows").mkdir()
    (tmp_path / "review_lab" / "workflows" / "workflow.py").write_text(
        f'''from unilabos.workflow.authoring import workflow_definition

def dynamic_value():
    return "runtime"

@workflow_definition(
    workflow_uuid="{workflow_uuid}",
    displayname=dynamic_value(),
)
def prepare() -> None:
    pass
''',
        encoding="utf-8",
    )
    (tmp_path / "package.yaml").write_text(
        f"""package:
  name: review_lab

workflows:
  - workflow_uuid: {workflow_uuid}
    source: review_lab/workflows/workflow.py
""",
        encoding="utf-8",
    )

    with pytest.raises(PackageCompileError) as caught:
        compile_package_source(WorkspaceSource(tmp_path))

    assert "DEFINITION_METADATA_DYNAMIC" in {
        item.code for item in caught.value.diagnostics
    }
