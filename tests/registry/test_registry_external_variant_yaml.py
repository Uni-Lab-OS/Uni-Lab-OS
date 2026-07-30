"""外部 variant YAML 按当前 JSON-enforced 合同隔离加载。"""

import subprocess
import sys
from pathlib import Path
from textwrap import dedent

FIXTURE = Path(__file__).parent / "fixtures" / "external_variant_registry"


def test_registry_loads_multiple_variants_sharing_same_class(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            dedent(
                """
                import sys
                from concurrent.futures import ThreadPoolExecutor
                from pathlib import Path

                from unilabos.registry.registry import Registry

                fixture = Path(sys.argv[1])
                registry = Registry()
                with ThreadPoolExecutor(max_workers=2) as executor:
                    registry._startup_executor = executor
                    try:
                        registry.load_device_types(
                            fixture,
                            complete_registry=False,
                        )

                        model_a = registry.device_type_registry[
                            "vendor.lh.model_a"
                        ]
                        model_b = registry.device_type_registry[
                            "vendor.lh.model_b"
                        ]

                        assert model_a["class"]["module"].endswith(
                            ":JsonConfiguredDevice"
                        )
                        assert model_b["class"]["module"].endswith(
                            ":JsonConfiguredDevice"
                        )
                        assert model_a["implementation"]["variant"] == "model_a"
                        assert model_b["implementation"]["variant"] == "model_b"
                        assert "init" not in model_a["class"]
                        assert "init" not in model_b["class"]
                        assert model_a["init_param_enforce"] == {
                            "backend_type": "mock",
                            "backend_params": {"port": 4008},
                            "deck_name": "model-a-deck",
                            "channels": 8,
                        }
                        assert model_b["init_param_enforce"] == {
                            "backend_type": "mock",
                            "backend_params": {"port": 4096},
                            "deck_name": "model-b-deck",
                            "channels": 96,
                        }
                        assert (
                            "setup"
                            in model_a["class"]["action_value_mappings"]
                        )
                        assert (
                            "initialized"
                            in model_b["class"]["status_types"]
                        )
                    finally:
                        registry._startup_executor = None
                """
            ),
            str(FIXTURE.resolve()),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
