# AGENTS.md

This file provides guidance to coding agents working in this repository.
Rules in this file apply to the whole `Uni-Lab-OS` repository.

## Build & Development

```bash
UNILAB_ENV=/home/changjunhan/.micromamba/envs/unilab

# Install in editable mode (the unilab Python 3.11 environment is mandatory)
"$UNILAB_ENV/bin/python" -m pip install -e .
"$UNILAB_ENV/bin/python" -m pip install -r unilabos/utils/requirements.txt

# Run with a device graph
"$UNILAB_ENV/bin/unilab" --graph <graph.json> --config <config.py> --backend ros
"$UNILAB_ENV/bin/unilab" --graph <graph.json> --config <config.py> --backend simple  # no ROS2 needed

# Common CLI flags
"$UNILAB_ENV/bin/unilab" --app_bridges websocket fastapi    # communication bridges
"$UNILAB_ENV/bin/unilab" --test_mode                        # simulate hardware, no real execution
"$UNILAB_ENV/bin/unilab" --check_mode                       # CI validation of registry imports
"$UNILAB_ENV/bin/unilab" --skip_env_check                   # skip auto-install of dependencies
"$UNILAB_ENV/bin/unilab" --visual web                      # rviz / web / disable
"$UNILAB_ENV/bin/unilab" --is_slave                         # run as slave node

# Workflow upload subcommand
"$UNILAB_ENV/bin/unilab" workflow_upload -f <workflow.json> -n <name> --tags tag1 tag2

# Tests
"$UNILAB_ENV/bin/python" -m pytest tests/
"$UNILAB_ENV/bin/python" -m pytest tests/resources/test_resourcetreeset.py
"$UNILAB_ENV/bin/python" -m pytest tests/resources/test_resourcetreeset.py::TestClassName::test_method
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

## Workflow, Runtime, and Debugger Invariants

### Authority and Boundaries

- Uni-Lab-OS is the execution authority. Frontends and `local_bridge` are
  transports/projections; they do not own scheduling, node terminal states,
  debug state, resource admission, or reconciliation.
- `WorkflowRevision` schema v2 is the lossless authoring/execution source.
  Compile the complete immutable revision once, including branch/join and other
  control nodes. Never execute a graph reconstructed from a frontend canvas.
- `unilabos/scheduler/DagWalk` owns dependency state, `DagExecutor` owns async
  admission/execution, and `DebugController` gates admission. Do not duplicate
  any of those responsibilities in `runtime`, `local_bridge`, or UI adapters.
- The offline bridge must reuse the same `TaskDag`, `DagExecutor`, resource
  locking, journal, and debug path as the normal OS path. “Offline” may replace
  process/transport boundaries, never execution semantics.

### Unified Frontend Contract

The maintained frontend boundary is:

- `GET|PUT /api/v1/workflows/{workflow_id}/graph`
- `POST /api/v1/workflows:validate`
- `POST /api/v1/authoring/compile`
- `POST /api/v1/authoring/generate-python`
- `POST /api/v1/authoring/validate`
- `POST /api/v1/runtime/runs`
- `GET /api/v1/runtime/runs/{run_id}`
- `GET /api/v1/runtime/runs/{run_id}/nodes`
- `GET /api/v1/runtime/runs/{run_id}/events?after_seq=...`
- `POST /api/v1/runtime/runs/{run_id}/commands`
- `POST /api/v1/runtime/runs/{run_id}/cancel`
- `WS /api/v1/runtime/events?run_id=...&after_seq=...`

Legacy `/api/run` and `/api/runtime/local/*` may remain as HTTP adapters.
The old Cloud panel `/ws/workflow/{uuid}` server and port 8891 are removed and
must not be reintroduced. `task_dag`/`job_status` remains the OS schedule wire,
not a frontend API. Keep public field spelling and casing stable; translate
only at the boundary.

Runtime Action Catalog entries come from current Graph instance IDs combined
with their Registry contracts; live HostNode instance mappings override those
contracts once available. Driver initialization/online state is capability
state and must not erase a configured instance's authoring contract. The
loopback-only `GET /internal/v1/runtime-actions` exposes this merged snapshot.
The local bridge refreshes it after `host_ready`; a genuinely new action first
seen in the existing `report_action_lock` wire invalidates the projection and
causes another ETag-guarded HTTP fetch. Busy/free flips for known actions must
not refetch schemas. HostNode must report only genuinely new remote actions as
free; re-registration must never reset an existing busy lock. The bridge must
clear the prior live catalog if refresh fails. Profiles may contribute explicit
contracts, but frontend files, demo catalogs, stale caches, and workflow
payloads are never action authorities.
The application-level `ping`/`pong` used by `host_node.test_latency` belongs to
the schedule wire and is separate from WebSocket keepalive frames.

### Authoring Safety

- Python workflows are parsed and compiled from AST through
  `unilabos.workflow.from_python_script`; user source must never be imported,
  evaluated, or executed to construct a DAG.
- JSON → Python, Python → Canonical, and candidate validation are separate
  fail-closed operations. A failed candidate must never replace the last valid
  revision or be saved/run.
- Source maps must cover every stable `node_id`, including compiler-created
  control nodes. Generated Python must make implicit joins visible with a stable
  comment/location so code, DAG, breakpoints, and diagnostics remain aligned.
- Never manufacture a successful diagnostic, revision id, source map, or action
  catalog entry to make a client accept invalid source.
- `--test_mode` may skip physical dispatch, but its successful return value
  must still conform to the selected action's Registry result schema. Never add
  undeclared framework metadata such as `test_mode` or `action_name` to device
  outputs; expose simulation state through logs/capabilities instead.

### Start Point and Breakpoint Semantics

- `start_node_id` is run configuration over the complete DAG. `DagWalk` computes
  the reachable subgraph, marks all other nodes and edges `SKIPPED`, journals the
  result, and treats the selected node as a new boundary. Never ask the frontend
  to physically crop the graph or merely paint excluded nodes gray.
- Breakpoints pause before node admission. `DebugController` must remain before
  resource lease acquisition and device action queueing; a paused-before node
  owns no newly acquired resource and has not been dispatched.
- A pause request stops new admissions and lets already-running physical
  actions drain to terminal state. Never freeze a driver midway and call that a
  safe debugger pause.
- `step` admits exactly one logical ready node and pauses again after that node
  reaches terminal/quiescence. In debugger v1, `step_over` and `step_into` are
  explicit aliases of `step`; do not claim nested-frame semantics until such a
  model exists.
- Continuing or stepping past a hit breakpoint bypasses that breakpoint once;
  it does not delete it. `start_node_id` is immutable for an existing run.
- Commands are run-scoped and use the unified command endpoint. Unknown nodes,
  malformed payloads, and invalid transitions must fail closed with a stable
  structured error.

### State, Events, and Exceptions

- The scheduler/journal is the truth. HTTP acceptance, successful WS delivery,
  dispatch acknowledgement, or a lagging bridge projection is not a terminal
  execution result.
- Events use monotonically increasing sequence numbers and support replay after
  a cursor. Never overwrite or renumber history to fit a frontend view.
- Preserve `dispatch_unknown`, `reconciling`, cancellation, resource-waiting,
  and fenced states. Do not coerce them to success/failure or redispatch an
  uncertain physical action without reconciliation.
- Exactly one layer owns each run/node terminal event. Projection/read APIs
  never write terminal journal entries.
- Failure remains fail-fast for unresolved descendants, but already-dispatched
  physical work must be cancelled/fenced through the device/runtime path rather
  than hidden by an in-memory state change.

## Local Material Service Invariants

### Authority and Contract Boundary

- `unilab -g/--graph` loads the one mutable `ResourceTreeSet` owned by the OS
  process. `CurrentMaterialState` holds that same object, not a copy or second
  database. Internal OS resource operations may modify it.
- `material_api.py` caches only a read-only schedule snapshot of the current OS
  memory state. Every HTTP material read refreshes from OS; GET/UI commands
  cannot mutate `ResourceTreeSet`. A graph file is only a startup input and
  must never be reread as runtime authority.
- The local server and the Go backend deliberately share list/detail path
  spelling and pagination shape where possible. This does not make their full
  semantics identical: the local server returns a projected aggregate in
  `config`, while the Go backend currently exposes persistent Material,
  RelativePosition, Site, and MaterialStateHistory records separately.
- Add a capability only after its complete semantics exist. A local projection
  must remain read-only until create/update/move/attach/detach/undo have a
  revisioned, idempotent, compensatable command contract; do not compose those
  semantics from unrelated row CRUD endpoints.
- Local profiles are singleton-scoped and do not require `laboratoryId`.
  Multi-laboratory scope belongs to a future cloud adapter, not the local bridge.

### Graph, Placement, and State

- Stable UUIDs are derived from the public graph identity and source node id;
  graph node ids remain the source trace. Do not expose host paths as public ids.
- Persistent placement is low-frequency configuration. A material attached to
  a site follows the site's current link pose in the scene, but joint updates
  must not rewrite the site or static relative position.
- High-frequency joint state belongs on a separate realtime channel. General
  time-series material state belongs in backend `material_state_history`;
  missing joint state may fall back to URDF initial values.
- A domain Site is a carrier/deck/hotel mounting location that can hold another
  material. Legacy graph Well/TipSpot nodes are temporarily projected inside
  `config.sites` for rendering compatibility, but they are labware internals,
  not the long-term Site contract. Do not add Site mutation/business semantics
  around that compatibility representation.
- Reagent, sample, current substance, and container content are backend domain
  records. Do not preserve or expand arbitrary graph `data` as a universal
  state model.

### Models and Rendering Assets

- `material_models.py` is the only local model registry. Model definitions are
  validated at bridge startup and public URLs are resolved beneath
  `unilabos/device_mesh`; absolute paths and directory traversal are forbidden.
- Register reusable XACRO/URDF/mesh assets by stable identity tokens. Do not
  edit a device model, pose, dimensions, or joint defaults merely to make one
  screenshot/test graph look correct.
- The frontend owns camera fitting and 2D/2.5D presentation. The OS exports
  authoritative geometry/model metadata; it must not send case-specific camera
  coordinates, UI colors, or Pascal scene overrides.

### Resource Template Catalog

- The loaded Registry is the only Edge template authority. Device/resource YAML
  may add declarative `catalog` metadata, but `template_catalog.py` must project
  the already-built Registry; it must not scan a second directory or derive
  templates from the current `-g` Material Graph.
- Public identity is UUID5 of
  `unilabos:resource-template:v1:{source_namespace}:{kind}:{key}`. It is
  independent of file path, load order, display name, and process lifetime.
- Resources are public by default; devices are internal by default and require
  an explicit `catalog.visibility: public`. Never publish driver module paths,
  action schemas, credentials, host paths, or arbitrary Registry internals.
- The internal Registry API is loopback-only on the OS web server. The browser
  consumes only the `local_bridge` projection on `:8014`; keep the bridge URL
  to the execution server explicit and do not discover it from a schedule WS.
- List responses contain lightweight summaries. Geometry, container layout,
  configuration and declared assets are lazy detail data. Assets must be
  explicitly named and confined beneath their declaration YAML directory.
- ETag/revision and the bridge's short-lived memory cache are read
  optimizations, not a second catalog authority. If the execution server is
  temporarily unavailable, cached data may be returned with `stale=true`, but
  all creation metadata must be disabled. No cache means a structured 503.
- Template catalog and current Material Graph are separate domains. A template
  describes a type that could be instantiated; it is never evidence that an
  instance exists in OS memory.

## Absolutely Forbidden

- Do not run OS tests, migrations, compilers, or servers with system Python.
  Use the `unilab` Python 3.11 environment. On this host the guaranteed
  interpreter is `/home/changjunhan/.micromamba/envs/unilab/bin/python`;
  do not assume the `micromamba` command itself is on `PATH`.
- Do not execute user-authored workflow Python with `eval`, `exec`, import, or a
  subprocess.
- Do not add a second DAG scheduler/debugger in `local_bridge`, runtime service,
  a device driver, or a frontend-specific adapter.
- Do not pause after taking a resource lease or enqueueing a physical action.
- Do not crop the submitted DAG to implement `start_node_id`.
- Do not infer success from transport success or fabricate device feedback.
- Do not weaken cycle checks, binding validation, action catalog validation,
  source-map coverage, safety fences, or terminal-event uniqueness to pass a test.
- Do not expose local bridge ports beyond loopback by default. A public bind
  requires explicit authentication, authorization, origin, and deployment review.
- Do not turn `local_bridge` into a material database or copy the Go backend's
  repository/service stack into OS.
- Do not advertise material write capabilities that only partially update a
  Material/RelativePosition/Site graph.
- Do not treat Well/TipSpot compatibility projections as long-term domain Site
  rows.
- Do not rewrite Site/RelativePosition from high-frequency joint updates.
- Do not expose arbitrary files through the material asset endpoint or weaken
  asset-root traversal checks.
- Do not hard-code one experiment graph's camera, occupancy, dimensions, or
  model transform into a supposedly generic contract.
- Do not restore frontend-bundled/Cloud template JSON as a fallback for Edge,
  publish all devices by default, or couple catalog availability to a schedule
  WebSocket session.
- Do not treat an ETag cache hit or stale cached catalog as permission to
  create a device/resource.
- Do not hard-code a Registry method into the frontend or demo action catalog
  to bypass `ACTION_NOT_FOUND`, or retain a live Action Catalog across a failed
  OS reconnect.

## Workflow-Focused Verification

Run relevant suites in the `unilab` Python 3.11 environment:

```bash
UNILAB_PY=/home/changjunhan/.micromamba/envs/unilab/bin/python
"$UNILAB_PY" -m pytest tests/workflow
"$UNILAB_PY" -m pytest tests/scheduler
"$UNILAB_PY" -m pytest tests/runtime
"$UNILAB_PY" -m pytest tests/app
```

Material projection/model changes must run `tests/app/test_material_api.py` and
must verify list/detail pagination, stable ids, declared site geometry, model
registry startup, asset traversal rejection, and frontend real-OS 2D/2.5D/3D/
Split integration. Do not modify the frontend or backend fixture to hide a
contract mismatch.

Template catalog changes must also run
`tests/registry/test_template_catalog.py`,
`tests/app/test_resource_template_internal_api.py`, and
`tests/app/test_resource_template_proxy.py`. Verify public visibility defaults,
stable UUID/revision, lazy details, asset confinement, ETag revalidation,
cache isolation and fail-closed stale behavior.

For contract changes, add tests for both the canonical/runtime layer and the v1
HTTP/WS projection. Test complete control-flow DAGs, source-map round trips,
start-node skipping, breakpoint-before-admission, one-node stepping, event
replay, exception propagation, cancellation, and reconciliation. If a referenced
sibling-repository fixture is unavailable, report that environmental gap; do
not “fix” it by deleting or weakening the contract assertion.

## Licensing

- Framework code: GPL-3.0
- Device drivers (`unilabos/devices/`): DP Technology Proprietary License — do not redistribute
