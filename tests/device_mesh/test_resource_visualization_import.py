import importlib
import sys


def test_resource_visualization_does_not_require_launch_param_builder(
    monkeypatch,
) -> None:
    """资源可视化模块不得依赖未声明的 YAML 读取包。"""

    monkeypatch.setitem(sys.modules, "launch_param_builder", None)
    sys.modules.pop("unilabos.device_mesh.resource_visalization", None)

    module = importlib.import_module(
        "unilabos.device_mesh.resource_visalization"
    )

    assert callable(module.load_yaml_file)
