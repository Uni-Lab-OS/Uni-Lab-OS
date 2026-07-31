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

**Resource Tracking** (`unilabos/resources/resource_tracker.py`): Pydantic-based `ResourceDict` → `ResourceDictInstance` → `ResourceTreeSet` hierarchy. `ResourceTreeSet` is the canonical in-memory representation used by devices and PLR conversion, but is not the durable Material authority. Graph I/O is in `resources/graphio.py` (reads JSON/GraphML device topology files into `nx.Graph` + `ResourceTreeSet`).

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

## Workflow Migration Round Gate

These rules are mandatory for every implementation round in the
WorkflowTask/runtime migration:

- Treat one mergeable plan slice as one round. Create a fresh
  `migration/<round>-<topic>` branch from the latest
  `integration/workflow-task-runtime` before changing production code. Never
  stack the next round on an unmerged round branch.
- The existing `migration/01-backend-contract` work is one legacy-named round.
  It must pass this same gate before merge; after merge, continue 01E and later
  slices on fresh round branches.
- Before implementation, assign exactly one independent test-author subagent
  for that round. Give it a separate Git worktree and `test/<round>-*` branch.
  Do not run another subagent concurrently. It must commit tests that fail for
  the intended missing behavior before implementation.
- Bring the independent test commit onto the round branch without squashing
  its provenance. The implementation must make that suite pass; do not weaken,
  delete, skip, or xfail an independently authored test merely to make the gate
  green.
- Before review, run the round-target tests, the complete repository test
  suite, configured lint/static checks, and `git diff --check`. Every test must
  pass; a partial suite is not merge evidence.
- Pin review to the exact tested commit SHA. A review round uses exactly one
  independent review subagent that did not author the implementation or that
  round's tests. Rotate decision/spec, module-design, and regression/security
  perspectives across sequential review rounds; never run reviewers
  concurrently. Reviewers must inspect code and tests, not only test output.
- A round may merge only after every blocking review finding is fixed, all
  affected tests and the full gate are rerun, and the relevant reviewers
  confirm the fixes. Any code change after the reviewed SHA invalidates the
  corresponding review.
- Record the tested SHA, commands/results, test-author subagents, reviewer
  subagents, and finding disposition in the migration ledger. Merge locally
  into `integration/workflow-task-runtime` with reviewable history; do not
  squash migration provenance and do not push without explicit authorization.

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
- `WorkflowNode.disabled` is the only user-selected way to omit an individual
  Node from execution. It is editable Workflow definition state, advances the
  Workflow revision when changed, and affects every Task created from that
  saved definition until re-enabled.
- Do not add Task-scoped `skip_node_uuids`, an initial-skip launch option, or a
  `skip` Task command. A Scheduler-derived `WorkflowNodeJob.skipped` result and
  a Node outside a debug start scope are not aliases for a disabled Node.
- Exactly one scheduler owns readiness, admission, node terminal state, and
  task completion. Bridges, HTTP handlers, WebSocket sessions, device drivers,
  and frontend projections must not implement another DAG walker or debugger.
- Transport acceptance, dispatch acknowledgement, HTTP success, and WebSocket
  delivery are not node terminal results. An uncertain physical action must
  remain fenced until explicit query/reconciliation establishes its state.
- Mirror Backend's `normal`, `step`, and `single_node` run modes and
  `step`/`pause`/`resume`/`cancel` Task commands before adding debugger
  behavior.
- Persisted breakpoints, partial-DAG start nodes, and source-map runtime
  highlighting are explicit OS-only extensions in this migration. Use
  WorkflowTask/WorkflowNode UUIDs and the Node-only state machine; do not
  restore Run identities, persisted Edge state, or a frontend DAG walker.
- Frontend support for OS-only debugging comes from explicit Authority
  configuration. Do not add a capabilities endpoint, append extension fields
  to Backend shared requests, or infer support from a failed request.
- A Debug Launch Configuration contains only a non-empty start frontier and
  breakpoint Nodes. They are WorkflowTask-scoped debug configuration, not
  editable Workflow graph fields, and never advance Workflow revision.
- Represent the start frontier only as `start_node_uuids`; do not retain a
  singular `start_node_uuid` alias. Its order has no execution meaning.
- Compute debug start scope by directed reachability after excluding disabled
  Nodes: the active subgraph is the union of every frontier Node and its
  reachable descendants, with only Edges whose endpoints are both active.
  Never use topological position as a substitute for reachability.
- Nodes retained in the WorkflowTask snapshot but outside that active subgraph
  are out of scope. They receive no WorkflowNodeJob and must not be rewritten
  as disabled or skipped.
- Validate required Node inputs after constructing the active debug subgraph.
  Debug Task creation returns `400 invalid_input` without persisting a Task
  when an active Node has neither a non-null compatible `param` value, an
  in-scope Edge, nor a declared Workflow input binding resolved from validated
  Task input supplying a required target Handle.
- Never fill an input cut by debug scope from an earlier Task/Job, transient
  frontend state, or an undeclared/arbitrary WorkflowTask input key. The caller
  must save a valid Node parameter, declare and supply a valid Workflow input
  binding, change the graph, or choose an earlier start frontier.
- A frontend that changes Node participation before debugging must first save
  the Node's `disabled` value through the ordinary graph-editing Interface,
  then create the debug Task from the resulting persisted Workflow.
- At debug Task creation, atomically persist the initial start frontier,
  breakpoint Node UUIDs, exact Workflow snapshot, applied source hash, and
  matching source map. Later Workflow/source edits must not alter the Task's
  debug scope or runtime highlighting.
- Persist Task debug configuration across OS restart. If the saved graph has no
  applied source/source map, DAG debugging remains valid but code highlighting
  is unavailable.
- A breakpoint creates a Node-local admission hold, not a Task-global pause or
  a persisted Branch state. Continue admitting unrelated ready Nodes; normal
  dependencies keep the held Node's descendants and downstream convergence
  Nodes waiting.
- A Task may remain active while one or more breakpoint holds exist. Keep the
  global `pause` command distinct: it blocks new admission across the whole
  Task. Neither operation cancels a Job that was already running.
- Persist breakpoint hits/holds across restart and project them by `node_uuid`.
  Do not compute, store, or synchronize a mutable set of "frozen branch"
  Nodes or Edges.

### Workflow Graph Concurrency

- Mirror Backend's `Workflow.revision` contract. A new Workflow starts at
  revision `1`; every WorkflowNode or WorkflowEdge create, update, or delete,
  including full-graph `PUT` and batch delete, increments
  `Workflow.revision` and `Workflow.update_time` in the same transaction.
- Graph reads expose the current revision. Full-graph `PUT` must carry the
  revision observed by its caller, reject a mismatch atomically with `409`, and
  return the reconciled graph with its revision incremented.
- Entity `update_time` values are audit data, not graph concurrency tokens.
  Ordinary presentation metadata `PUT` does not increment graph revision.
  Reserved `unilab` input/output contracts, root output bindings, and Node
  input bindings are the OS-only exception: change them only through atomic
  Authoring Apply and advance graph revision.
- `POST /api/v1/workflow-tasks` carries no expected revision. It snapshots the
  persisted graph observed when the request is handled.

### Workflow Graph Editing

- Do not add `PATCH /api/v1/workflows/{workflow_uuid}/graph`. Use
  `PATCH /api/v1/workflow-nodes/{node_uuid}` for routine partial Node edits;
  omitted fields remain unchanged and explicit `null` clears nullable fields.
  Retain Backend's complete Node `PUT` Interface, but do not use it as the
  ordinary frontend property-edit path.
- Create Nodes and Edges through their workflow-scoped `POST` routes. Update an
  Edge completely with `PUT /api/v1/workflow-edges/{edge_uuid}`. Delete
  individual entities with `DELETE`, or use the workflow `batch-delete` route
  for a multi-entity deletion.
- Use revision-guarded full-graph `PUT` for ordinary explicit graph saves,
  Backend-bound Python candidates, JSON/Python synchronization, and multi-Node
  or control-structure graph changes. Its body contains only `revision`,
  stable-UUID `nodes`, and stable-UUID `edges`; Workflow ownership comes from
  the path. An OS-local Python Apply uses the OS-only atomic Authoring
  Interface so reserved contract metadata, graph, source, and source map commit
  together; never add those fields to the shared graph PUT body.
- Full-graph `PUT` reconciles by UUID and soft-deletes omitted entities. It
  must not delete and recreate unchanged entities.
- Graph writes never start execution. Run with dirty editor state first commits
  it through the applicable graph-save or OS Authoring Apply Interface, then
  separately calls `POST /api/v1/workflow-tasks`.

### Frontend Runtime Transport

- Mirror Backend's frontend realtime interface with the single
  `GET /api/v1/events` SSE stream. Task/Job state, Job feedback, intervention
  notifications, and Edge status notifications all use this stream and resume
  with `Last-Event-ID`.
- Do not add a frontend WorkflowTask WebSocket. `/api/v1/edge/ws` is an
  internal Backend-to-OS Edge control channel, not a frontend interface.
- SSE is a projection, not a state authority. Re-hydrate durable state through
  REST, and submit Task commands and intervention decisions through their REST
  write endpoints.

### Frontend HTTP Response Envelope

- Mirror Backend's frozen JSON envelope. Success with a body is
  `{"code": 0, "data": ...}`; errors are
  `{"code": <http_status>, "error": {"code": "...", "message": "..."}}`;
  successful deletes generally return an empty `204`.
- Preserve the two frozen `09609a2` frontend-visible route exceptions:
  ResourceTemplate deletion returns HTTP `200` with
  `{"code": 0, "data": {}}`, and an invalid `/api/v1/events`
  `Last-Event-ID` returns the route's unwrapped `{"error": ...}` body. Do not
  generalize either exception to other routes.
- Normalize FastAPI request-validation and handler errors at the HTTP seam.
  Never expose framework `detail` bodies or naked Workflow, Task, or authoring
  objects.
- Authoring diagnostics from a well-formed pure transformation request are
  successful `data`, even when the Draft has syntax/semantic errors and no
  Candidate Workflow. Only malformed requests or service/infrastructure
  failures use the error envelope.
- Do not wrap SSE frames in the JSON response envelope.

### Frontend Interface Authority

- For capabilities shared with Backend, frontend, CLI, and MCP callers switch
  only the base URL. Keep paths, methods, DTOs, envelopes, errors, and business
  meaning identical.
- Freeze the read-only Backend frontend contract at
  `feat/workflow@09609a2`. Frozen frontend-facing Handler DTOs, Service
  behavior, public-route tests, and Models at that commit outrank older
  interface documents when they disagree.
- Scope Backend parity only to interfaces observable by frontend clients,
  including workflow authoring, template/material/action data used by the
  editor, Task/Job commands and queries, feedback, manual confirmation,
  intervention, and the frontend SSE stream.
- Do not derive the OS frontend Interface from Backend-to-Edge communication.
  `/api/v1/edge/*`, Edge HTTP/WebSocket protocols, registration, Job tokens,
  Command/Inbox, ACK/replay, session reconciliation, device execution locks,
  PostgreSQL advisory locks, and the Backend process split are internal
  Backend implementation and are outside the parity boundary.
- An event delivered through the frontend SSE stream remains in scope even
  when its producer is an Edge Agent. Only the Backend-to-Edge transport and
  implementation that produced it are excluded.
- Treat effective Workflow input as an explicit OS-only execution extension
  until Backend supports it. A Workflow defines an ordered input contract, a
  WorkflowTask supplies run-scoped `input`, and Node bindings may consume those
  values.
- Keep the version-1 Workflow Input/Output value vocabulary finite and shared:
  `str`, `int`, `float`, `bool`, opaque `dict[str, JSONValue]`,
  `ResourceSlot`, and one-dimensional homogeneous `list[T]` for each of those
  non-container base types. Allow `list[dict[str, JSONValue]]` and
  `list[ResourceSlot]`, but no declared nested or heterogeneous lists.
- Treat JSON objects as opaque recursively valid JSON edited as a whole. Do not
  infer a typed object form, accept arbitrary Python/Pydantic models, or hide a
  ResourceSlot inside an opaque object. Reject `Any`, bare `object`, tuple, set,
  arbitrary unions, bytes/files, datetime, and Decimal in version 1.
- Validate Workflow values strictly and identically for Python defaults, Draft
  compilation, Task input/snapshotting, and Workflow output. Never parse
  strings as numbers/booleans, treat `0`/`1` as booleans, accept a bare UUID as
  ResourceSlot, or coerce list elements.
- Only at the declared top-level `POST /api/v1/workflow-tasks` input boundary,
  normalize explicit null as omission before filling defaults and checking
  required inputs. Do not extend that equivalence to unknown keys, nested
  opaque JSON, list elements, PATCH semantics, or Workflow outputs. Persist
  only the complete normalized input in the immutable Task snapshot.
- Keep Workflow Input/Output validation constraints finite: scalar `enum`,
  inclusive numeric `minimum/maximum`, string `minLength/maxLength`, list
  `minItems/maxItems`, and ResourceSlot
  `allowed_resource_template_uuids`. Treat `title/description` as presentation
  only and reject every unsupported JSON Schema keyword instead of silently
  ignoring it.
- Keep Workflow contract envelopes, parameter descriptors, value schemas, Task
  input, final Workflow output, and external ResourceSlot references closed.
  Reject unknown fields instead of dropping them; external ResourceSlot values
  contain only `uuid`. Arbitrary keys remain legal only inside a value
  explicitly declared as opaque `dict[str, JSONValue]`, and closing one
  contract must not remove defined sibling metadata.
- Workflow outputs have no `required` or `default` fields. Every declared key
  must resolve before Task success; nullable outputs still require an explicit
  key whose value may be null. Validate the complete final output before the
  terminal success transition and never publish an invalid result as
  successful. ResourceSlot pass-through is an explicit runtime-produced
  Binding, not an output default.
- Parse Workflow and Action function parameters through one strict
  annotation-to-schema implementation. Types come from annotations and actual
  defaults come from `=`, never from `Field(default=...)`. Allow both Pydantic
  Field and Uni-Lab parameter docstrings to supply title/description; after
  trimming, use the sole non-empty source and let a non-empty Pydantic Field
  value win a conflict. Workflow parsing remains AST-only and must not inherit
  the Action parser's unknown-type fallback.
- Accept both `Optional[T]` and `T | None` in Action/Workflow annotations but
  generate only `T | None` in normalized Workflow Python. Nullable Workflow
  input must be declared as `T | None = None`; reject required-nullable input
  and a null default on non-null `T`. Preserve the semantic distinction between
  nullable collections and empty collection defaults.
- Pydantic Field is optional. When present in an Action/Workflow `Annotated`
  parameter, accept only `ge/le`, type-directed `min_length/max_length`, and
  `title/description`; reject all other Field arguments and keep actual
  defaults after `=`. A bare supported type has no numeric/length bounds.
- Restrict ResourceSlot templates with
  `AllowedResourceTemplates(@resource_symbol, ...)`, never a ResourceSlot
  instance or hard-coded UUID in `json_schema_extra`. Resolve symbols
  statically through the selected catalog to the Applied UUID allowlist,
  without executing Workflow source or resource factories; fail closed on
  unresolved/stale symbols. An absent annotation means unconstrained.
- Use `Literal[...]` as the sole scalar enum syntax. Infer one strict
  string/boolean/integer/number family, reject null, illegal mixtures,
  duplicates and non-finite members, and preserve declaration order. Nullable
  wraps Literal; homogeneous lists may use Literal items but never nullable
  items. Run D-083 base-type validation before membership because Pydantic
  Literal validation can equate booleans and integers.
- Normalize every device selector as a module-scope annotated assignment:
  `selector: DeviceTemplate = device()` means independently schedule a matching
  instance for each Node/Job; `selector: DeviceTemplate = device("device-id")`
  pins every represented Node to that registered instance. Reject unannotated
  selectors and every other `device(...)` argument shape.
- Resolve the device annotation, `@device` template, and called `@action`
  statically against the selected Backend-issued Template Catalog. Never import
  or execute authoring source, decorators, device classes, or drivers to
  compile a Workflow. Reusing one unbound selector never creates an implicit
  Task-level device lease or affinity.
- Persist a fixed selector only in reserved
  `WorkflowNode.meta_data.unilab.executor_binding`; do not add a top-level
  device field to the Backend-shaped Node. Snapshot it with the Task, treat it
  as graph-semantic, and make a busy fixed instance wait rather than silently
  selecting another device.
- Record the admitted concrete instance in
  `WorkflowNodeJob.meta_data.unilab.executor_assignment`. Project assignment,
  ordinary Task/Job state, and feedback to the frontend through D-025's single
  `/api/v1/events` SSE stream; never add a frontend WorkflowTask WebSocket.
- Generate an ephemeral action-only Python typing projection from the same
  authority/fingerprint-scoped Catalog used by compilation. It must hide
  `@not_action`/driver internals, expose typed parameters and generated named
  result attributes including implicit ResourceSlot pass-through, and serve
  Monaco, IDE, CLI/MCP, and coding-agent completion. Static language results
  are advisory; Compile Preview remains the fail-closed semantic authority.
- Accept finite integer or fractional JSON numbers for `number`, but exclude
  booleans. Accept only a mathematical integer for `integer`, exclude booleans,
  and normalize `3.0` to `3`. Reject NaN/infinities and non-JSON object values.
  A Backend-shaped WorkflowTask input type failure is `400 invalid_input`
  before any Task/Job write.
- Validate and resolve all WorkflowTask input before creating or dispatching a
  Job. Persist the resolved values in the immutable Task snapshot and never
  mutate the saved Workflow or WorkflowNode `param` for one run.
- Persist the versioned ordered contract only at
  `Workflow.meta_data.unilab.input_contract` and per-Node bindings only at
  `WorkflowNode.meta_data.unilab.input_bindings`. Binding keys are real target
  WorkflowHandleTemplate UUIDs from the selected Graph Authority.
- Use `ResourceSlot` as the sole Workflow type for business materials across
  root Task input, Node Handles, parent calls, and statically inserted
  subworkflows. Do not introduce `MaterialRef` or convert a ResourceSlot merely
  because it crosses a subworkflow scope.
- Accept an external WorkflowTask ResourceSlot only as a Material Authority
  reference object containing its stable `uuid`; do not require the legacy
  local `id`, and do not accept a caller-supplied flattened resource tree.
  Inside OS execution, a Handle may still carry either an authority reference
  or the existing flattened single-root resource list. Normalize both through
  one shared ResourceSlot resolver at the consuming Handle/action boundary.
- Preserve a ResourceSlot Binding unchanged when statically expanding a
  subworkflow. Do not eagerly resolve, copy, wrap, or serialize it at the group
  boundary.
- Express optional ResourceSlot template restrictions with Backend's existing
  `allowed_resource_template_uuids` spelling. Omission accepts any Material
  template; a present list must be non-empty, UUID-valid, and unique. Match
  version 1 only by exact `Material.resource_template_uuid`, never by name,
  tag, class, hierarchy, package, or Python inheritance.
- Resolve and validate an externally submitted ResourceSlot UUID through the
  selected Material Authority before creating the WorkflowTask or any Job.
  Return `404 not_found` for a missing or soft-deleted Material,
  `400 invalid_input` for a template mismatch, and `409 conflict` for a stable
  non-runnable disposition. Frontend selector filtering never replaces this
  validation.
- For an OS-local Task, the authority receiving `POST /workflow-tasks` is also
  the sole Material Authority. It must use the durable local Material module;
  never accept a separate authority selector, proxy/fallback to Backend, or
  execute a Material-backed Task after merely warning that Inventory is absent.
- Keep exactly one durable Material truth. Deepen the reviewed Inventory
  transaction engine into the local Material module; do not add another
  Material table to Workflow storage. Treat `ResourceTreeSet` as a controlled
  execution projection and never synchronize it with SQLite by polling,
  last-write-wins, or unrestricted two-way writes.
- Refresh affected ResourceTreeSet roots from persisted Material state before
  action dispatch. After a confirmed action success, serialize and validate
  affected roots, commit them idempotently by `job_uuid + attempt` with
  optimistic versions/ledger/outbox, and only then persist Job success.
  Projection refresh failure makes the projection stale and blocks affected
  actions; it never authorizes stale memory to overwrite durable state.
- Never roll an old ResourceTreeSet snapshot over a physical action that may
  already have side effects. Fence or quarantine affected Materials on
  post-dispatch failure, `dispatch_unknown`, or pending persistence, and require
  device or human reconciliation before downstream consumption.
- A device graph may seed an empty Material authority only through an explicit
  one-time import. It must not overwrite persisted Material state on restart.
  High-frequency joint/telemetry state remains outside Material persistence.
- Use one Material UUID. A Material owns `uuid`; all relationships, Workflow
  fields, Inventory rows, runtime locks, and local reference variables use
  `material_uuid`. Remove `edge_uuid`, `legacy_cloud_id`, graph-derived Material
  UUIDs, and `instance_uuid` aliases from the migrated path; explicit imports
  preserve the source UUID and one-time migration rewrites all dependents.
- Backend `Material.code` means the laboratory barcode. OS may name and persist
  the single domain value `barcode`, but the shared HTTP DTO projects it as
  `code`. Never store independently mutable `code` and `barcode` values or hide
  a second barcode in config/data. Create and full update require a non-blank
  barcode, case-insensitively unique among non-deleted Materials.
- The common Material identity table may contain both devices and business
  resources, but their operational state stays separate. Inventory
  lot/reservation/consumption/quarantine state applies only to business
  Materials; device online/health/action locks remain in DeviceState/Scheduler.
  Both are keyed by the same `material_uuid`.
- Persist business-Material disposition as exactly `active`, `consumed`,
  `discarded`, `quarantined`, or `reconciling`. Derive `reserved` and `in_use`
  from Task Material Reservations and Job Execution Claims; never store them
  as competing Material status. A reconciling Material remains durably fenced.
- Persist Backend-shaped Sites in the durable Material module with stable Site
  UUIDs, owner `material_uuid`, optional `occupied_material_uuid`, ordering,
  template allowlist, and geometry. `Material.parent_uuid` is composition only;
  Site occupancy is placement only. Never derive one from the other.
- Retire `resource_relation.slot_id` as writable persistent truth after its
  one-time migration into Site occupancy. Map persisted Site names/order into
  the ResourceTreeSet/PLR execution projection, but never use a PLR resource
  name as durable identity. Shared frontend relationships use Site UUIDs.
- Keep `lab_zone`/`lab_placement` as independent 2D layout. They are not Sites,
  do not own Material occupancy, and do not provide scheduler locks.
- Separate durable Task Material Reservations from durable Job Execution
  Claims. Task creation atomically attempts to reserve accepted business
  Material identities and quantities under `workflow_task_uuid`; transient
  contention persists the Task as pending without a partial reservation and
  the coordinator retries. A ready Job atomically claims its selected device
  Material UUID, every mutable business Material UUID, and each Site UUID whose
  occupancy it may change under `job_uuid + attempt`.
- Acquire a Job's complete claim set or none, using stable UUIDs and a
  deterministic order. Version 1 locks the selected device instance, not
  `device_id + action_name`. A claim, executor, or other transient admission
  conflict keeps the Job pending rather than failing it. Do not infer
  production claims from parameter names or runtime value shapes.
- Release execution claims only after pre-dispatch failure or after affected
  Material/Site state commits. Cancellation, `dispatch_unknown`, and unresolved
  post-action persistence must retain a durable fence through restart; Task
  reservation release waits until all dispatched/unknown Jobs are settled.
- Site occupancy is persistent placement state, not a scheduler lock.
  Process/SQLite/ROS mutexes are implementation guards and never replace Task
  Reservations or Job Execution Claims.
- Soft-delete Materials and exclude them from normal reads. Reject deletion
  with `409 material_in_use` while an active Reservation, Claim, uncertainty
  fence, or live Site/Material relationship protects the Material. Never
  silently unlink a live runtime reference during deletion.
- During static subworkflow composition, intersect a parent ResourceSlot
  parameter's template allowlist with every bound child-input allowlist;
  omission is the universal set for this operation. Persist and expose the
  effective parent constraint in Compile Preview/Apply. Reject an empty
  intersection at compile/Apply time without changing persisted Workflow
  state; do not defer it to Task creation.
- Use the existing Backend-shaped `WorkflowTask.workflow_snapshot` to freeze
  the Graph/input contract/bindings and `WorkflowTask.input` to freeze the
  complete validated/default-resolved run input. Canonical ResourceSlot input
  is `{uuid, resource_template_uuid}`, with the template UUID supplied by the
  Material Authority rather than trusted from the request. Add no parallel
  snapshot field or adapter.
- Never copy a complete mutable Material tree into WorkflowTask input. Put the
  same canonical reference in directly bound Job parameters and resolve the
  current tree at the consuming Handle/action boundary. If later resolution
  fails, fail the Job before invoking its executor; never run from stale
  Task-time Material state.
- Persist `Workflow.meta_data.unilab.output_contract` as a versioned ordered
  sibling of `unilab.input_contract`. Output names are unique and every v1
  output must resolve. Use the same scalar/ResourceSlot schema vocabulary;
  do not create a pseudo Output Node or virtual WorkflowEdge.
- Persist root output Bindings only at
  `Workflow.meta_data.unilab.output_bindings`, as a graph-semantic sibling of
  the Input and Output Contracts. Copy them into the existing
  `WorkflowTask.workflow_snapshot`; Task completion must never read or
  recompile the live Draft to resolve output.
- Version 1 root output Bindings have exactly two variants:
  `workflow_input` carries an exact Input Contract `parameter`, and
  `node_output` carries both the persisted `workflow_node_uuid` and the real
  source `source_handle_uuid`. Static subworkflow and implicit pass-through
  bindings normalize to those variants. Do not add literal, expression,
  result-path, subworkflow, or implicit variants; derived values require a real
  compute Node.
- Compile one final top-level Python `return workflow_output(name=value, ...)`
  into named output Bindings. Statically inserted subworkflows substitute
  those Bindings directly into the caller; successful root execution resolves
  them into Backend's existing `WorkflowTask.output`.
- Source ResourceSlot output constraints only from an effective pass-through
  input contract, an inserted subworkflow Output Contract, or the selected
  Graph Authority's real output
  `WorkflowHandleTemplate.meta_data.unilab.allowed_resource_template_uuids`.
  Never infer business material from the executor
  `WorkflowNodeTemplate.resource_template_uuid`, names, tags, Python classes,
  or runtime examples.
- Compile a ResourceSlot connection only when the producer template set is a
  subset of the consumer set; omission is the universal set. Thus an
  unconstrained producer cannot feed a constrained consumer. At runtime,
  resolve and check every produced Material against the producer contract;
  contract violation fails the producer Job and prevents downstream dispatch.
- For every Workflow or action ResourceSlot input without an explicit
  compatible same-name output, synthesize a same-name `implicit: true`
  pass-through output with the input's effective template allowlist. Create no
  Node/Edge for it. Scalar inputs do not pass through automatically, and an
  incompatible same-name explicit output is a contract error.
- For a Workflow-capable Action, treat its Python parameter annotations as the
  typed input contract. Explicit named outputs come from either a `TypedDict`
  return type or a frozen standard-library dataclass return type. Also accept
  a literal inline return-annotation dict as non-recommended compatibility
  syntax, but parse it statically and reject dynamic, unpacked, duplicate, or
  unsupported fields. Normalize all forms to one ordered contract.
- Every successful explicit Action result field is present; use `T | None` and
  explicit `None` rather than a missing optional key. `-> None` has no explicit
  output, and a bare opaque `dict` never produces guessed Workflow Handles.
  Declaration-form changes must preserve Handle UUIDs through the normal
  `(workflow_node_template_uuid, handle_key, io_type)` catalog identity.
- After a successful action, merge missing implicit outputs from its resolved
  inputs into the canonical Node output map; drivers need not return them and
  failed actions produce none. A subworkflow implicit output does not require
  a Python assignment target. Optional ResourceSlot pass-through always emits
  its fixed key with a nullable schema and `null` value when absent.
- Materialize an action's implicit output during local template-catalog
  projection, before template sync. Persist it as the same `handle_key`,
  `io_type=source`, `data_source=result`, and same-name `data_key`, marked in
  Handle metadata and carrying the inherited allowlist. Let Backend-shaped
  upsert preserve its real UUID by
  `(workflow_node_template_uuid, handle_key, io_type)`.
- Workflow compilation may only use that persisted real Handle UUID. Never
  generate UUIDv5/per-Workflow Handles, silently sync templates, or persist an
  Edge against an unpublished identity. A stale/missing implicit Handle is a
  Preview diagnostic and Apply failure.
- Model `List[ResourceSlot]` only as an ordered `list[dict]`: every outer dict
  is one independent root Material and may recursively contain
  `children: list[dict]`. Never permit an outer list element or sibling outer
  dicts to encode one flattened tree. Keep single-ResourceSlot flattened lists
  only as migration-only internal input.
- External Task collections contain only `{uuid}` references; their immutable
  Task form is ordered `{uuid, resource_template_uuid}` references. Resolve
  internal collection dictionaries independently, preserve duplicates/order,
  validate the item allowlist per element, distinguish `null` from `[]`, and
  apply same-name implicit pass-through to the entire collection.
- Keep `WorkflowNode.material_uuid` separate: it binds the executor device
  Material, while ResourceSlot values are the samples, reagents, containers,
  plates, or other materials processed by actions.
- Do not create a pseudo Input Node or virtual WorkflowEdge, and do not put
  runtime placeholders in static `WorkflowNode.param`.
- A target Handle may be supplied by exactly one of a static non-null Node
  parameter, an incoming WorkflowEdge, or a Workflow input binding. Reject
  ambiguous providers before Task creation; optional Handles alone may have no
  provider.
- Treat the reserved `unilab.input_contract`, `unilab.output_contract`,
  `unilab.input_bindings`, and `unilab.output_bindings` metadata as
  graph-semantic state. Changes advance `Workflow.revision` and Apply atomically
  with graph, source, and source-map changes; ordinary presentation-metadata
  updates must not overwrite them.
- Backend `09609a2` accepts the `input` wire field but ignores it. Do not claim
  that a Workflow depending on runtime input is executable under Backend
  authority, and never silently discard its values when an unsupported
  authority is selected.
- The OS local micro-backend is an independent selected authority. It must not
  transparently proxy or fall back to Backend, or split one frontend operation
  across local and cloud state. OS-local Scheduler/driver communication,
  locking, recovery, and persistence remain private implementation details and
  do not need to copy Backend's `/edge/*` protocol.
- `/workflows` always denotes static definitions, `/workflow-tasks` executions,
  and `/events` the frontend SSE stream. Never preserve a legacy OS meaning on
  a public Backend path.
- Keep OS-only diagnostics and administration in clearly private namespaces.
  Declare unsupported local capabilities explicitly instead of forwarding them
  silently.
- Do not add a shared `/api/v1/capabilities` endpoint or use capability flags to
  hide missing Backend-defined shared workflow functionality. The OS local
  micro-backend must implement those agreed shared definition, execution,
  query, command, and event Interfaces. Python authoring and the start-frontier
  / breakpoint debugger remain explicit OS-only Interfaces selected by
  Authority configuration. The old `/api/v1/runtime/capabilities` leaves with
  the legacy Runtime/Run routes.
- Pure Python authoring transformation is the explicit exception to base-URL
  parity. Only OS exposes `POST /api/v1/authoring/compile`,
  `POST /api/v1/authoring/generate-python`, and
  `POST /api/v1/authoring/validate`; Backend must not implement or
  transparently proxy them. Frontend, CLI, MCP, and coding agents address OS
  directly, then persist the returned Backend-shaped candidate graph through
  the separately selected graph authority.
- Compilation resolves every `workflow_node_template_uuid` from the selected
  graph authority's template catalog. Backend-bound candidates use Backend
  template UUIDs synchronized into OS; local candidates use OS-local template
  UUIDs. Importability at the execution OS may validate an action contract, but
  it never changes the graph authority's UUID.
- OS must never fabricate a WorkflowNodeTemplate UUID or substitute a local
  UUID into a Backend-bound candidate. Missing or mismatched authority catalogs
  fail compilation explicitly before graph persistence.

### Workflow Template Catalog

- Persist Backend-shaped `workflow_node_template` and
  `workflow_handle_template` catalogs in local `workflow.db`, partitioned by
  Graph Authority identity.
- For the OS-local Graph Authority, import actions from
  Registry/ResourceTemplate, allocate each local template UUID once, persist
  the stable action-to-template mapping, and reuse it across contract updates
  and restarts.
- For a Backend Graph Authority, synchronize templates through Backend's
  read-only list/detail routes and store the exact Backend template and Handle
  UUIDs. Do not expose removed direct template-mutation routes or substitute a
  local/other-Authority cache.
- Template lifecycle belongs to the ResourceTemplate aggregate. Driver
  importability validates a local implementation but never changes template
  identity.
- Bind every Authoring Compilation to one Graph Authority and catalog
  fingerprint. Apply must reject a fingerprint changed since compilation and
  require recompilation.

### WorkflowNode Template Defaults

- Mirror Backend's frozen parameter fallback exactly; do not recursively
  materialize JSON Schema property `default` values.
- Individual Node creation with an omitted/null root `param` uses the
  template's non-empty `goal_default`, then non-empty `goal`, then `{}`.
  Full-graph `PUT` applies the same rule to every submitted Node whose root
  `param` is omitted/null, including an existing UUID.
- Complete Node `PUT` with omitted/null root `param` stores `{}`. Node `PATCH`
  with `param` omitted preserves the current value; explicit null stores `{}`.
  A supplied object is stored as supplied.
- WorkflowTask creation, snapshotting, planning, and Job dispatch never apply
  template defaults.
- Frontend forms may prefill values for convenience, but values expected to
  persist must be sent explicitly. A display-only default is not server state.

### Python Workflow Authoring Identity

- Python workflow source is a Uni-Lab authoring format, not a portability-clean
  standalone script. It may carry visible, non-executable structured comments
  containing the real `WorkflowNode.uuid`.
- The normalized anchor syntax is exactly
  `# unilab:node_uuid=<uuid>`. Place it immediately before the source construct
  for a persisted WorkflowNode. Source-only parallel structure does not receive
  a UUID anchor.
- A UUID anchor is compiler-maintained authoring metadata. It must never be
  passed as a device action argument, included in a Node's `param`, or validated
  against the device action parameter Schema.
- `from_python_script` preserves valid anchors across parameter changes,
  reformatting, and source reordering. It allocates real UUIDv4 values for
  newly authored persisted action/control Nodes, and returns
  normalized Python source containing those anchors.
- Duplicate or otherwise invalid UUID anchors are compilation diagnostics and
  must never cause two source constructs to persist as one WorkflowNode.
- A duplicate UUID blocks a valid candidate, graph persistence, and Task
  creation. Its diagnostic must include the duplicated UUID, every source
  range, and machine-applicable alternative fixes that preserve one occurrence
  and assign fresh UUIDv4 values to the others. This contract serves frontend,
  CLI, MCP, and coding-agent callers equally; never require a frontend-only
  repair interaction.
- An explicit editor copy command may allocate fresh UUIDs for the copy because
  the user's intent is known. Raw source edits and pastes remain fail-closed on
  duplicates; the compiler must not guess which occurrence inherits historical
  debugging identity.
- Coding agents preserve anchors when editing existing Nodes, omit or allocate
  anchors for new Nodes, and remove or replace copied anchors. Before graph
  persistence they must compile and write the returned normalized source back
  to the authoring file.
- UUID anchors do not replace source maps. Keep source ranges for diagnostics,
  breakpoint markers, start-node markers, and runtime highlighting distinct
  from stable WorkflowNode identity.
- Deleting or changing an anchor is an identity-affecting edit. Compilation
  must expose the resulting create/delete identity changes before persistence;
  it must not silently disguise them as an in-place parameter edit.

### Normalized Python Workflow Form

- Keep the authoring Interface static and AST-only. Do not execute authoring
  source or add `asyncio`, reflection, dynamic imports, `eval`, `exec`, or
  data-dependent unbounded control flow.
- Emit one WorkflowNode per action or control statement. Use stable,
  descriptive, single-assignment-style variable names; do not nest action
  calls or rely on variable rebinding to express Node identity.
- Action calls are keyword-only. Bind one result object per action and access
  registry-declared named outputs as attributes; do not use positional
  parameters or tuple unpacking for action outputs.
- Statements in the same lexical block have a control dependency in source
  order. Variable references add data dependencies. A Node becomes ready only
  after both kinds of dependency are satisfied.
- Parallelism is explicit through `with parallel()`. Its direct `group(...)`
  child blocks are concurrent branches; statements inside each group remain
  sequential. `parallel()` changes graph topology but does not create synthetic
  Fork or Join WorkflowNodes.
- A real downstream Node with multiple active incoming Edges is its own AND
  convergence point. Do not insert a no-op Join before it. When control-only
  synchronization is required, connect branch terminals directly to the real
  downstream Node with dependency-only Edges.
- Express ordered exclusive branching with native `if` / `elif` / `else`.
  Compile the complete chain to exactly one real Backend `condition`
  WorkflowNode, identified by the UUID anchor immediately before `if`.
- Lower condition predicates to Backend's restricted JSON expression AST;
  never execute authoring Python. Preserve first-match semantics by guarding
  each `elif` with the negation of earlier predicates and treating `else` as
  their complement. An `if` without `else` must have an explicit fallthrough
  handle.
- Do not use Python `if` to expose Backend's independent multi-handle
  selection semantics and do not add a hidden/implicit Join after a condition.
- Treat values produced inside an `if`, `elif`, or `else` body as
  branch-local. Reject their use after the condition with
  `UNREPRESENTABLE_BRANCH_VALUE_MERGE`, even when every alternative assigns
  the same Python variable name.
- Do not migrate the old OS-only `ConditionalBinding`, duplicate downstream
  actions, permit multiple Edges into one target Handle, or create hidden
  compute/Phi Nodes to merge branch-local values. A future explicit
  branch-value merge requires its own reviewed Backend contract.
- Close an exclusive condition with an explicit Conditional Join when control
  continues afterward. It is a real persisted Node with stable UUID, source
  map, Job, state, and debugger identity; it is not a data-value merge.
- Until Backend provides an official Join execution kind, use the dedicated
  OS template with Backend-supported `node_type=compute`, distinct optional
  dependency target Handles for branch terminals, an empty compute output, and
  one `ready` source Handle. Do not emit an unsupported `node_type=join`.
- Keep the temporary Conditional Join authority-scoped. A Backend authority
  without that exact template produces a structured missing-template
  diagnostic, never an OS fallback. Replace the temporary representation
  explicitly and completely after Backend publishes its official NodeType.
- Use statically resolved subworkflows for large or reusable branches. Keep
  normalized source deterministic so frontend, CLI, MCP, and coding agents all
  observe the same Python, graph, diagnostics, and changeset.

### Python Authoring Persistence

- Keep persistent OS Authoring Workflow-scoped:
  `GET /api/v1/workflows/{workflow_uuid}/authoring`,
  `PUT /api/v1/workflows/{workflow_uuid}/authoring/draft`, and
  `POST /api/v1/workflows/{workflow_uuid}/authoring/apply`. The path is the
  sole Workflow identity. Do not add persistent behavior to the top-level pure
  transformation routes or extend the shared graph PUT body.
- The selected Graph Authority owns the persisted Workflow graph. When OS is
  selected, use a local SQLite `workflow.db` as the authority for Applied
  Workflow definitions, WorkflowTask snapshots, Jobs, Node results, and other
  execution facts.
- Mirror Backend identities and persistent table names in the local workflow
  database, including `workflow`, `workflow_node`, `workflow_edge`,
  `workflow_task`, and `workflow_node_job`. Do not retain
  `workflow_runs.spec_json` as an editable Workflow definition or use
  `INSERT OR REPLACE` to overwrite Task history.
- Keep Backend's frozen public Workflow model unchanged: do not put
  workflow-level Python source, source maps, or source hashes in Backend,
  Workflow `meta_data`, or a WorkflowNode's per-Node `script` field.
- OS may use a private `workflow_authoring` table for normalized applied source,
  `source_hash`, source map, applied Workflow revision, compiler version, and
  template-catalog fingerprint. These fields must not leak into the Backend
  Workflow wire model.
- A local `workflow.py` is the human- and coding-agent-editable Authoring Draft,
  not the Applied Workflow authority. It may be incomplete, invalid, unapplied,
  or stale.
- A file write may preserve an incomplete or invalid Authoring Draft. It must
  not change the persisted graph, create a WorkflowTask, or replace the last
  applied source association unless compilation succeeds and the candidate is
  explicitly applied through the OS Authoring Apply Interface.
- Explicit local Apply accepts one opaque server-issued Candidate hash. It
  resolves the bound Workflow revision and Draft hash on the server, checks
  that the Candidate's normalized source has already been materialized as the
  package Draft, acquires a stable Catalog snapshot/guard, then opens one
  SQLite write transaction. After that transaction has begun and before any
  graph mutation, it rechecks the actual Draft and uses that check as the Apply
  linearization point. The Catalog guard remains held through the transaction,
  so the fingerprint cannot change at that point. Keep one internal lock order:
  Catalog before Store; never resolve or lock Catalog from a Store transaction
  callback. The transaction writes the complete graph, applied source, source
  map, and new revision. Source-only Apply updates authoring state without
  advancing graph revision.
- Create a local WorkflowTask and copy its exact `workflow_snapshot` in the
  same database transaction. The Scheduler consumes only the snapshot and
  never reads or compiles the live draft file.
- Apply never writes the editable file. If normalized source differs from the
  current Draft at the in-transaction linearization point, require the caller
  to accept the full diff and save that source through Draft PUT before Apply.
  An uncontrolled external write after that point is a later dirty Draft edit;
  it must be preserved and projected as stale rather than rolled back or
  overwritten.
- Persisted graphs remain independently readable and executable without a
  current draft file. Missing or stale Drafts are authoring-state conditions,
  not runtime dependencies.
- Every frontend or coding-agent draft write triggers OS compilation. A valid
  result may be rendered as a clearly marked unapplied Candidate Workflow; an
  invalid result shows diagnostics while the DAG retains the last applied
  persisted graph. Never render or persist a partially trusted graph.
- Draft writes do not call graph `PUT`, increment Workflow revision, create a
  WorkflowTask, or change runtime state. Only explicit Apply/Save may commit a
  complete candidate. For an OS-local Graph Authority this is one
  revision/hash-guarded SQLite transaction over graph-semantic metadata, graph,
  applied source, and source map; it is not a separate shared graph PUT call.
  Run applies a valid dirty draft before separately creating its WorkflowTask.
- Notify browsers of external coding-agent source changes through the existing
  `/api/v1/events` SSE stream. SSE carries an invalidation/availability signal;
  callers rehydrate the draft and compile result from OS.
- Use only `workflow.authoring.changed` for persistent Authoring invalidation.
  Its payload is limited to `workflow_uuid`, `cause`, `workflow_revision`,
  nullable `draft_hash`, and nullable `candidate_hash`; allowed causes are
  `external_draft_changed`, `draft_saved`, `draft_compiled`, `applied`, and
  `recovered`. Never put source, graph, diagnostics, or an Authoring aggregate
  in the event.
- Persist the Authoring state and its frontend event in the same SQLite
  transaction, and expose the event to SSE only after commit. File-watch events
  must be debounced/coalesced under the per-Workflow lock and same-hash OS
  writes deduplicated. Reuse Backend global event ids, `Last-Event-ID` replay,
  and id deduplication; clients may coalesce many events into one Authoring GET.
- A successful Draft PUT or Apply response already hydrates the caller. Ignore
  a later event carrying the same Workflow revision, Draft hash, and Candidate
  hash tuple instead of refetching or showing a duplicate notification.
- Browsers must never read or write the local `workflow.py` directly. OS alone
  owns path resolution, file watching, file I/O, and compilation; `source_uri`
  is provenance/routing metadata only.
- Treat one lab workspace as the local isolation unit. At the current stage a
  domain-device package repository may equal one lab workspace, but keep the
  concepts separate so several packages can later share one Workflow/Task
  history. Set `BasicConfig.working_dir` once at startup to the workspace's
  ignored `unilabos_data` child; never hot-switch it or inherit the legacy
  `~/.unilabos/*.db` Scheduler defaults.
- Run exactly one OS Workflow Authority process for one `working_dir`. Acquire
  a non-blocking process lease before opening `workflow.db`; reject a second OS
  process for the same workspace instead of supporting concurrent Store/schema
  initialization. One Authority may manage and execute many Workflows, and
  per-Workflow Authoring locks remain independent.
- A registered editable package's version-controlled `workflows/*.py` is the
  only Authoring Draft. Do not create another editable
  `working_dir/workflows/<uuid>/workflow.py`. Keep `workflow.db`, logs, package
  cache, durable events, Applied Workflow state, and execution snapshots under
  `working_dir`.
- Map each `workflow_uuid` to one registered package-relative source path and
  expose a logical `package://<package-id>/<relative-path>` URI. Resolve it only
  below an explicitly loaded editable package root; reject caller-selected
  paths, traversal, symlinks, and non-regular files. A compiler declaration or
  blind `.py` scan is not a durable source-identity registry.
- The package file is authority for Draft saves and external coding-agent/Git
  edits; SQLite is authority for Apply and execution. Reconcile file-first
  Draft changes into derived Candidate/event state. Materialize an explicitly
  accepted normalized source through Draft PUT before Apply. Apply performs
  a final read-only Draft validation inside its SQLite write transaction while
  holding the stable Catalog snapshot acquired before that transaction. It has
  no Draft mutation or post-commit file writeback.
- Reconcile registered sources at startup. A missing or renamed package file
  produces nullable Draft/Candidate state without deleting the Applied
  Workflow, and Authoring GET must not recreate it. Restore only the registered
  path. Workflow deletion must never recursively delete a package or lab
  workspace; use ignored runtime trash plus Git recovery where needed.
- When an external Draft-change event arrives, a clean frontend document
  rehydrates the Workflow-scoped Authoring aggregate and synchronizes Python,
  Candidate DAG, diagnostics, and hashes. A dirty Code or DAG document keeps
  its complete local buffer, does not fetch source into that document, and
  displays an external-change-pending state.
- Draft saves use the source hash observed when editing began. A changed file
  produces a conflict and source diff; overwrite is allowed only after explicit
  user confirmation against the newly observed hash. Never let `loadRevision`,
  an SSE refresh, or a generated candidate silently clear a dirty draft.
- Every persistent Draft PUT carries the complete `python_source`, the exact
  SHA-256 hash of the UTF-8 Draft bytes observed when editing began, and the
  observed integer `Workflow.revision`. The hash may be null only when no Draft
  existed. Check both tokens against the actual file and database under one
  per-Workflow lock before atomically replacing the file.
- A Draft hash or Workflow revision mismatch returns `409` without writing
  source or Candidate state. Do not expose an unguarded or force-save path.
  Matching Draft writes save even invalid source and compile it for diagnostics;
  they never Apply, advance Workflow revision, create a WorkflowTask, or
  dispatch a device.
- Authoring Apply accepts only one opaque server-issued `candidate_hash`.
  Never accept separate client-provided Draft/revision tokens, graph, reserved
  metadata, normalized source, source map, compiler version, or
  template-catalog data in the Apply request.
- Bind Candidate hash to the Draft hash, Workflow base revision, complete
  graph-semantic Apply bundle, normalized source/source map, compiler version,
  and authority-scoped catalog fingerprint. Resolve and recheck those
  server-owned facts under the per-Workflow lock, revalidate the Candidate,
  then acquire a stable Catalog snapshot before beginning the atomic SQLite
  write transaction. Before mutating the graph, perform the final actual-Draft
  check inside that transaction and require normalized source to equal those
  Draft bytes; the held snapshot proves the Catalog fingerprint is unchanged
  at the same point. Compiler adapters backed by a mutable Catalog must expose
  a snapshot guard that spans this transaction. Keep the internal lock order
  `Catalog -> Store`; the Store callback is Draft-only and must not enter the
  Catalog. Apply does not create or run a WorkflowTask.
- Authoring GET and successful Draft PUT return one self-consistent aggregate
  containing current Backend-shaped Applied Graph, nullable Draft, nullable
  current Candidate, nullable Applied Source, Workflow revision, hashes,
  diagnostics, changeset, provenance, and one server-derived state. Reuse the
  frozen Backend Graph projection for both applied and candidate graphs.
- Never return an old Candidate for a new Draft. Missing Draft or Applied Source
  is represented by a nullable singular field and a successful GET; all
  collections remain `[]`. Source maps identify
  `workflow_node_uuid`, never legacy Node IDs.
- Persisting invalid Python is a successful Draft PUT with Chinese diagnostics
  and no Candidate. Apply returns its graph/source-only result plus the complete
  post-Apply aggregate. Its `warnings` collection is empty because Apply has no
  post-commit Draft writeback.
- Keep the Backend `code/data/error` envelope. Authoring errors use the specific
  machine codes fixed in D-079 and directly displayable Chinese messages, do not
  embed source or Candidate payloads, and preserve dirty frontend state.
- Keep `authoring/compile`, `authoring/generate-python`, and
  `authoring/validate` pure. They use Backend workflow, revision, Node, and Edge
  UUID wire models but never write draft/source state, graphs, Tasks, runtime
  state, or devices. Internal Canonical models must not leak as their public
  wire contract.
- When compilation proves that only comments, whitespace, or formatting
  changed, with identical UUID anchors and identical complete Backend graph
  semantics, Apply updates the OS source artifact and source map against the
  current Workflow revision without calling graph `PUT`.
- Treat source-only Apply as proof-based. Any added, removed, replaced, or
  duplicated UUID anchor is identity-affecting and must follow the normal graph
  changeset and Apply path.
- In the first migration phase, do not build or imply a Python CST/source-range
  patch engine. DAG edits that change Python-represented actions, parameters,
  Nodes, Edges, or control structure generate a complete deterministic
  normalized Python candidate.
- Show the complete source diff and require explicit acceptance before a
  generated candidate replaces human or coding-agent source. Never silently
  overwrite it.
- Graph-only presentation changes that Python does not represent, such as
  canvas layout, keep the source unchanged and associate it with the graph
  revision returned by the successful graph write.
- Mark an Applied Authoring Source stale whenever its recorded revision differs
  from the current persisted Workflow revision. Stale is not the same as an
  invalid draft.
- Never silently change the recorded revision or automatically merge a stale
  source with a newer graph. Require either graph-wins normalized generation
  with a source diff, or code-wins recompilation against the latest revision
  with a complete graph diff.
- A stale or missing source does not block explicit execution of the selected
  Graph Authority's saved graph. It does block any claim that an unreconciled
  draft was applied or executed.

### Migration and Verification

- Treat Backend `feat/workflow@09609a2` as the read-only authority for its
  frontend-facing contract in this migration. Do not modify Backend source,
  schema, docs, tests, branches, or commits. Record defects and missing
  capabilities instead of fixing Backend or silently changing a shared OS
  route. Backend-to-Edge protocols and deployment internals are outside this
  parity scope.
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
