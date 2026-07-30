"""社区设备类名在公开 Registry 构建入口中的身份合同。"""

import json
import subprocess
import sys
from pathlib import Path


def test_build_registry_keeps_community_namespace_as_entity_key(tmp_path):
    package_dir = tmp_path / "example_community_package"
    package_dir.mkdir()
    (package_dir / "device.py").write_text(
        """
from unilabos.registry.decorators import device


@device(id="sample_device", category=["test"])
class SampleDevice:
    pass
""",
        encoding="utf-8",
    )

    script = """
import json
import sys
from pathlib import Path

from unilabos.registry.registry import build_registry

package_dir = Path(sys.argv[1]).resolve()
namespace = "community.example"
registry = build_registry(
    devices_dirs=[str(package_dir)],
    external_only=True,
    community_namespaces={str(package_dir): namespace},
)
print(json.dumps({
    "qualified_key_exists": f"{namespace}.sample_device" in registry.device_type_registry,
    "stripped_key_exists": "sample_device" in registry.device_type_registry,
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(package_dir)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout.strip().splitlines()[-1])
    assert observed == {
        "qualified_key_exists": True,
        "stripped_key_exists": False,
    }
