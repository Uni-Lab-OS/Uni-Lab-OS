# AGENTS.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Also follow the monorepo-level rules in `../AGENTS.md`.

## Build & Development

```bash
# Install in editable mode (requires mamba env with python 3.11)
pip install -e .
uv pip install -r unilabos/utils/requirements.txt

# Run with a device graph
unilab --graph <graph.json> --config <config.py> --backend ros
unilab --graph <graph.json> --config <config.py> --backend simple  # no ROS2 needed

# Common CLI flags
unilab --app_bridges websocket fastapi    # communication bridges
unilab --test_mode                        # simulate hardware, no real execution
unilab --check_mode                       # CI validation of registry imports
unilab --skip_env_check                   # skip auto-install of dependencies
unilab --visual rviz|web|disable          # visualization mode
unilab --is_slave                         # run as slave node

# Workflow upload subcommand
unilab workflow_upload -f <workflow.json> -n <name> --tags tag1 tag2

# Tests
pytest tests/                              # all tests
pytest tests/resources/test_resourcetreeset.py  # single test file
pytest tests/resources/test_resourcetreeset.py::TestClassName::test_method  # single test
```

## Architecture

### Startup Flow

`unilab` CLI → `unilabos/app/main.py:main()` → loads config → builds registry → reads device graph (JSON/GraphML) → starts backend thread (ROS2/simple) → starts FastAPI web server + WebSocket client.

### Core Layers

**Registry** (`unilabos/registry/`): Singleton `Registry` class discovers and catalogs all device types, resource types, and communication devices from YAML definitions. Device types live in `registry/devices/*.yaml`, resources in `registry/resources/`, comms in `registry/device_comms/`. The registry resolves class paths to actual Python classes via `utils/import_manager.py`.

**Resource Tracking** (`unilabos/resources/resource_tracker.py`): Pydantic-based `ResourceDict` → `ResourceDictInstance` → `ResourceTreeSet` hierarchy. `ResourceTreeSet` is the canonical in-memory representation of all devices and resources, used throughout the system. Graph I/O is in `resources/graphio.py` (reads JSON/GraphML device topology files into `nx.Graph` + `ResourceTreeSet`).

**Device Drivers** (`unilabos/devices/`): 30+ hardware drivers organized by device type (liquid_handling, hplc, balance, arm, etc.). Each driver is a Python class that gets wrapped by `ros/device_node_wrapper.py:ros2_device_node()` to become a ROS2 node with publishers, subscribers, and action servers.

**ROS2 Layer** (`unilabos/ros/`): `device_node_wrapper.py` dynamically wraps any device class into `ROS2DeviceNode` (defined in `ros/nodes/base_device_node.py`). Preset node types in `ros/nodes/presets/` include `host_node`, `controller_node`, `workstation`, `serial_node`, `camera`. Messages use custom `unilabos_msgs` (pre-built, distributed via releases).

**Protocol Compilation** (`unilabos/compile/`): 20+ protocol compilers (add, centrifuge, dissolve, filter, heatchill, stir, pump, etc.) that transform YAML protocol definitions into executable sequences.

**Communication** (`unilabos/device_comms/`): Hardware communication adapters — OPC-UA client, Modbus PLC, RPC, and a universal driver. `app/communication.py` provides a factory pattern for WebSocket client connections to the cloud.

**Web/API** (`unilabos/app/web/`): FastAPI server with REST API (`api.py`), Jinja2 template pages (`pages.py`), and HTTP client for cloud communication (`client.py`). Runs on port 8002 by default.

### Configuration System

- **Config classes** in `unilabos/config/config.py`: `BasicConfig`, `WSConfig`, `HTTPConfig`, `ROSConfig` — all class-level attributes, loaded from Python config files
- Config files are `.py` files with matching class names (see `config/example_config.py`)
- Environment variables override with prefix `UNILABOS_` (e.g., `UNILABOS_BASICCONFIG_PORT=9000`)
- Device topology defined in graph files (JSON with node-link format, or GraphML)

### Key Data Flow

1. Graph file → `graphio.read_node_link_json()` → `(nx.Graph, ResourceTreeSet, resource_links)`
2. `ResourceTreeSet` + `Registry` → `initialize_device.initialize_device_from_dict()` → `ROS2DeviceNode` instances
3. Device nodes communicate via ROS2 topics/actions or direct Python calls (simple backend)
4. Cloud sync via WebSocket (`app/ws_client.py`) and HTTP (`app/web/client.py`)

### Test Data

Example device graphs and experiment configs are in `unilabos/test/experiments/` (not `tests/`). Registry test fixtures in `unilabos/test/registry/`.

## Code Conventions

- Code comments and log messages in simplified Chinese
- Python 3.11+, type hints expected
- Pydantic models for data validation (`resource_tracker.py`)
- Singleton pattern via `@singleton` decorator (`utils/decorator.py`)
- Dynamic class loading via `utils/import_manager.py` — device classes resolved at runtime from registry YAML paths
- CLI argument dashes auto-converted to underscores for consistency

## ResourceDict 根字段（提升字段）新增守则

ResourceDict（`unilabos/resources/resource_tracker.py`）是全系统唯一内存模型。把 PLR
序列化产物（config/data）中的状态键提升为根字段时（先例：`barcode/barcode_symbology`
从 config 提升、`liquids/liquid_history/unknown_counter` 从 data 提升），**必须一次性
覆盖以下全部点位，缺任何一处即在对应通路丢数据**：

1. **模型**：`ResourceDict` 与 `ResourceDictType` 同步加字段；用 `Optional`+`None`
   区分「无该状态」与「空状态」（`[]`/`0`/`""`）。成组的提升键定义为模块级常量
   （如 `TRACKER_STATE_KEYS`）并在所有点位引用，禁止散写字符串。
2. **提升（唯一入口）**：`ResourceDictInstance.get_resource_instance_from_dict` 漏斗内
   从源命名空间 `pop` → 根字段；根字段已有值则以根为准、仅清理源。所有 dict 输入
   （图文件、TCP/HTTP JSON、msg 回程）都汇入该漏斗，提升逻辑不得写在别处。
3. **回装（全部出口）**，与提升方向严格对称：
   - `ResourceTreeSet.to_plr_resources`（`load_all_state` 前经 `assemble_tracker_state`
     类函数组装回 serialize_state 形态）；
   - `get_plr_nested_dict`（PLR 嵌套形态，根键不外泄）；
   - `unilabos/ros/msgs/message_converter.py` 的 `Resource` msg 转换器（msg 无根字段，
     进 msg 前把提升键归位回 config/data，参照 `obtain_config_with_barcode` /
     `obtain_data_with_uuid`）；
   - `host_node` graph 合并（已存在节点同步刷新根字段，防与 data 双真相漂移）；
   - `graphio.resource_ulab_to_plr` 老转换器（兼容老/新双形态输入，参照 `state_of`）。
4. **白名单自动化**：`graphio.canonicalize_nodes_data` 的根键白名单由
   `RESOURCE_ROOT_FIELDS` 从 `ResourceDict.model_fields` 派生——新增根字段自动生效。
   **不得改回硬编码清单**。
5. **守护测试**：在 `tests/resources/test_tracker_state_promotion.py` 为新字段补
   漏斗提升 / 根字段优先 / None 区分 / dump 幂等 / msg 双形态往返 / PLR round-trip
   用例；`TestRootFieldContract` 会兜底白名单机制。
6. **契约登记**：在 `unilab-edge-ui/docs/protocol/cloud-mapping.md` §6 拆装表登记
   新字段的云端表化归属与拆装规则。

## Workflow Task Runtime Invariants

### Backend-Aligned Identity and Naming

- `Workflow` is an editable static definition. `WorkflowTask` is one immutable
  execution snapshot. `WorkflowNodeJob` is one attempt to execute one node in a
  task. Never use `/api/v1/workflows` to submit or address a running task.
- Mirror the Backend model directly:
  - model types use names such as `WorkflowNode` and `WorkflowTask`;
  - persistent tables use singular snake case such as `workflow_node` and
    `workflow_task`;
  - entity primary keys are exposed as `uuid`;
  - relationships use `workflow_uuid`, `workflow_task_uuid`,
    `workflow_node_uuid`, `source_node_uuid`, and `target_node_uuid`;
  - path and local identity variables use `workflow_uuid`, `task_uuid`,
    `node_uuid`, `edge_uuid`, and `job_uuid`.
- Do not introduce or retain parallel execution identities such as `run_id`,
  `task_id`, `node_id`, `job_id`, camel-case wire aliases, or
  `/api/v1/runtime/runs`. Never default `task_uuid` to `workflow_uuid`.

### Runtime Authority

- Node execution state and node result are the only mutable persisted workflow
  execution facts. Workflow edges are immutable graph topology; their
  active/inactive/unresolved resolution is derived inside the scheduler from
  topology, node state, and node result.
- Branch selection belongs in the branch node result. A skipped node must carry
  a stable reason. Do not create a second persisted edge execution state.
- Exactly one scheduler owns readiness, admission, node terminal state, and
  task completion. Bridges, HTTP handlers, WebSocket sessions, device drivers,
  and frontend projections must not implement another DAG walker or debugger.
- Transport acceptance, dispatch acknowledgement, HTTP success, and WebSocket
  delivery are not node terminal results. An uncertain physical action must
  remain fenced until explicit query/reconciliation establishes its state.

### Migration and Verification

- Run Python tests, migration tools, and local servers only with the `unilab`
  Python 3.11 environment. On this host the guaranteed interpreter is
  `/home/changjunhan/.micromamba/envs/unilab/bin/python`.
- Preserve the reviewed Edge Scheduler, HostLink, Inventory, and action-policy
  tests while migrating the current workflow/scheduler/runtime tests. Never
  delete, weaken, permanently skip, or xfail a contract test to make a
  migration phase pass.
- New workflow execution code and tests use Backend-aligned identity names
  immediately; no compatibility adapter is maintained for the old Run
  vocabulary.

## Licensing

- Framework code: GPL-3.0
- Device drivers (`unilabos/devices/`): DP Technology Proprietary License — do not redistribute
