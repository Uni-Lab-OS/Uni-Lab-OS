"""Registry-owned initialization parameters are JSON data, not factories."""

import pytest

from unilabos.registry.init_enforce import (
    merge_init_param_enforce,
    validate_init_param_enforce,
)


CONFIG_SCHEMA = {
    "config": {
        "type": "object",
        "required": ["channels"],
        "properties": {
            "channels": {"type": "integer"},
            "transport": {"type": "object"},
        },
    }
}


def test_merge_init_param_enforce_applies_registry_values_recursively():
    runtime_config = {
        "channels": 1,
        "transport": {"host": "127.0.0.1", "port": 9000},
    }
    enforced = {
        "channels": 96,
        "transport": {"port": 5000},
    }

    merged = merge_init_param_enforce(runtime_config, enforced)

    assert merged == {
        "channels": 96,
        "transport": {"host": "127.0.0.1", "port": 5000},
    }
    assert runtime_config["channels"] == 1
    assert enforced["transport"]["port"] == 5000


def test_validate_init_param_enforce_accepts_json_config():
    validate_init_param_enforce(
        "vendor.lh.model_a",
        CONFIG_SCHEMA,
        {"channels": 8, "transport": {"kind": "mock"}},
    )


@pytest.mark.parametrize(
    "legacy_value",
    [
        {"channels": 8, "backend": {"factory": "example:Backend"}},
        {"channels": 8, "name": "${node.id}"},
        {"channels": 8, "deck": {"value": "model-a"}},
    ],
)
def test_validate_init_param_enforce_rejects_class_init_dsl(legacy_value):
    with pytest.raises(ValueError, match="不支持"):
        validate_init_param_enforce(
            "vendor.lh.model_a",
            CONFIG_SCHEMA,
            legacy_value,
        )
