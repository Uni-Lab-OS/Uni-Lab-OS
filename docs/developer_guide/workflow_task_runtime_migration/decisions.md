# Migration decisions

Confirmed decisions are append-only during the migration. If one changes, add
a later decision that explicitly supersedes it.

## Decision status ledger

This ledger was audited against the frozen, read-only frontend contract at
Backend `feat/workflow@09609a2` and the confirmed decisions through D-070.
Historical text remains for provenance, but its status controls
implementation:

- **SUPERSEDED** has no implementation authority.
- **PARTIALLY SUPERSEDED** retains only the portion named in its status note.
- unmarked decisions remain active unless a later marked decision explicitly
  narrows them.

Fully superseded decisions:

- D-014, D-015, and D-016 by D-033;
- D-017 by D-034;
- D-021 by D-044 and D-058;
- D-022 by D-043;
- D-023 by D-025; and
- D-026 by D-045.

Partially superseded or narrowed decisions:

- D-004, D-010, D-011, D-013, D-018, D-019, D-020, D-024, D-027, D-028, D-029,
  D-033, D-035, D-036, D-039, D-043, D-044, D-047 through D-052, D-054,
  and D-055.

Do not derive new work from a superseded clause merely because it appears
earlier in this append-only file.

## D-001: preserve the reviewed target

The branch `feat/edge-networking-and-scheduler@7bbfd38` is an immutable reviewed
baseline. Migration work starts from it on a new integration branch.

## D-002: migrate the union of tests

Preserve every target test and migrate every current-source unit test. Tests
move with their owning functional phase, before its implementation. Phase 09
must account for every source test as migrated, manually merged, or explicitly
superseded by a stronger Interface test.

## D-003: functional migration, not Git history synthesis

The repositories have no common Git ancestor. Do not merge or whole-branch
cherry-pick the source mirror. Classify source code as direct migration,
semantic migration, manual merge, or superseded. Record source commit and path.

## D-004: contract authority

> **Status: PARTIALLY SUPERSEDED by D-058.** User-confirmed decisions remain
> authoritative. The claim that unchanged details default to documents 10-12
> is retired; frozen Backend frontend code at `09609a2` outranks those
> historical documents.

User-confirmed migration decisions are authoritative. Unchanged contract
details follow Backend review documents 10-12. Supplemental protocols require
explicit adoption. Existing target and source code are implementation evidence,
not contract authority.

## D-005: mirror Backend identity and naming

Use Backend model types, singular snake-case tables, `uuid` entity primary keys,
Backend relationship fields, Backend path variables, and the same identity
names in local variables. Do not retain `run_id`, old camel-case wire aliases,
or `/api/v1/runtime/runs`. Do not maintain a compatibility adapter for the old
Run vocabulary.

## D-006: stage branches and reviewable history

> **Status: ACTIVE REMAINDER, REFINED by D-096.** Completed work still
> accumulates on the integration branch and migration provenance remains
> unsquashed, but D-096 replaces phase-sized implementation branches with one
> fresh branch and one independent gate per mergeable round.

Completed phases accumulate on `integration/workflow-task-runtime`. Each phase
uses `migration/NN-*`, keeps reviewable functional commits, and merges only
after the phase gate. Do not squash migration provenance. Do not push without
explicit authorization.

## D-007: bounded baseline debt

Phase-target tests must be green. Existing collection blockers are registered
debt with an owning phase and cannot become permanent skips/xfails. Phase 09
requires a fully collectable and passing suite.

## D-008: temporary and permanent documentation

Migration decisions, manifests, and progress live temporarily under
`docs/developer_guide/workflow_task_runtime_migration/`. Permanent invariants go
to `AGENTS.md`; maintained module behavior goes to each module README. Remove
the temporary migration directory after phase 09 has distilled lasting facts.

## D-009: phase 00 scope

Phase 00 may create branches, temporary ledgers, baseline evidence, and
permanent `AGENTS.md` invariants. It must not change production code, tests,
frontend code, Backend code, or database schemas, and it must not repair known
baseline failures.

## D-010: atomic full-graph authoring

> **Status: PARTIALLY SUPERSEDED by D-013 and D-034.** Atomic full-graph `PUT`
> remains active. The claim that it is the editor's only save operation is
> retired.

Add `PUT /api/v1/workflows/{workflow_uuid}/graph` to the shared Backend and OS
Interface. It is the workflow editor's only save operation and atomically
replaces the complete control DAG. Existing per-Node and per-Edge CRUD remains
available for Backend administration but is not a workflow-editor Interface.

## D-011: stable WorkflowNode UUID anchors in Python

> **Status: PARTIALLY SUPERSEDED by D-054.** Real persisted action and control
> Nodes retain UUID anchors. Source-only parallel/Fork/Join structure does not
> receive a WorkflowNode UUID.

Generated Python authoring source carries non-executable structured comments
that anchor statements and implicit control nodes to their real
`WorkflowNode.uuid`. `from_python_script` preserves anchored UUIDs, allocates
UUIDs for new statements, and returns normalized source containing those
anchors. Source reordering or parameter edits must not change the anchored
Node identity. Duplicate anchors must never be persisted as one Node identity;
their exact authoring UX is decided separately.

## D-012: Python compilation is side-effect free

`POST /api/v1/authoring/compile` invokes `from_python_script` as a pure
conversion and validation boundary. It returns normalized Python, a candidate
Backend-shaped graph, a graph changeset, and diagnostics without changing
persistent state. Only an explicit graph-save request may persist a successfully
compiled candidate.

## D-013: graph writes and task submission have separate semantics

> **Status: PARTIALLY SUPERSEDED by D-034.** Saving and Task creation remain
> separate, and full-graph `PUT` remains active. The proposed graph `PATCH`
> route and changeset are retired.

This decision supersedes D-010's single-write-operation restriction. Both
shared graph-write operations are retained:

- `PATCH /api/v1/workflows/{workflow_uuid}/graph` applies a routine editor
  changeset atomically.
- `PUT /api/v1/workflows/{workflow_uuid}/graph` reconciles an entire submitted
  graph atomically by entity UUID. It updates existing entities in place,
  creates only new UUIDs, and soft-deletes omitted entities; it must never
  implement delete-all/recreate-all.

Neither graph-write operation creates an execution. When the editor has dirty
state, Run first saves the complete graph with `PUT`, then creates execution
with `POST /api/v1/workflow-tasks`. Only the latter creates a `WorkflowTask`
and its immutable `workflow_snapshot`.

## D-014: retain Backend update_time

> **Status: SUPERSEDED by D-033. Do not implement.**

Do not add a `graph_revision` field. Retain Backend's existing `update_time`
model for Workflow graph change tracking. The exact concurrency-precondition
semantics of this timestamp are decided separately.

## D-015: update_time is the graph concurrency token

> **Status: SUPERSEDED by D-033. Do not implement.**

Every Workflow metadata, Node, or Edge mutation, including individual CRUD and
atomic `PATCH`/`PUT` graph writes, must update `Workflow.update_time` in the
same transaction. Graph writes and WorkflowTask creation compare the
client-observed Workflow timestamp before changing state or taking a snapshot.
A mismatch returns `409` and performs no partial write or Task creation.

## D-016: preserve current Backend timestamp semantics

> **Status: SUPERSEDED by D-033. Do not implement.**

This decision supersedes D-015. Keep Backend's current per-entity
`update_time` behavior: Workflow, WorkflowNode, and WorkflowEdge update only
when that entity changes. Do not touch `Workflow.update_time` for Node or Edge
changes, do not add graph-version or expected-timestamp request fields, and do
not use timestamps for optimistic graph locking. `PUT`/`PATCH` graph writes use
last-write-wins semantics, and WorkflowTask creation snapshots the graph
observed when `POST /api/v1/workflow-tasks` is handled.

## D-017: entity-level PATCH changesets

> **Status: SUPERSEDED by D-034. Do not implement.** Backend has Node `PATCH`
> and batch delete, but no graph `PATCH` changeset.

Graph `PATCH` contains only changed entities and deleted entity UUIDs. Each
changed WorkflowNode or WorkflowEdge uses its complete Backend write DTO; do
not add field-path, JSON Patch, or nested `set`/`unset` semantics. Deletions
use `delete_node_uuids` and `delete_edge_uuids`.

## D-018: graph writes do not own templates

> **Status: PARTIALLY SUPERSEDED by D-034.** Graph reads return template
> collections and graph writes never own or mutate them. The frozen graph PUT
> does not carry an embedded Workflow write model; its body is only
> `revision`, `nodes`, and `edges`.

Full-graph `PUT` accepts only Workflow, WorkflowNode, and WorkflowEdge write
models. WorkflowNodeTemplate and WorkflowHandleTemplate objects returned by
graph reads are shared reference data and must not be created or changed by a
graph write. Supplying either template collection to graph `PUT` is a
validation error. Control nodes reference separately registered templates.

## D-019: real UUIDs in atomic graph writes

> **Status: PARTIALLY SUPERSEDED by D-034.** Stable real UUIDs remain mandatory
> for full graph PUT. References to a graph PATCH are retired because that
> route does not exist.

Every WorkflowNode and WorkflowEdge in graph `PUT`/`PATCH` carries its real,
non-zero Backend `uuid`. The DAG editor allocates UUIDv4 values for manually
added entities; `from_python_script` allocates them for new Python statements
and derived edges. These values become the persisted primary keys. Do not add
temporary IDs, legacy `node_id`/`edge_id`, or ID-mapping responses. Existing
single-entity create endpoints may continue allocating UUIDs server-side.

## D-020: Backend-shaped graph write DTOs

> **Status: PARTIALLY SUPERSEDED by D-034 and D-058.** Stable entity UUIDs and
> response-only timestamps remain active. The frozen graph `PUT` body is only
> `revision`, `nodes`, and `edges`; it does not contain `Workflow.uuid`, and
> its Node items do not carry `workflow_uuid`.

Graph writes require `Workflow.uuid` and each `WorkflowNode.workflow_uuid` to
match the path `workflow_uuid`; every WorkflowNode and WorkflowEdge carries its
real entity `uuid`. Edge ownership is derived from its endpoint Nodes. Server
fields `create_time` and `update_time` are response-only and are invalid in a
write request. Frontend services must construct write DTOs rather than echoing
an unmodified graph-read response.

## D-021: current Backend execution implementation is authoritative

> **Status: SUPERSEDED by D-044 and D-058. Do not use `5c05941` as the
> contract baseline.**

For execution, commands, feedback, events, intervention, timeout, and local
execution behavior, use the clean Backend branch
`feat/workflow@5c05941` as the implementation authority. Documents 10 and 11
remain the baseline for template and graph details not explicitly superseded.
Document 12 is historical where it exposes direct Task/Job status mutation:
those public write routes must not be restored. User-confirmed graph-authoring
extensions remain deliberate additions. `/edge/*` routes are Backend-to-OS
control/data-plane interfaces and are not frontend routes.

## D-022: mirror Backend HTTP response envelopes

> **Status: SUPERSEDED by D-043. Do not implement this envelope.**

OS frontend-facing HTTP routes use the same Backend envelope: successful JSON
is `{"data": ...}`, errors are
`{"error": {"code": "...", "message": "..."}}`, paginated results live below
`data`, and successful deletes return an empty `204`. Do not expose FastAPI
`detail` bodies or naked Workflow/Task objects. SSE retains its native
`id`/`event`/`data` framing. Authoring diagnostics are successful response data
unless the request or service itself fails.

## D-023: separate attention and routine realtime channels

> **Status: SUPERSEDED by D-025. Do not add the frontend routine-status
> WebSocket described here.**

Workflow realtime delivery uses two frontend channels. SSE is reserved for
attention-demanding notifications that can open operator UI, including human
intervention and node-retry decisions. Routine Task/Job state and feedback
updates use WebSocket. Neither transport is a second state authority: durable
WorkflowTask, WorkflowNodeJob, feedback, command, and intervention records
remain authoritative and are re-hydrated through REST.

## D-024: intervention decisions remain REST writes

> **Status: PARTIALLY SUPERSEDED by D-025.** REST decision writes and durable
> intervention history remain active. The routine-status WebSocket reference
> is retired.

SSE only announces attention events such as `intervention.required`,
`intervention.resolved`, and `intervention.superseded`. The operator's human
interaction or retry choice is submitted through
`POST /api/v1/workflow-interventions/{intervention_uuid}/decisions` with the
Backend revision and `Idempotency-Key`. Do not send authoritative decisions
upstream through SSE or the routine-status WebSocket.

## D-025: frontend realtime mirrors Backend SSE

This decision supersedes D-023 and the routine-status WebSocket reference in
D-024. All frontend-facing realtime projections use
`GET /api/v1/events` as the single SSE stream, including routine Task/Job state,
Job feedback, intervention notifications, and Edge status notifications. The
frontend resumes the stream with `Last-Event-ID` and re-hydrates durable state
through REST; SSE delivery is never the state authority.

Do not add a frontend WorkflowTask WebSocket or
`/api/v1/workflow-tasks/{task_uuid}/events`. The existing
`/api/v1/edge/ws` WebSocket remains an internal Backend-to-OS Edge control
channel and must not be exposed as a frontend interface. Task commands and
intervention decisions remain REST writes.

## D-026: materialize template defaults only at Node instantiation

> **Status: SUPERSEDED by D-045. Do not recursively materialize property
> defaults on the server.**

This decision deliberately extends the current Backend behavior for atomic
frontend, CLI, and MCP authoring. When a WorkflowNode is first instantiated
from a WorkflowNodeTemplate, recursively materialize applicable JSON Schema
`default` values into the Node's `param`, then validate and persist the fully
resolved object. A client-supplied value takes precedence; only a missing
property is defaulted, and explicit `null` is not treated as missing.

Instantiation includes individual Node creation, a new Node produced by
`from_python_script`, and a Node UUID in graph `PUT`/`PATCH` that does not yet
exist in persistence. A preallocated client UUID does not make such a Node an
update. The operation returns the resolved persisted Node so every caller can
observe the actual parameters.

Never reapply template defaults when updating an existing Node, creating a
WorkflowTask, building its snapshot, or dispatching a Job. Removing an optional
property from an existing Node remains a real removal. Changing a template
default does not mutate existing Nodes. After default materialization, missing
required values still fail Schema validation. Frontend, CLI, MCP, and transport
adapters must not maintain separate authoritative default-merging
implementations.

The frontend may initialize a newly created Node's form and editor state from
Schema defaults for immediate UX feedback, but opening an existing Node must use
only its persisted `param` and must not reapply current template defaults. A
displayed default must be present in the new Node's editor state rather than
remaining a visual placeholder. After any create or graph-save operation, the
frontend replaces its candidate with the resolved Node returned by the server.

## D-027: one frontend Interface, one selected authority

> **Status: PARTIALLY SUPERSEDED by D-031, D-046, and D-058.** Shared
> Backend-defined frontend capabilities retain base-URL parity and one selected
> authority. OS-only Authoring Compilation and debug extensions are explicit
> exceptions. The prescription that OS internals must use Backend `/edge/*`
> channels is retired.

For every capability shared by Backend and the OS local micro-backend, frontend,
CLI, and MCP callers switch only the base URL. Path, HTTP method, request and
response DTOs, envelopes, error semantics, and business meaning remain
identical. In particular, `/workflows` always manages static definitions,
`/workflow-tasks` always manages executions, and `/events` is always the
frontend SSE stream.

The OS local micro-backend is an independent selected authority and must not
transparently proxy, fall back to, or split a frontend operation across Backend.
Backend-to-OS synchronization and execution use explicit internal `/edge/*`
channels instead. OS-only diagnostics and administration may remain under
clearly private namespaces, but a public Backend path must never retain a
different legacy OS meaning. Unsupported local capabilities are declared
explicitly rather than silently forwarded; the capability-discovery Interface
is decided separately.

## D-028: no shared capability-discovery endpoint

> **Status: PARTIALLY SUPERSEDED by D-031 and D-046.** The prohibition on a
> shared capabilities endpoint remains active. The claim that both authorities
> must implement OS-only authoring and start-frontier/breakpoint extensions is
> retired.

This decision closes D-027's deferred capability-discovery question. Do not add
`GET /api/v1/capabilities` in this migration. Shared workflow authoring,
execution, event, and debugging Interfaces must actually be implemented by both
Backend and the OS local micro-backend; capability flags must not hide parity
gaps.

The source-only `/api/v1/runtime/capabilities` describes limitations of its
legacy Python fallback engine and leaves the public Interface together with the
old Runtime/Run routes. Backend's Edge `capability_revision` remains an internal
registration value, not frontend discovery. Existing static frontend
capability matrices are not promoted into the shared server contract. Genuine
material or execution-engine differences are decided in their owning phases
and must fail explicitly until then.

## D-029: Python accepts compiler-maintained WorkflowNode UUIDs

> **Status: PARTIALLY SUPERSEDED by D-054.** UUID anchors remain active for
> real persisted Nodes, not source-only parallel/Fork/Join structure.

This decision confirms and sharpens D-011. Python workflow files are Uni-Lab
Authoring Source, not portability-clean standalone scripts. They may carry
visible, non-executable structured comments containing the real
`WorkflowNode.uuid`. The UUID is authoring metadata and never a device action
argument or a member of the Node's `param`.

`from_python_script` preserves valid anchors across ordinary edits, allocates
real UUIDv4 values for newly authored explicit action nodes and implicit
control nodes, and returns normalized source containing the allocated anchors.
UUID anchors establish Node identity; source maps independently establish
source ranges for diagnostics, editor markers, and runtime highlighting.

A missing, deleted, or changed anchor is an identity-affecting edit and must be
visible in the candidate changeset before persistence. Duplicate or invalid
anchors are diagnostics and must never bind two constructs to one persisted
Node. The exact duplicate-anchor recovery interaction and the Backend field
used to persist Authoring Source and its source map remain separate decisions.

## D-030: duplicate UUID repair is machine-operable and fail-closed

A duplicate Python UUID anchor prevents a valid Candidate Workflow, graph
persistence, and WorkflowTask creation. Compilation returns a structured
`DUPLICATE_NODE_UUID` diagnostic containing the UUID, every occurrence's source
range, and machine-applicable alternative fixes. Each alternative names the
occurrence that retains the historical identity and supplies fresh UUIDv4
replacements for the others.

This is one caller-independent contract for frontend, CLI, MCP, and coding
agents. A frontend may render the alternatives as quick fixes, but compilation
must not depend on a frontend interaction. An explicit copy command may allocate
fresh UUIDs proactively because the copy intent is known; raw source edits and
pastes remain fail-closed because the compiler cannot safely infer which
occurrence owns historical breakpoints and execution identity.

Coding agents preserve anchors on existing Nodes, omit or allocate anchors for
new Nodes, and remove or replace anchors on copied Nodes. Before persistence
they compile, apply any selected fix, and write the returned normalized source
back to the Python authoring file.

## D-031: Authoring Compilation is OS-only

This decision narrows D-027's base-URL parity and supersedes D-028 only where it
requires authoring compilation on both authorities. In this migration, only OS
exposes `POST /api/v1/authoring/compile` and executes `from_python_script`.
Backend does not implement, proxy, or fall back to this route. D-012's
side-effect-free compile semantics remain unchanged.

Frontend, CLI, MCP, and coding agents send Python Authoring Source directly to
OS for compilation. They may then persist the returned Backend-shaped Candidate
Workflow through the separately selected graph authority. If OS compilation is
unavailable, no other authority silently substitutes for it.

The WorkflowNodeTemplate UUID catalog against which OS compiles—particularly
when the candidate will be persisted by Backend—remains a separate decision.

## D-032: template UUIDs follow the graph authority

OS Authoring Compilation resolves WorkflowNodeTemplate identities from the
selected graph authority's template catalog. A Candidate Workflow destined for
Backend uses Backend-issued `workflow_node_template_uuid` values synchronized
into OS; a locally persisted candidate uses OS-local template UUIDs. A local
driver's importability may validate that an action contract is available, but
it never replaces the graph authority's identity.

OS must not generate a WorkflowNodeTemplate UUID or substitute an OS-local UUID
into a Backend-bound candidate. A missing authority catalog fails compilation
with `TEMPLATE_CATALOG_UNAVAILABLE`; a contract or identity mismatch fails with
`TEMPLATE_CATALOG_MISMATCH`. Neither failure yields a persistable candidate.
The catalog synchronization transport and selection mechanism are decided
separately.

## D-033: mirror Backend Workflow revision concurrency

> **Status: PARTIALLY SUPERSEDED by D-060, D-066, and D-071 for reserved OS authoring
> metadata.** Ordinary shared Workflow metadata PUT still does not advance graph
> revision. Changes to `unilab.input_contract`, `unilab.output_contract`, or
> `unilab.output_bindings`, or Node `unilab.input_bindings` are graph-semantic
> and may occur only through atomic OS Apply, which does advance revision.

This decision supersedes D-014, D-015, and D-016. Mirror Backend
`feat/workflow@09609a2`: `Workflow.revision` starts at `1`. Every WorkflowNode
or WorkflowEdge create, update, or delete—including full-graph `PUT` and batch
delete—increments `Workflow.revision` and `Workflow.update_time` in the same
transaction.

Graph reads expose `Workflow.revision`. Full-graph `PUT` requires the revision
observed by the caller, rejects a mismatch atomically with `409`, and returns
the reconciled graph with its revision incremented. Entity `update_time` values
remain audit data and must not be used as graph concurrency tokens. Workflow
metadata `PUT` does not increment the graph revision, matching the current
Backend contract.

`POST /api/v1/workflow-tasks` does not accept an expected revision. It creates
an immutable snapshot from the persisted graph observed when the request is
handled.

## D-034: mirror Backend graph editing routes

This decision supersedes D-013 and D-017 only where they define
`PATCH /api/v1/workflows/{workflow_uuid}/graph`; that route does not exist.
Mirror Backend `feat/workflow@09609a2` instead:

- use `PATCH /api/v1/workflow-nodes/{node_uuid}` for routine partial Node
  edits, with an omitted field meaning unchanged and an explicit `null`
  clearing a nullable field;
- use the workflow-scoped Node and Edge `POST` routes to create entities;
- retain Backend's complete Node `PUT` Interface, while ordinary frontend
  property edits use Node `PATCH`;
- use `PUT /api/v1/workflow-edges/{edge_uuid}` for a complete Edge edit;
- use individual `DELETE` routes or
  `POST /api/v1/workflows/{workflow_uuid}/batch-delete` for multi-entity
  deletion; and
- use revision-guarded `PUT /api/v1/workflows/{workflow_uuid}/graph` for a
  complete graph reconciliation produced by Python compilation, JSON/Python
  synchronization, multi-Node or control-structure changes, or an explicit
  full save.

Full-graph `PUT` contains `revision`, `nodes`, and `edges`; the path identifies
the Workflow, and every submitted entity carries its stable UUID. It reconciles
by UUID and soft-deletes omitted entities, without delete-all/recreate-all.

No graph write creates an execution. If Run begins with dirty editor state, the
caller first saves the complete graph with graph `PUT`, then separately creates
the execution with `POST /api/v1/workflow-tasks`.

## D-035: Backend owns the graph; OS owns Python authoring artifacts

> **Status: PARTIALLY SUPERSEDED by D-041.** Backend still does not persist
> workflow-level Python. When OS is the selected Graph Authority, local SQLite
> owns both the Applied Workflow graph and applied authoring record; the
> editable `.py` file is only a Draft.

Keep Backend's current Workflow model unchanged. Backend persists the
Backend-shaped Workflow graph and immutable WorkflowTask snapshots; it does
not persist workflow-level Python source, source maps, or source hashes. A
WorkflowNode's `script` field belongs only to that Node, and Workflow
`meta_data` must not be used as a hidden workflow-source store.

OS owns the Python Authoring Store. Human and coding-agent edits are written to
local `.py` files, while a local index records at least the `workflow_uuid`,
Graph Authority identity, `source_uri`, `source_hash`, and the latest persisted
`Workflow.revision` to which a successfully applied source corresponds. OS
also owns the normalized source and source map produced by Authoring
Compilation.

A file may persist an incomplete or invalid Authoring Draft without changing
the Backend graph. Only a successfully compiled Candidate Workflow that is
explicitly applied through the graph-write Interface changes the persisted
Workflow. A Backend graph remains independently readable and executable when
no Python artifact is present; the OS association exists to support code
authoring and code/DAG synchronization, not to add an execution dependency.

## D-036: source writes compile and preview but do not auto-apply

> **Status: PARTIALLY SUPERSEDED by D-041.** Draft write, validation, Preview,
> and explicit Apply remain active. For the OS-local Graph Authority, Apply is
> one SQLite transaction over graph and authoring artifacts rather than a
> separate call to the shared graph PUT route.

Writing an Authoring Draft—whether through the frontend or directly by a
coding agent—must trigger OS Authoring Compilation, but must not automatically
write the persisted Workflow or create a WorkflowTask.

A successful compile returns a complete Backend-shaped Candidate Workflow,
normalized Python, source map, diagnostics, and changes relative to the
persisted graph revision on which the draft is based. The frontend may render
that complete Candidate Workflow immediately as an explicitly marked
**unapplied preview**. Preview never calls graph `PUT`, increments
`Workflow.revision`, changes runtime state, or becomes an execution source.

An invalid draft remains stored and its diagnostics are shown in the code
surface. It does not yield a partially trusted graph: the DAG surface retains
the last applied persisted graph and clearly marks the draft invalid.

Only an explicit Apply or Save operation may submit the complete candidate
through revision-guarded graph `PUT`. Run with a valid unapplied draft performs
that Apply first and creates a WorkflowTask only after Apply succeeds. A coding
agent uses the same explicit Apply contract as the frontend.

When a coding agent changes a watched source outside the frontend, OS notifies
the browser through the established `/api/v1/events` SSE projection. The
frontend then rehydrates the draft and compile result from OS; the SSE payload
is not the Candidate Workflow or a persistence authority.

## D-037: source-only Apply does not advance graph revision

If Authoring Compilation proves that a draft changes only comments,
whitespace, or formatting while preserving every UUID anchor and producing the
same complete Backend Node, Edge, and control semantics as the persisted
Workflow, its graph changeset is empty.

Explicitly applying such a draft updates only the OS Applied Authoring Source,
`source_hash`, and source map associated with the current
`Workflow.revision`. It does not call graph `PUT` and does not advance the
revision. Run may complete this local source-only Apply and then create a
WorkflowTask from the unchanged persisted graph.

This optimization is proof-based, not text-based. Adding, removing, replacing,
or duplicating a UUID anchor is identity-affecting and can never be classified
as a source-only change.

## D-038: semantic DAG edits generate complete normalized Python

The first migration phase keeps the existing `to_python_script` boundary and
does not add a concrete-syntax-tree or source-range Python patch engine.

Graph changes that affect Python-represented semantics—including actions,
parameters, Nodes, Edges, and control structure—generate a complete
deterministic normalized Python candidate. Before Apply, the frontend must show
the resulting source diff and require explicit acceptance; OS must never
silently overwrite the human or coding-agent source.

Graph-only presentation changes that Python does not represent, such as canvas
layout, do not regenerate source. After the graph write succeeds, the unchanged
Applied Authoring Source is associated with the new `Workflow.revision`.

Comment- and formatting-preserving local Python patching is a possible later
enhancement, not a prerequisite or hidden partial implementation in this
migration phase.

## D-039: graph revision mismatch makes authoring source stale

> **Status: PARTIALLY SUPERSEDED by D-041 for OS-local persistence.** Stale
> detection and explicit graph-wins/code-wins reconciliation remain active.
> OS-local code-wins commits through the atomic SQLite Apply transaction, not a
> separate shared graph PUT request.

An Applied Authoring Source is stale whenever its recorded applied revision
does not equal the current persisted `Workflow.revision`. Stale is distinct
from syntactically or semantically invalid: it means that the persisted graph
changed after that source was applied.

OS and frontend must not silently rewrite the recorded revision, overwrite the
source, overwrite the graph, or automatically merge control structure and UUID
identity. The frontend shows the latest persisted DAG alongside the stale
source and its base revision, then requires one explicit reconciliation:

- **graph wins** generates normalized Python from the latest graph and requires
  source-diff acceptance before replacing the source; or
- **code wins** recompiles the source against the latest graph revision and
  requires complete graph-diff acceptance before graph `PUT`.

The first migration phase does not implement automatic three-way source/graph
merge.

The current persisted graph remains independently executable while its local
authoring source is missing or stale. A caller may explicitly run that saved
graph, but must not claim to run or apply an unreconciled draft.

## D-040: retain three OS-only pure authoring transforms

This decision extends D-031's OS-only authoring exception to the complete
bidirectional transformation boundary. Retain these three routes:

- `POST /api/v1/authoring/compile` invokes `from_python_script` and converts
  Python to a Backend-shaped Candidate Workflow;
- `POST /api/v1/authoring/generate-python` converts a Backend-shaped candidate
  graph to deterministic normalized Python; and
- `POST /api/v1/authoring/validate` validates a complete candidate against the
  OS template and action contracts.

All three routes are pure with respect to authoring persistence, graph
persistence, WorkflowTask creation, runtime state, and device dispatch. They
return normalized source where applicable, source maps, structured
diagnostics, and the candidate or graph changes needed by the caller.

Their models use `workflow_uuid`, `revision`, `node_uuid`, `edge_uuid`, and the
complete Backend Node and Edge write DTOs. They must not retain the old
`workflow_id`, `base_revision_id`, `node_id`, or Canonical-only public wire
contract, although internal Canonical compilation remains an implementation
detail.

Backend does not implement, proxy, or fall back for these routes. Persistent
draft/source resources and applied-source acknowledgement are separate from
the three pure transformations.

## D-041: SQLite is the local Applied Workflow authority

This decision supersedes D-035 where it assigns Applied Authoring Source
persistence to local `.py` files or says that Backend always persists the
graph. The selected Graph Authority persists the Workflow graph. When OS is
selected, a local SQLite `workflow.db` is authoritative for both the Applied
Workflow and workflow execution facts.

The local schema mirrors Backend domain identities and table names, including
`workflow`, `workflow_node`, `workflow_edge`, `workflow_task`, and
`workflow_node_job`. OS may add a private `workflow_authoring` table containing
the normalized applied source, `source_hash`, source map, applied Workflow
revision, compiler version, and template-catalog fingerprint. These local
authoring fields do not change or leak into Backend's public Workflow DTO.

A human- and coding-agent-editable `workflow.py` remains the Authoring Draft.
It may be incomplete, invalid, unapplied, or stale and is never the Applied
Workflow persistence authority. Explicit Apply checks both the observed
Workflow revision and draft source hash, then writes the complete graph,
normalized applied source, source map, and new revision in one SQLite
transaction. A source-only Apply changes the authoring row without advancing
the graph revision.

`POST /api/v1/workflow-tasks` creates the WorkflowTask and copies the exact
current graph into `workflow_snapshot` in the same database transaction. The
Scheduler consumes that snapshot and never reads or compiles the live draft
file.

After a successful database commit, OS may write the normalized source back to
the editable file. A file-write failure marks the Draft stale and is
recoverable from `workflow_authoring`; it does not invalidate or roll back the
committed Workflow.

The existing `WorkflowHistoryStore.workflow_runs.spec_json` is an execution
history snapshot, not a static Workflow store. It must be migrated to the
Backend-shaped WorkflowTask/WorkflowNodeJob model rather than reused as an
editable definition or retained with `INSERT OR REPLACE` semantics.

## D-042: persist authority-scoped template catalogs in SQLite

This decision completes D-032's deferred catalog persistence and synchronization
direction. Local `workflow.db` stores Backend-shaped
`workflow_node_template` and `workflow_handle_template` catalogs partitioned by
Graph Authority identity.

For an OS-local Graph Authority, Registry/ResourceTemplate import allocates a
local template UUID once, persists the stable action-to-template mapping, and
reuses that UUID across contract updates and restarts. Importability validates
the local action implementation but does not replace the persisted template
identity.

For a Backend Graph Authority, OS explicitly synchronizes the read-only
catalog through Backend's template list and detail routes and stores the exact
Backend template and Handle UUIDs. OS must not expose the removed direct
template-mutation routes, generate Backend identities, substitute local
identities, or silently use another Authority's cached catalog. Template
lifecycle remains owned by the ResourceTemplate aggregate.

Every Authoring Compilation binds to one Graph Authority and records the exact
catalog fingerprint it observed. Apply verifies that fingerprint; a catalog
change between compile and Apply invalidates the Candidate Workflow and
requires recompilation.

An unavailable required catalog returns `TEMPLATE_CATALOG_UNAVAILABLE`; an
identity or contract mismatch returns `TEMPLATE_CATALOG_MISMATCH`. Neither
condition produces or applies a persistable candidate.

## D-043: mirror Backend's current HTTP response envelope

> **Status: ACTIVE WITH D-058 ROUTE EXCEPTIONS.** The general envelope remains
> active. At frozen `09609a2`, invalid `/events` `Last-Event-ID` returns the
> Handler's unwrapped error object, and ResourceTemplate delete returns HTTP
> 200 with an empty-object data envelope; those observed route contracts
> override the generic 204/envelope rules until the authority baseline is
> explicitly upgraded.

This decision supersedes D-022's envelope shape. Mirror Backend
`feat/workflow@09609a2` exactly for frontend-facing JSON routes:

- a successful response with a body is
  `{"code": 0, "data": ...}`;
- an error response is
  `{"code": <http_status>, "error": {"code": "...", "message": "..."}}`;
- paginated results remain below `data`; and
- successful deletes return an empty `204`.

FastAPI validation and handler failures must be normalized to this envelope;
do not expose framework `detail` bodies or naked objects. Shared routes use
Backend's status and error-code meaning. OS-only authoring routes may use their
specific stable domain error codes inside the same envelope.

Authoring diagnostics produced by a valid transformation request—including a
syntax-invalid or semantically invalid Draft with no Candidate Workflow—are
successful response `data` with top-level `code: 0`. Only an invalid request
envelope or service/infrastructure failure produces the HTTP error envelope.

SSE retains its native `id`/`event`/`data` framing and is not wrapped in the
JSON response envelope.

## D-044: Backend is read-only during this OS migration

> **Status: PARTIALLY SUPERSEDED by D-058.** Backend remains read-only. The
> obsolete `f352f54` commit reference is retired and the parity scope is
> narrowed to the frontend contract frozen at `09609a2`.

This decision supersedes D-021's old Backend reference commit. For this
migration, Backend `feat/workflow@f352f54` is the read-only contract and
behavior authority. Backend source, database schema, documentation, tests,
branches, and commits are outside the write scope.

Shared OS routes mirror Backend's current paths, methods, DTOs, envelopes,
errors, and business behavior even when an alternative design might be
preferable. A discovered Backend defect or missing capability is documented as
a contract fact or deferred gap; it is not fixed in Backend and must not be
silently given different meaning on the same OS route.

Implementation, migration, and test changes in this phase are confined to the
Gaojing OS branch and its explicitly scoped frontend/E2E consumers. The
already-confirmed OS-only authoring transformations remain explicit
exceptions, not reasons to extend Backend.

## D-045: mirror Backend's current template-param defaults

This decision supersedes D-026. OS mirrors Backend
`feat/workflow@09609a2` rather than adding recursive JSON Schema default
materialization:

- individual Node creation with an omitted or null root `param` uses the
  template's non-empty `goal_default`, then non-empty `goal`, then `{}`;
- full-graph `PUT` applies the same fallback to every submitted Node whose root
  `param` is omitted or null, regardless of whether that UUID already exists;
- complete Node `PUT` with an omitted or null root `param` stores `{}`;
- Node `PATCH` with `param` omitted preserves the current value, while an
  explicit null root `param` stores `{}`; and
- a supplied parameter object is used as supplied. OS does not recursively
  merge JSON Schema property `default` values.

WorkflowTask creation, snapshotting, planning, and Job dispatch do not apply
template defaults. A frontend may prefill editor state for convenience, but
any value it expects persisted must be sent explicitly; frontend-only display
defaults are not server state.

## D-046: debugger extensions are OS-only in this migration

OS first mirrors Backend `feat/workflow@09609a2` debugger behavior exactly:
WorkflowTask run modes `normal`, `step`, and `single_node`; optional
`target_node_uuid` with Backend's current meaning; and Task commands `step`,
`pause`, `resume`, and `cancel`.

Persisted breakpoints, a start node that scopes a partial DAG run, and
Python/source-map runtime highlighting do not exist in the current Backend.
They are explicit OS extensions in this migration and must not be presented as
implemented Backend capabilities. Backend implementation is outside this
phase's write scope.

OS debugger extensions use only Backend-aligned `workflow_uuid`, `task_uuid`,
`node_uuid`, and `job_uuid` identities and the Node-only runtime state model.
They must not restore the old Run Interface, persist Edge execution state, or
put DAG-walking/debugger authority in the frontend.

Frontend enables these extensions from its explicitly configured Authority
type. Do not add a capabilities endpoint, silently send extension fields to
Backend shared routes, or infer support from transport failure.

## D-047: start nodes and breakpoints are WorkflowTask-scoped

> **Status: PARTIALLY SUPERSEDED by D-052.** Task-scoped configuration and
> snapshot isolation remain active. Replace singular `start_node_uuid` with
> the non-empty `start_node_uuids` frontier everywhere.

A start node and breakpoint set configure one debug WorkflowTask; they are not
part of the editable Workflow graph. Creating or changing a Debug Launch
Configuration does not write Workflow, WorkflowNode, or WorkflowEdge records
and does not advance `Workflow.revision`.

The frontend's markers before Task creation are a Debug Launch Configuration,
not persisted runtime facts. Debug Task creation atomically persists the
initial `start_node_uuid`, breakpoint Node UUIDs, exact
`workflow_snapshot`, applied source hash, and corresponding source map against
the new `task_uuid`.

Normal and debug Tasks may coexist for the same Workflow with different debug
configurations. Later Workflow or Python edits do not change a Task's graph,
Node identity, code highlighting, or debug scope. OS restart restores the
debug Task from its persisted Task-scoped configuration and execution facts.

Source and source-map capture may be absent when a persisted graph has no
applied authoring source; DAG debugging remains valid, while code highlighting
is explicitly unavailable for that Task.

## D-048: debug launch does not have Task-scoped Node skipping

> **Status: PARTIALLY SUPERSEDED by D-052.** The no-skip/disabled distinction
> remains active. Any singular start-node wording is retired.

A Debug Launch Configuration contains only `start_node_uuid` and breakpoint
Node UUIDs. Do not add `skip_node_uuids`, an initial-skip list, or a `skip`
WorkflowTask command.

User-selected Node participation remains part of the editable Workflow
definition through Backend's existing `WorkflowNode.disabled` field. Before
creating a debug Task, the frontend saves any changed `disabled` value through
the ordinary graph-editing Interface. That graph write advances
`Workflow.revision`; debug Task creation then captures the resulting
`workflow_snapshot` and execution plan. The saved choice affects every later
Task created while the Node remains disabled, not only one debug Task.

Mirror Backend planning behavior: a disabled Node and its incident Edges are
absent from the execution plan and do not receive a WorkflowNodeJob. Preserve
the disabled Node in the WorkflowTask snapshot so a frontend can explain why
it is not executable.

Keep three states distinct:

- a disabled Node is editable Workflow definition state;
- a Node before or outside a debug start scope is Task-scoped out-of-scope
  state; and
- a `WorkflowNodeJob` with `status=skipped` is a Scheduler-derived runtime
  result, such as an inactive condition branch.

The frontend may render all three as non-executing Nodes, but it must preserve
their distinct labels, reasons, and edit behavior.

## D-049: debug start scope uses directed reachability

> **Status: PARTIALLY SUPERSEDED by D-052.** Directed reachability remains
> active, but it is computed from the union of a start frontier rather than one
> start Node.

After excluding disabled Nodes, compute a debug Task's active subgraph as the
selected `start_node_uuid` plus exactly the enabled descendants reachable from
it by following directed WorkflowEdges. Retain only Edges whose source and
target Nodes are both in that active subgraph.

Do not interpret "after the start Node" as every Node appearing later in a
topological ordering. Topological order is not a stable semantic boundary for
parallel DAG branches.

Nodes outside the active subgraph remain present in the immutable
`workflow_snapshot` but are out of scope for that Task. They are absent from
the execution plan, receive no WorkflowNodeJob, and must not be rewritten as
disabled or skipped.

For example, with `A -> B -> D` and `A -> C -> D`, selecting `B` produces the
active Node set `{B, D}`. Nodes `A` and `C` are out of scope, and Edge `C -> D`
is not part of the debug execution plan.

## D-050: reject a debug scope with missing required inputs

> **Status: PARTIALLY SUPERSEDED by D-059 and D-060.** Scoped required-input
> validation remains active. A declared Workflow input binding resolved from
> this Task's validated `input` is now a legitimate provider; only arbitrary,
> undeclared Task data and earlier Task/Job results remain prohibited.

After constructing the active debug subgraph, validate every required target
Handle against that scoped plan. An active Node is valid only when each
required input has either a compatible non-null value in the Node's persisted
`param` or an in-scope WorkflowEdge that supplies the target Handle.

If scope removal cuts the only provider of a required input, debug Task
creation returns `422` and persists no WorkflowTask or WorkflowNodeJob. Do not
defer this deterministic planning error until dispatch.

Do not recover a cut input from an earlier Task or Job, transient frontend
state, or an implicit reinterpretation of `WorkflowTask.input`. The caller
must explicitly save a valid Node parameter, change the persisted graph, or
select an earlier debug start scope.

This extends Backend's existing execution-plan validation to the OS-only debug
scope: narrowing a graph must not weaken the input contract of a Node that
remains executable.

## D-051: normalized Python optimizes for coding-agent edits

> **Status: PARTIALLY SUPERSEDED by D-054.** The static coding-agent-friendly
> form remains active. Source-only parallel/Fork/Join structure is not a
> persisted WorkflowNode and has no UUID anchor.

The sole generated Python authoring form is a deterministic, static AST subset,
not arbitrary executable Python. It uses `workflow_uuid` at the Workflow
declaration and the exact structured comment
`# unilab:node_uuid=<uuid>` for every persisted WorkflowNode. An anchor
immediately precedes the source construct that represents its Node.

Normalized Python follows these rules:

- one action or control Node per statement;
- stable descriptive single-assignment-style variable names, with no nested
  action calls or identity-significant variable rebinding;
- keyword-only action arguments;
- one result object per action and attribute access for registry-declared named
  outputs rather than tuple unpacking;
- source-order control dependencies between statements in the same lexical
  block;
- data dependencies from variable and named-output references; and
- explicit `with parallel()` blocks whose direct `group(...)` children are
  concurrent sequential branches.

Do not introduce `asyncio`, reflection, dynamic import execution, `eval`,
`exec`, or data-dependent unbounded control flow. Large or reusable branches
use statically resolved subworkflows.

This small Interface is shared by humans, frontend code editing, CLI/MCP, and
coding agents. A coding agent edits the Draft, invokes pure Authoring
Compilation, applies structured diagnostics or UUID fixes, writes the returned
normalized source, reviews the complete DAG changeset, and only then performs
explicit Apply. No caller maintains a second Python-to-graph interpretation.

## D-054: convergence does not create synthetic Fork or Join Nodes

> **Status: PARTIALLY SUPERSEDED by D-057.** Ordinary parallel convergence still
> uses the real downstream Node and Backend AND-admission semantics. An explicit
> Conditional Join is the sole temporary exception and is a real published
> `compute` template/Node, not a hidden compiler-only barrier.

This decision supersedes D-011, D-029, and D-051 only where they assign
WorkflowNode UUIDs to implicit Fork/Join constructs. Backend's current
executable Node kinds do not include Fork or Join, and its Scheduler already
implements AND convergence for every Node with multiple active incoming
Edges.

`with parallel()` is source-level authoring structure. It suppresses sequential
control dependencies between its concurrent child branches but does not
persist a Fork or Join WorkflowNode, create a WorkflowNodeJob, or receive a
`node_uuid`. It retains source ranges for structural diagnostics.

A real downstream action such as
`transfer(a=a_result.sample, b=b_result.sample)` is the Converging
WorkflowNode. The compiler emits data Edges from both producers directly to
its distinct target Handles, and Scheduler admits it only after all active
predecessors succeed. Do not insert a no-op Join before it.

When a real downstream Node must wait for a branch whose result it does not
consume, emit a dependency-only Edge from that branch terminal directly to the
downstream Node. Workflow completion already waits for every planned Node to
reach a terminal state, so terminal parallel branches do not require a final
Join.

Backend `condition`, `manual_confirm`, and other persisted control-bearing
Node kinds remain real WorkflowNodes with UUID anchors, source-map entries,
Jobs where Backend creates them, and normal debugging identity. Backend
`group` remains persisted presentation structure but is absent from the
execution plan. Do not fabricate executable barrier Nodes to make the graph
look more explicit.

## D-052: parallel debugging uses a start frontier

> **Status: PARTIALLY SUPERSEDED by D-059 and D-060 for required-input
> providers.** The start-frontier and union-of-reachability rules remain active.
> A cut required input may also be supplied by a declared Workflow input
> binding resolved from validated Task input; undeclared/arbitrary Task keys and
> values from earlier Task/Job execution remain forbidden.

This decision supersedes the singular `start_node_uuid` wording in D-047,
D-048, and D-049. A Debug Launch Configuration carries only the non-empty
`start_node_uuids` start frontier and `breakpoint_node_uuids`; do not retain a
singular alias or compatibility adapter. The list represents a set and its
order has no execution meaning.

After disabled Nodes are excluded, the active debug subgraph is the union of
each frontier Node and all of its directed reachable descendants. Retain only
Edges whose endpoints are in that union. Nodes preserved in the
`workflow_snapshot` but outside the union remain out of scope and receive no
WorkflowNodeJob.

Required-input validation from D-050 runs against the complete union. Every
frontier Node must receive each cut required input from a compatible non-null
persisted `param`; the Task must not restore values from an earlier Job or
frontend memory.

For a multi-material parallel Workflow, a frontier such as
`[A2, B1, C2]` represents three entry points in one WorkflowTask, not three
Tasks. Scheduler ownership, Task completion, breakpoints, source mapping, and
event projection remain global to that one Task.

## D-053: breakpoints create Node-local admission holds

Hitting a breakpoint does not globally pause a parallel WorkflowTask. Before
dispatching a ready breakpoint Node, Scheduler persists a Node-local
Breakpoint Hold and withholds that Node while continuing to admit unrelated
ready Nodes.

Do not define or persist a mutable "frozen branch." Descendants of the held
Node and downstream Joins remain pending through ordinary Node and input
dependencies. Other branches may continue until their own dependencies,
breakpoints, or convergence on the held path prevent further progress. Edge
runtime state remains derived.

A Task may therefore remain active while one or more Breakpoint Holds exist.
Frontend projection distinguishes an active Task with held Nodes from a
Task-global pause. The existing `pause` command retains its global meaning and
blocks all new admission; neither a Breakpoint Hold nor global pause cancels a
Job that was already running.

Breakpoint hits and holds persist across OS restart and are addressed by
`task_uuid` plus `node_uuid`. Resume and step behavior for one or several
simultaneous holds is decided separately.

## D-055: native exclusive conditions map to one Backend condition Node

> **Status: PARTIALLY SUPERSEDED by D-057.** The single Backend `condition` Node,
> ordered first-match lowering, and explicit fallthrough remain active.
> Condition branches may now terminate at one explicit published Conditional
> Join Node; the compiler must still never insert a hidden/implicit Join.

Normalized Python expresses ordered, mutually exclusive branching with native
`if` / `elif` / `else`. The exact
`# unilab:node_uuid=<uuid>` anchor immediately before `if` identifies one real
Backend `condition` WorkflowNode. The complete chain does not create one Node
per clause and does not create a synthetic Join.

The compiler accepts only the documented static expression subset and lowers
it to Backend's restricted JSON expression AST. It never executes the Python
condition. An `if` predicate `p1` becomes the first branch; each later `elif`
predicate is guarded by the negation of every earlier predicate, and `else`
is the complement of all preceding predicates. This preserves Python's
first-match semantics even though Backend can independently select multiple
condition handles.

An `if` without `else` has an explicit fallthrough handle so later sequential
statements remain reachable when no predicate matches. A complete
`if` / `elif` / `else` selects exactly one handle for a successfully evaluated
condition. Backend records that selected handle in the condition Job's control
data; Nodes reached only through inactive handles become runtime `skipped`
under the existing Scheduler rules.

Python `if` does not expose Backend's multi-selection condition behavior.
If multicast branching is needed later, give it a separate explicit authoring
construct rather than weakening the ordinary meaning of `if`.

## D-056: branch-local values do not escape an exclusive condition

The first migration mirrors Backend's current graph and expression semantics
and does not preserve the old OS-only `ConditionalBinding`. A value produced
inside an `if`, `elif`, or `else` body may be consumed only inside that same
body. Compilation rejects any use of that value after the complete condition
with the structured diagnostic
`UNREPRESENTABLE_BRANCH_VALUE_MERGE`.

This applies even when every alternative assigns the same Python variable
name. Backend permits only one incoming WorkflowEdge for a target Node and
target Handle pair, and its current restricted compute expression set and
template catalog provide no Phi or branch-selection operation that can safely
merge mutually exclusive producers.

Do not hide this gap by retaining Scheduler-time conditional bindings,
duplicating a downstream action, accepting multiple Edges for one target
Handle in OS only, or fabricating an implicit compute Node without a
source-level UUID anchor. Values defined before the condition remain available
after it, and each branch may continue to consume the values it produces.

A future branch-value merge requires a separately reviewed Backend contract
and an explicit persisted WorkflowNode with stable identity. It is not part of
this migration.

## D-057: OS temporarily represents an explicit Conditional Join as compute

This decision supersedes D-054 and D-055 only where they prohibit every Join
after an exclusive condition. A Conditional Join is not an implicit parallel
barrier or a data merge. It is an explicit, persisted control Node that closes
one exclusive condition region and provides a single control predecessor for
the following sequential Node.

Until Backend introduces an official Join execution kind, OS represents a
Conditional Join with a dedicated WorkflowNodeTemplate whose Backend-supported
`node_type` is `compute`. The Node has its own stable `node_uuid`, Python
anchor, source-map range, WorkflowNodeJob, runtime state, and debugger
identity. Its compute parameter has no data output; distinct optional
dependency target Handles receive the alternative branch terminals and its
single `ready` source Handle feeds the continuation.

Scheduler waits only for active incoming Edges. Therefore the Conditional Join
runs after the selected alternative reaches its terminal Node while Edges from
inactive alternatives do not block it. This resolves the duplicate
`target_handle_uuid` problem without duplicating the continuation. It does not
make a Branch-local Workflow Value available after the condition; D-056
remains unchanged.

The OS authority provides the temporary compute template in its local template
catalog. Compilation against another Graph Authority requires that authority
to expose the referenced template and otherwise fails with a structured
missing-template diagnostic; it must not substitute a different template or
fall back to OS execution.

When Backend publishes an official Join NodeType, adopt it through an explicit
graph/source migration. Do not claim the temporary compute template is the
Backend NodeType, and do not maintain both representations after that
migration is complete.

## D-058: freeze only Backend's frontend contract at 09609a2

This decision supersedes D-044's authority commit and narrows the meaning of
"mirror Backend." The read-only contract authority for this migration is
Backend `feat/workflow@09609a2`. When sources disagree, current
frontend-facing Handler DTOs, Service behavior, public-route tests, and Models
at that commit take precedence over Feishu document revision 12, whose body
still describes `5c05941`. Documents 10 through 12 remain historical context
only where the current implementation has not superseded them.

OS parity covers only the Interface that Backend exposes to frontend clients:
workflow, WorkflowNode, WorkflowEdge, template/material/action data consumed by
the editor, WorkflowTask and WorkflowNodeJob commands and queries, feedback,
manual confirmation and intervention reads/writes, and frontend SSE events.
Match their paths, methods, wire DTO field names, envelopes, HTTP and domain
errors, and frontend-observable business meaning. If an event such as
`edge.status_changed` is delivered through the frontend SSE stream, that event
projection is in scope even though the internal transport that produced it is
not.

Backend-to-Edge communication is explicitly outside this parity boundary.
Ignore `/api/v1/edge/*`, Edge HTTP and WebSocket registration/control/data
planes, Job tokens, Edge Command/Inbox, ACK/replay, Edge session
reconciliation, device execution locks, PostgreSQL advisory locks, and the
Backend HTTP/Scheduler process split when deriving the OS-to-frontend
Interface. Do not expose any of them as frontend routes or require the OS
internal device transport and persistence implementation to copy them.

This exclusion does not remove OS-local device execution. OS may retain or
redesign its internal Scheduler-to-driver communication, locking, recovery,
and persistence behind the frontend Interface. Those internals must still
produce the confirmed frontend-visible Task/Job states, REST behavior, and SSE
projections, but Backend's Edge protocol is neither their public contract nor a
migration-parity gate. Backend remains read-only.

## D-059: implement Workflow input as an OS-only execution extension

OS implements Workflow input before finalizing its Backend-compatible
`POST /api/v1/workflow-tasks` behavior. Keep three concepts distinct:

- the editable Workflow defines an ordered Workflow Input Contract;
- one WorkflowTask request supplies this run's `input` values; and
- WorkflowNode input bindings may reference values from that contract.

OS validates the complete Task input before creating or dispatching any Job,
applies declared Workflow-level defaults, rejects unknown, missing, or
type-invalid values, and captures the resolved input in the immutable
WorkflowTask snapshot. Resolution supplies Task-scoped Job parameters without
mutating the persisted Workflow or its WorkflowNode `param` values.

The request field remains wire-compatible with Backend
`feat/workflow@09609a2`, whose Handler accepts `input` but whose current Service
ignores it and persists `{}`. Effective Workflow input binding and execution
are therefore an explicit OS-only semantic extension until Backend implements
the same capability. A Workflow that depends on runtime input must not be
presented as executable by a Backend Graph/Execution Authority, and OS must
fail that unsupported authority choice explicitly before execution rather than
silently dropping values.

Migrate the current OS Workflow-parameter contracts and tests as the behavior
baseline: Python function-signature declarations, ordered parameters,
required/default distinction, strict unknown/missing/type preflight, and
runtime-parameter-to-Node-input binding. The Backend-shaped persistence
location, supported schema type surface, and exact binding representation are
decided separately.

## D-060: persist Workflow input contracts and Handle bindings in metadata

Do not create a pseudo Input WorkflowNode, virtual WorkflowEdge, or runtime
placeholder inside `WorkflowNode.param`. Preserve the Backend-shaped public
models and use a versioned, reserved metadata namespace:

```json
{
  "unilab": {
    "input_contract": {
      "version": 1,
      "parameters": []
    }
  }
}
```

The object above is stored at `Workflow.meta_data`. Each consuming
WorkflowNode stores bindings at
`WorkflowNode.meta_data.unilab.input_bindings`:

```json
{
  "<target_handle_uuid>": {
    "parameter": "plate_no"
  }
}
```

A binding key is the selected Graph Authority's real target
WorkflowHandleTemplate UUID. The referenced parameter name must exist exactly
once in the Workflow Input Contract. Do not use a Handle display name,
`data_key`, action argument spelling, list position, or a locally substituted
template identity as the binding identity.

For one target Handle, static `WorkflowNode.param` data, an incoming
WorkflowEdge, and a Workflow input binding are mutually exclusive providers.
Planning resolves the Handle Template's `data_key` to determine whether a
static non-null parameter supplies that Handle. A required Handle must have
exactly one provider after disabled/debug-scope pruning; an optional Handle may
have none. Ambiguous providers fail graph/authoring validation before Task
creation.

The `unilab.input_contract` and `unilab.input_bindings` namespaces are
server-managed semantic data, not arbitrary presentation metadata. Changing
either advances `Workflow.revision`. Python/source Apply persists the input
contract, all Node bindings, Nodes, Edges, normalized source, and source map in
one OS-local transaction. The ordinary shared Workflow metadata update must
not silently rewrite these reserved keys without the corresponding graph
revision and validation.

WorkflowTask creation copies the exact contract and bindings into its immutable
snapshot, validates and resolves Task `input`, and writes resolved values into
Task-scoped Job parameters. It never rewrites the persisted Workflow,
WorkflowNode `param`, or binding metadata. Backend may round-trip this metadata
but remains unable to execute its OS-only semantics under D-059.

## D-061: use ResourceSlot across Workflow and subworkflow boundaries

Workflow input v1 includes `ResourceSlot` in addition to the scalar parameter
types. Do not introduce a parallel `MaterialRef`, `WorkflowMaterial`, or generic
UUID parameter type for the same concept.

`ResourceSlot` is one logical Workflow value across:

- a root Workflow Input Contract and WorkflowTask `input`;
- a WorkflowNode input binding or output;
- a parent Workflow call argument;
- an inserted subworkflow input and output; and
- the final action parameter resolved to a complete PLR Resource.

Current subworkflows are statically expanded into the parent Workflow under a
persisted group scope. Their input arguments retain the caller's Binding; they
do not create another WorkflowTask or introduce a serialization boundary.
Therefore composition must not wrap, copy, or convert a ResourceSlot merely
because it crosses a subworkflow boundary.

Keep the executor binding distinct. `WorkflowNode.material_uuid` selects the
device Material that executes a device action; a ResourceSlot is business
material such as a sample, reagent, container, plate, or another resource that
flows through action Handles. Neither is an alias for the other.

The Workflow Input Contract uses the existing ResourceSlot schema/selector
semantics, and Python authoring uses `ResourceSlot` annotations. The external
Task input reference form, the canonical resolved ResourceSlot representation,
and when authority data is materialized are decided separately; these are
representations of the same domain value, not separate parameter types.

## D-062: separate external ResourceSlot references from internal transport

The external WorkflowTask input form for one `ResourceSlot` is a Material
Authority reference:

```json
{
  "uuid": "<material_uuid>"
}
```

The UUID is the stable Material identity. Do not require the legacy local
`id` field, and do not accept a frontend-, CLI-, or MCP-supplied flattened
resource tree as a substitute for an authority-owned Material.

Inside the OS execution boundary, a Node Handle may carry either a Material
reference object or the existing flattened, single-root resource list used by
device actions. These are transport forms of the same `ResourceSlot`, not
different Workflow parameter types. One shared ResourceSlot resolver must
normalize either internal form into the complete PLR Resource expected by the
action. Do not maintain separate root-input, Node-Handle, and subworkflow
resolvers.

A statically inserted subworkflow retains the caller's Binding unchanged.
Crossing the persisted group scope does not eagerly resolve, copy, wrap, or
serialize the ResourceSlot. Resolution occurs only at the ordinary consuming
Handle/action boundary, just as it does for a Node in the parent Workflow.

WorkflowTask input validation must resolve the submitted UUID against the
selected Material Authority before any Job is created. The exact supported
ResourceTemplate constraints and the immutable Task snapshot representation
are decided separately.

## D-063: constrain ResourceSlot inputs by allowed ResourceTemplates

A ResourceSlot parameter in the Workflow Input Contract may restrict the
Material templates that the caller can select:

```json
{
  "name": "sample",
  "schema": {
    "$slot": "ResourceSlot",
    "allowed_resource_template_uuids": [
      "<resource_template_uuid>"
    ]
  },
  "required": true
}
```

Reuse Backend's existing `allowed_resource_template_uuids` spelling. Do not
introduce a singular synonym or an OS-only template identifier. An omitted
field means that any existing Material template is accepted. A present array
must be non-empty, contain unique valid UUIDs, and is an exact allowlist. An
empty array is invalid rather than an alternate spelling for either "allow
all" or "allow none".

Version 1 performs exact equality against
`Material.resource_template_uuid`. It does not infer compatibility from
ResourceTemplate name, tag, class, hierarchy, package, or Python inheritance.
The frontend uses the same allowlist to filter its Material selector, but
frontend filtering is only a convenience and never replaces server
validation.

Before creating a WorkflowTask or any Job, OS resolves the submitted Material
UUID through the selected Material Authority and verifies that the Material
exists, is not deleted, and matches the allowlist when one is present. Any
missing, unavailable, deleted, or template-mismatched Material rejects the
request with the ordinary input-validation 422 response and leaves no
WorkflowTask or Job behind.

How a parent Workflow ResourceSlot constraint is checked against an inserted
subworkflow's constraint, and how the resolved Material is represented in the
immutable Task snapshot, are separate decisions.

## D-064: intersect ResourceSlot constraints during static composition

When a parent Workflow ResourceSlot parameter is bound to an inserted
subworkflow input, compilation derives the composite Workflow's effective
template constraint by set intersection. A missing
`allowed_resource_template_uuids` field represents the universe of available
Material templates for this operation:

- omitted intersect omitted remains omitted;
- omitted intersect a non-empty allowlist becomes that allowlist;
- two allowlists become their set intersection; and
- one parent parameter used by multiple inserted subworkflows is intersected
  with every corresponding child-input allowlist.

An empty effective intersection means there is no Material that can satisfy
the composed Workflow. Compile Preview must report this as a graph/source
diagnostic, and Apply must reject the Workflow without changing its persisted
graph, source, metadata, or revision.

A successful compilation returns and persists the effective constraint in the
parent Workflow Input Contract. Preview must show that effective contract so
an author or coding agent can see any narrowing before Apply. This is
composition of preconditions, not a delayed WorkflowTask validation rule:
static subworkflow expansion does not retain a second runtime input boundary.
WorkflowTask creation still validates the selected concrete Material against
the already compiled effective constraint under D-063.

This decision covers only a parent value bound to an inserted subworkflow
input. ResourceSlot output constraints and compatibility of values produced by
actions remain separate decisions.

## D-065: snapshot ResourceSlot selections as canonical references

Use Backend's existing WorkflowTask fields without adding a parallel snapshot
model or adapter:

- `WorkflowTask.workflow_snapshot` freezes the complete Graph, including the
  effective Workflow Input Contract and Node input bindings stored in
  metadata; and
- `WorkflowTask.input` freezes this run's fully validated and default-resolved
  input values.

The canonical ResourceSlot value stored in `WorkflowTask.input` is:

```json
{
  "uuid": "<material_uuid>",
  "resource_template_uuid": "<resource_template_uuid>"
}
```

The `resource_template_uuid` is the authority-resolved value used to satisfy
D-063, not a caller assertion. Scalar parameters are stored after defaults
have been applied, so `WorkflowTask.input` is the complete effective input and
not a copy of the raw request. Do not duplicate the input contract or bindings
inside Task `input` or Task metadata.

Do not snapshot the complete Material tree, mutable name, location, contents,
or state into the WorkflowTask. A Job whose Handle is bound directly to a
Workflow input receives the same canonical reference in its Task-scoped
`param`. At the consuming Handle/action boundary, the shared ResourceSlot
resolver reads the current complete Material tree by UUID. Values propagated
from upstream Handle outputs continue to use the internal forms allowed by
D-062.

This permits later Jobs to observe legitimate Material changes made by earlier
Jobs. If the referenced Material is deleted, unavailable, or otherwise cannot
be resolved after Task creation, the consuming Job fails before its executor
is invoked; it must not execute against a stale Task-time Material tree.
`WorkflowTask.input` and `workflow_snapshot` remain immutable after creation.

ResourceSlot output constraints and compatibility of values produced by
actions remain a separate decision.

## D-066: define a sibling Workflow Output Contract

Persist a versioned, ordered Workflow Output Contract at
`Workflow.meta_data.unilab.output_contract`. It is a sibling of, not a child
of, `Workflow.meta_data.unilab.input_contract`:

```json
{
  "unilab": {
    "input_contract": {
      "version": 1,
      "parameters": []
    },
    "output_contract": {
      "version": 1,
      "outputs": [
        {
          "name": "sample",
          "schema": {
            "$slot": "ResourceSlot",
            "allowed_resource_template_uuids": [
              "<resource_template_uuid>"
            ]
          }
        }
      ]
    }
  }
}
```

Output names are ordered, non-empty, and unique. Each version 1 output is
required to resolve; optional or branch-dependent outputs are not supported.
Output schemas use the same Workflow value vocabulary as input schemas,
including ResourceSlot and its `allowed_resource_template_uuids` spelling.

Python authoring continues to use exactly one final top-level
`return workflow_output(name=value, ...)` to define named output Bindings. Do
not create a pseudo Output WorkflowNode or virtual WorkflowEdge. When the
Workflow is statically inserted, the compiler substitutes each named child
output Binding into the caller's variable and creates no serialization or
WorkflowTask boundary. When it is the root Workflow, successful completion
resolves those Bindings into Backend's existing `WorkflowTask.output`.

Compile Preview returns the effective Output Contract. Apply persists it
atomically with the graph, source, source map, Input Contract, and input
bindings, and advances `Workflow.revision` for any semantic change. The
reserved `unilab.output_contract` key is server-managed graph-semantic state
and must not be rewritten by an ordinary metadata patch.

How ResourceSlot output constraints are inferred, how producer-to-consumer
compatibility is checked, and the external ResourceSlot output representation
are separate decisions.

## D-067: require ResourceSlot producer guarantees to satisfy consumers

Derive a ResourceSlot producer's possible ResourceTemplate set from an
authoritative contract:

- a pass-through Workflow input inherits that input's effective allowlist;
- an action output reads
  `WorkflowHandleTemplate.meta_data.unilab.allowed_resource_template_uuids`
  from the selected Graph Authority's real output Handle Template; and
- a statically inserted subworkflow output uses its compiled Workflow Output
  Contract.

Do not infer a business-material output from
`WorkflowNodeTemplate.resource_template_uuid`, which identifies the executor
device template. Do not guess it from a Python class, Handle display name,
action name, tag, or observed runtime example.

For a ResourceSlot connection, let `S` be the producer's possible template set
and `T` the consumer's accepted template set. Compilation requires `S` to be a
subset of `T`. An omitted allowlist means the universal set for this check:

- any producer can feed an unconstrained consumer;
- `[plate96]` can feed `[plate96, plate384]`;
- `[plate96, plate384]` cannot feed `[plate96]`; and
- an unconstrained producer cannot feed a constrained consumer because safety
  cannot be proven.

An incompatible connection is a Compile Preview diagnostic and makes Apply
fail without changing Workflow state. Existing action outputs without
allowlist metadata remain usable with unconstrained ResourceSlot consumers,
but must have their Handle Template metadata completed before they can feed a
constrained consumer.

Static proof does not replace runtime defense. A produced ResourceSlot UUID
must resolve through the Material Authority, and its actual
`resource_template_uuid` must satisfy the producer output Handle contract.
Violation fails the producing Job as an output-contract error and prevents
all dependent Jobs from dispatching. A root Workflow must likewise validate
all final ResourceSlot values against its Output Contract before it can
complete successfully.

Automatic same-name pass-through for Workflow ResourceSlot inputs and the
external `WorkflowTask.output` representation are separate decisions.

## D-068: synthesize same-name ResourceSlot pass-through for Workflows and actions

Implicit Resource Pass-through is a uniform contract rule at both composition
levels:

- for every Workflow ResourceSlot input without an explicit compatible
  same-name output, synthesize a same-name Workflow output; and
- for every action ResourceSlot input without an explicit compatible
  same-name action output, synthesize a same-name action output.

Mark the synthesized output as `implicit: true`. It binds directly to the
resolved input value, creates no WorkflowNode or WorkflowEdge, and inherits
the input's effective `allowed_resource_template_uuids`. Scalar inputs do not
receive implicit outputs. An explicit compatible same-name ResourceSlot output
wins; a same-name output of an incompatible type is a compile/template
contract error rather than a reason to rename or suppress the pass-through.

For an action, the device/driver implementation does not need to return the
implicit value. After a successful action result, the runtime adds any missing
implicit outputs from the action's resolved input bindings to the Node's
canonical output map. An explicit action result of the same compatible name
remains authoritative. A failed action produces no implicit outputs.

For a statically inserted subworkflow, an implicit pass-through does not add a
required Python assignment target. If the caller passes `sample=a`, the
caller's `a` remains the same ResourceSlot after the subworkflow's control
exits complete. For a root Workflow, implicit values are included in
`WorkflowTask.output`.

An optional ResourceSlot input also has a fixed same-name output key. Its
schema is nullable:

```json
{
  "name": "sample",
  "implicit": true,
  "schema": {
    "anyOf": [
      {
        "$slot": "ResourceSlot"
      },
      {
        "type": "null"
      }
    ]
  }
}
```

When that optional input is absent, normalized Task/Job input and the
successful output map contain `"sample": null`. The key still resolves, so
this does not introduce an optional or shape-changing Workflow output and
does not contradict D-066.

The persistent stable Handle UUID for a synthesized action output,
`List[ResourceSlot]` behavior, and the external WorkflowTask output
representation are separate decisions.

## D-069: persist implicit action outputs through template catalog sync

Synthesize an action's implicit ResourceSlot output while projecting the
registered action contract into the local Backend-shaped template catalog,
before WorkflowNodeTemplate/WorkflowHandleTemplate synchronization. The
persisted output Handle has:

```json
{
  "handle_key": "sample",
  "io_type": "source",
  "type": "ResourceSlot",
  "data_source": "result",
  "data_key": "sample",
  "meta_data": {
    "unilab": {
      "implicit_passthrough": true,
      "allowed_resource_template_uuids": [
        "<inherited-resource-template-uuid>"
      ]
    }
  }
}
```

Omit `allowed_resource_template_uuids` when the input is unconstrained. The
corresponding input Handle uses the same `handle_key` with `io_type=target`,
so the two do not collide.

Use Backend's existing Handle business identity
`(workflow_node_template_uuid, handle_key, io_type)`. The first catalog sync
creates the real UUID and later syncs upsert by that business key, preserving
the UUID. Do not derive a UUID with UUIDv5 or another local formula.

Workflow compilation only consumes the selected Graph Authority's persisted
WorkflowHandleTemplate and its real UUID. It must never create a per-Workflow
implicit Handle, invent a Handle UUID, or persist a WorkflowEdge against an
unpublished template identity. If the registered action contract requires an
implicit output but the local catalog does not contain it, Compile Preview
reports a stale/missing-template diagnostic and Apply fails. Compilation must
not silently synchronize or mutate the template catalog.

The runtime writes the implicit value under the Handle's `data_key` in the
canonical result map after successful action execution, making
`data_source=result` authoritative. Any removal and later reintroduction of a
catalog Handle follows the Backend template lifecycle and may require
dependent Workflows to be recompiled against the newly published UUID.

`List[ResourceSlot]` behavior and the external WorkflowTask output
representation remain separate decisions.

## D-070: represent ResourceSlot collections as lists of root dictionaries

Do not introduce a separate `ResourceSlotList` domain type.
`List[ResourceSlot]` is an ordered collection whose JSON shape is always
`list[dict]`. Each outer dictionary is one independent root Material and may
contain its own recursively nested `children: list[dict]` tree:

```json
[
  {
    "uuid": "<material-a-uuid>",
    "resource_template_uuid": "<template-a-uuid>",
    "children": [
      {
        "uuid": "<material-a-child-uuid>",
        "children": []
      }
    ]
  },
  {
    "uuid": "<material-b-uuid>",
    "resource_template_uuid": "<template-b-uuid>",
    "children": []
  }
]
```

An outer element must never be a `list[dict]`. Do not use sibling dictionaries
in the outer collection as the flattened nodes of one tree: in a
`List[ResourceSlot]` context, every outer dictionary is always a separate
ResourceSlot root. Therefore `list[dict | list[dict]]` and
`list[list[dict]]` are invalid collection representations.

External WorkflowTask input remains an ordered list of authority references
such as `[{"uuid":"..."}, {"uuid":"..."}]`; callers cannot upload nested
Material trees. After validation, `WorkflowTask.input` stores an ordered list
of the canonical `{uuid, resource_template_uuid}` references from D-065.
Inside Handle transport, each root dictionary may instead carry its nested
tree. The shared resolver processes each outer dictionary independently:
reference-only dictionaries are resolved through the Material Authority, while
embedded root trees are assembled into one PLR Resource each.

The collection schema is an array whose `items` is a ResourceSlot schema.
Apply `allowed_resource_template_uuids` to every item independently and
preserve order and duplicate UUIDs. A caller that needs different constraints
by position must use separately named ResourceSlot parameters. Empty arrays
are accepted unless `minItems` requires otherwise. Optional collections use
`null` for absence; `null` and `[]` are distinct.

D-068 same-name implicit pass-through applies to the complete collection,
preserving order and duplicates, and D-069 gives its action output Handle a
real catalog identity. The legacy flattened `list[dict]` accepted for a single
ResourceSlot remains a migration-only internal form; it must never be
interpreted as one tree when the declared type is `List[ResourceSlot]`, and
new collection producers must emit nested root dictionaries.

The external WorkflowTask output representation remains a separate decision.

## D-071: persist root Workflow Output Bindings in Workflow metadata

Persist root Workflow Output Bindings only at
`Workflow.meta_data.unilab.output_bindings`, as a graph-semantic sibling of
`unilab.input_contract` and `unilab.output_contract`.

Every Output Contract name has exactly one persisted Binding. WorkflowTask
creation copies the complete Workflow metadata, including these Bindings, into
the existing immutable `workflow_snapshot`. Root completion resolves output
from that snapshot; it never reads or recompiles the Authoring Draft.

Do not keep Output Bindings only in the private `workflow_authoring` table,
create an Output WorkflowNode or virtual WorkflowEdge, or add a parallel Task
snapshot field. `workflow_authoring` may retain source/source-map provenance
but is not the executable graph-semantic authority.

`unilab.output_bindings` is server-managed reserved metadata. An ordinary
shared Workflow metadata PUT must not rewrite it. OS Authoring Apply persists
it atomically with the Input/Output Contracts, Node input Bindings, graph,
normalized applied source, source map, and Workflow revision. Do not add it to
the shared Backend graph PUT body.

The discriminated Binding variants and their exact fields are decided
separately.

## D-072: root Output Bindings have two source variants in version 1

Version 1 supports exactly two root Workflow Output Binding variants:

```json
{
  "sample": {
    "kind": "workflow_input",
    "parameter": "sample"
  },
  "result": {
    "kind": "node_output",
    "workflow_node_uuid": "<workflow-node-uuid>",
    "source_handle_uuid": "<workflow-handle-template-uuid>"
  }
}
```

`workflow_input` references one exact parameter name from the Workflow Input
Contract and covers both explicit and D-068 implicit pass-through, including a
nullable optional value. `node_output` identifies one persisted WorkflowNode
and one real source WorkflowHandleTemplate from that Node's selected template.
Do not replace either UUID with a Node name, Handle display name, `handle_key`,
`data_key`, Job UUID, or result-map path.

Static subworkflow expansion substitutes a child Output Binding until it
becomes one of these two root variants. Do not persist a
`subworkflow_output`, `implicit_passthrough`, or group-boundary variant.

Version 1 has no literal or expression Binding. A derived output is produced by
a real persisted compute Node and binds to its real source Handle. This avoids
a second expression evaluator at Workflow completion.

Compile/Apply validates that every Output Contract name has exactly one valid
Binding and that no unknown Binding name is present. The exact v1 schema
compatibility rules remain part of the Workflow schema decision.

## D-073: persistent Authoring is a Workflow-scoped OS-only resource

Keep the three pure, stateless transformation routes from D-040 at the
top-level OS-only authoring namespace:

```text
POST /api/v1/authoring/compile
POST /api/v1/authoring/generate-python
POST /api/v1/authoring/validate
```

Expose persistent Authoring state only below one persisted Workflow:

```text
GET  /api/v1/workflows/{workflow_uuid}/authoring
PUT  /api/v1/workflows/{workflow_uuid}/authoring/draft
POST /api/v1/workflows/{workflow_uuid}/authoring/apply
```

`GET .../authoring` returns the aggregate Authoring view needed to rehydrate an
editor, including Draft, Applied Authoring Source, state, hashes, diagnostics,
and Candidate summary. `PUT .../authoring/draft` replaces the complete Draft
only; it never applies a graph, advances Workflow revision, creates a
WorkflowTask, or executes a device. `POST .../authoring/apply` is the explicit
command that performs precondition checks and atomically commits the selected
Candidate under D-041, D-060, D-066, and D-071.

The path is the sole Workflow identity; none of these request bodies repeats
`workflow_uuid`. Do not add a parallel top-level `workflow-authoring` resource,
turn the pure transformation routes into persistent commands, or append
Authoring fields to the shared Backend graph PUT.

## D-074: synchronize external Draft changes without replacing a dirty editor

The browser never reads or writes a local Workflow file directly. A displayed
`source_uri` is provenance and routing metadata, not permission to use a browser
filesystem API. OS is the sole reader, writer, watcher, compiler, and path
authority for the editable `workflow.py` Draft.

When OS detects that a coding agent or another local process changed the Draft,
it emits an invalidation/availability event through the existing
`/api/v1/events` SSE stream. The event is not a second source-of-truth payload.
If the frontend has no unapplied edit, it rehydrates the Workflow-scoped
Authoring aggregate with `GET .../authoring` and updates Python, Candidate DAG,
diagnostics, hashes, and the corresponding code/DAG synchronization state.

If either the Code or DAG view owns an unapplied Draft, the frontend must not
reload external source into that document. It preserves the complete editor
buffer and marks an external Draft change as pending. Loading a newer revision
must not clear `dirtyView`/`draft`, reset the Code editor, or silently regenerate
Python from the external Candidate.

The subsequent Draft save carries the source hash observed when the edit began.
If the OS file has changed, OS returns a conflict instead of silently
overwriting either version. The frontend shows the source difference and
requires explicit user confirmation before resubmitting an overwrite against
the newly observed hash. Do not add an unguarded save path or let an automatic
refresh discard human or coding-agent changes.

This decision fixes synchronization behavior, not the exact Draft request,
response, event-name, or conflict-body schema; those remain part of the
Workflow-scoped Authoring Interface decision.

## D-075: guard every persistent Draft write with source and graph CAS

`PUT /api/v1/workflows/{workflow_uuid}/authoring/draft` accepts this complete
write request:

```json
{
  "python_source": "from unilabos.workflow import ...\n",
  "expected_draft_hash": "sha256:<64-lowercase-hex>",
  "expected_workflow_revision": 7
}
```

`expected_draft_hash` is the hash observed when editing began. It may be `null`
only when the same Authoring GET reported that no Draft existed. The hash is
SHA-256 over the exact UTF-8 bytes written as `workflow.py`; line endings,
trailing newline, comments, and formatting are significant. Do not hash a
normalized AST, Candidate graph, JSON serialization, platform-default encoded
text, or stripped source.

`expected_workflow_revision` is the integer `Workflow.revision` observed with
that Draft. It prevents a Python edit from being silently reinterpreted after
another editor applies a graph change, even when the local file itself has not
changed.

OS serializes the operation with a per-Workflow Authoring lock, reads and hashes
the actual Draft file under that lock, then compares both tokens before any
write. A mismatch in either token returns `409` without replacing the file,
persisting a Candidate, advancing Workflow revision, creating a WorkflowTask,
or dispatching a device. There is no unguarded or `force` write path; after the
D-074 conflict review, an explicit overwrite resubmits against the newly
observed hash and revision.

On a match, OS atomically replaces the complete Draft file and compiles that
exact source. The Draft remains saved even if compilation reports syntax or
semantic diagnostics. The request never applies a Candidate and never
increments `Workflow.revision`. Exact success and conflict response DTOs remain
to be decided.

## D-076: Apply references one server-owned Candidate with three tokens

`POST /api/v1/workflows/{workflow_uuid}/authoring/apply` accepts only these
preconditions:

```json
{
  "expected_draft_hash": "sha256:<64-lowercase-hex>",
  "expected_workflow_revision": 7,
  "expected_candidate_hash": "sha256:<64-lowercase-hex>"
}
```

The request does not carry Nodes, Edges, Input/Output Contracts, Node input
Bindings, root Output Bindings, normalized source, source map, compiler
version, or template catalog data. Those values belong to the server-owned
Candidate selected by the opaque `candidate_hash`; accepting a client-provided
Apply bundle would let the submitted graph differ from the graph that was
compiled, diagnosed, diffed, and previewed.

The Candidate hash binds the complete graph-semantic Apply bundle, normalized
source and source map, compiler version, and authority-scoped template-catalog
fingerprint. Clients echo the token returned by OS and do not calculate or
interpret it.

Under the same per-Workflow Authoring lock used by Draft writes, OS checks the
actual Draft bytes, current `Workflow.revision`, and current valid Candidate.
It revalidates the selected Candidate against the current compiler and template
catalog before opening the SQLite Apply transaction. Any token mismatch or
revalidation change rejects the command without applying a different
Candidate.

A semantic Apply atomically persists the complete graph, reserved contract and
Binding metadata, normalized Applied Source, source map, compiler/catalog
provenance, and incremented Workflow revision. A proof-equivalent source-only
Apply updates only the Authoring association and retains the current Workflow
revision under D-037. Apply never creates or starts a WorkflowTask; Run remains
a later `POST /api/v1/workflow-tasks`.

Exact success and error DTOs remain to be decided.

## D-077: Authoring GET returns one self-consistent editor aggregate

`GET /api/v1/workflows/{workflow_uuid}/authoring` returns the normal Backend
success envelope with one complete aggregate in `data`:

```json
{
  "workflow_uuid": "<uuid>",
  "workflow_revision": 7,
  "state": "unapplied_graph",
  "applied_graph": {
    "workflow": {},
    "nodes": [],
    "edges": [],
    "node_templates": [],
    "handle_templates": []
  },
  "draft": {
    "source_uri": "workflows/<uuid>/workflow.py",
    "python_source": "...",
    "draft_hash": "sha256:<64-lowercase-hex>",
    "update_time": "<RFC3339>",
    "diagnostics": []
  },
  "candidate": {
    "candidate_hash": "sha256:<64-lowercase-hex>",
    "base_workflow_revision": 7,
    "draft_hash": "sha256:<64-lowercase-hex>",
    "graph": {
      "workflow": {},
      "nodes": [],
      "edges": [],
      "node_templates": [],
      "handle_templates": []
    },
    "normalized_python_source": "...",
    "source_map": [],
    "changeset": {
      "kind": "graph",
      "created_node_uuids": [],
      "updated_node_uuids": [],
      "deleted_node_uuids": [],
      "created_edge_uuids": [],
      "updated_edge_uuids": [],
      "deleted_edge_uuids": [],
      "reserved_metadata_changed": false
    },
    "compiler_version": "...",
    "template_catalog_fingerprint": "sha256:<64-lowercase-hex>",
    "update_time": "<RFC3339>"
  },
  "applied_source": {
    "python_source": "...",
    "source_hash": "sha256:<64-lowercase-hex>",
    "workflow_revision": 6,
    "source_map": [],
    "compiler_version": "...",
    "template_catalog_fingerprint": "sha256:<64-lowercase-hex>",
    "update_time": "<RFC3339>"
  }
}
```

`applied_graph` reuses the exact frozen Backend Graph projection, including
Node and Handle Templates, so a single read supplies one consistent editor
snapshot. `candidate.graph` has the same shape. `draft` is `null` when no Draft
file exists; this is a successful aggregate read, not a 404. `candidate` is
`null` unless the current Draft has one complete, valid, current Candidate.
Never return an earlier Candidate as if it belonged to the current Draft.
`applied_source` is `null` until an Applied Source record exists.

Source-map entries use Backend identities:

```json
{
  "workflow_node_uuid": "<uuid>",
  "start_line": 10,
  "start_column": 1,
  "end_line": 15,
  "end_column": 2
}
```

All collections use `[]`, never `null`; only absent singular resources use
`null`. The top-level `state` is computed by OS and has exactly these values:

| State | Required frontend Chinese label |
|---|---|
| `draft_missing` | `尚无 Python 草稿` |
| `compiling` | `正在检查工作流…` |
| `draft_invalid` | `草稿存在错误，当前仍使用已保存的工作流` |
| `candidate_stale` | `预览已过期，请重新检查工作流` |
| `unapplied_source_only` | `源码有尚未应用的修改，工作流图未变化` |
| `unapplied_graph` | `工作流有尚未应用的修改` |
| `applied` | `源码与工作流已同步` |
| `applied_source_stale` | `源码与已保存的工作流不一致` |

Structured diagnostics carry stable machine codes and directly displayable
Chinese `message` values. The frontend must not infer a different state from
independently fetched resources.

## D-078: Draft PUT and Apply return rehydratable success state

Successful Draft PUT returns HTTP 200 with the same aggregate and the same
`{"code":0,"data":...}` shape as D-077 GET. Persisting an invalid Draft is
still HTTP 200 with `state=draft_invalid`, diagnostics, and no Candidate:
compilation failure does not mean the file write failed.

Required frontend Chinese success messages are:

- valid graph change: `草稿已保存，有尚未应用的工作流修改`;
- valid source-only change: `草稿已保存，仅源码发生变化`;
- invalid source: `草稿已保存，但存在错误，修复后才能应用`.

Successful Apply returns HTTP 200:

```json
{
  "code": 0,
  "data": {
    "apply_result": {
      "kind": "graph",
      "previous_workflow_revision": 7,
      "workflow_revision": 8,
      "applied_candidate_hash": "sha256:<64-lowercase-hex>",
      "applied_source_hash": "sha256:<64-lowercase-hex>",
      "warnings": []
    },
    "authoring": {}
  }
}
```

`authoring` is the complete post-Apply D-077 aggregate. `kind=graph` means a
semantic transaction advanced Workflow revision and displays
`工作流已应用，当前版本为 {workflow_revision}`. `kind=source_only` retains
the revision and displays `源码已应用，工作流图未发生变化`.

If the SQLite Apply transaction commits but normalized-source writeback fails,
Apply remains successful and returns this warning:

```json
{
  "code": "draft_writeback_pending",
  "message": "工作流已应用，但本地源码同步失败；OS 已保留可恢复的源码记录。"
}
```

Do not return an Apply failure after the authoritative transaction committed;
that would invite a duplicate Apply. Warning arrays are empty `[]` when no
warning exists.

## D-079: Authoring errors retain the Backend envelope and use specific codes

Authoring errors use the frozen Backend envelope:

```json
{
  "code": 409,
  "error": {
    "code": "draft_hash_conflict",
    "message": "草稿已被其他程序修改，请重新获取并比较差异。"
  }
}
```

Error responses never embed complete Draft source, Candidate graph, or a
replacement aggregate. A caller rehydrates with D-077 GET and must keep any
dirty editor buffer separate from the returned remote source.

| HTTP | `error.code` | Required frontend Chinese message |
|---:|---|---|
| 400 | `invalid_input` | `提交内容格式不正确` |
| 404 | `workflow_not_found` | `工作流不存在或已被删除` |
| 409 | `draft_hash_conflict` | `草稿已被其他程序修改，请查看差异后再保存` |
| 409 | `workflow_revision_conflict` | `工作流已在其他位置更新，请刷新并重新确认本次修改` |
| 409 | `candidate_hash_conflict` | `预览结果已变化，请重新检查 DAG 和源码差异` |
| 409 | `template_catalog_conflict` | `设备动作模板已更新，请重新编译并检查工作流` |
| 409 | `candidate_not_ready` | `当前草稿尚未生成可应用的工作流` |
| 422 | `draft_invalid` | `草稿存在错误，修复后才能应用` |
| 422 | `candidate_invalid` | `工作流校验失败，请检查节点、连线和输入输出` |
| 503 | `template_catalog_unavailable` | `设备动作模板暂不可用，请稍后重试` |
| 500 | `internal_error` | `本地工作流服务出现错误，请重试或查看日志` |

Draft PUT never returns 422 merely because the successfully persisted Python is
invalid. The 422 codes apply when a caller attempts to Apply content that
cannot become an executable Workflow.

When several Apply preconditions differ, OS reports the first conflict in this
fixed order: Draft hash, Workflow revision, template catalog, then Candidate
hash. Conflict handling always preserves local frontend state and rehydrates
remote state before offering a source or graph diff.

## D-080: Authoring uses one durable invalidation SSE event

OS projects every committed Authoring-state change through the existing
`GET /api/v1/events` stream as one event named
`workflow.authoring.changed`. Its data is deliberately small:

```json
{
  "workflow_uuid": "<uuid>",
  "cause": "external_draft_changed",
  "workflow_revision": 7,
  "draft_hash": "sha256:<64-lowercase-hex>",
  "candidate_hash": "sha256:<64-lowercase-hex>"
}
```

`draft_hash` and `candidate_hash` are nullable. `cause` is exactly one of
`external_draft_changed`, `draft_saved`, `draft_compiled`, `applied`, or
`recovered`. The event is only a version/invalidation signal: it never carries
Python source, a graph, diagnostics, an Authoring aggregate, or another state
authority. A client rehydrates through the D-077 Authoring GET.

The file watcher debounces and coalesces filesystem notifications, then takes
the per-Workflow lock, reads a complete file, and compares its exact UTF-8 byte
hash. A known same-hash write is ignored, including an OS write observed again
by the watcher. A real change is compiled and its Draft/Candidate state plus a
durable frontend event are committed in the same SQLite transaction. Apply
likewise commits graph, Authoring state, and its frontend event in one
transaction. An SSE frame may be sent only after that transaction commits.
`draft_compiled` is used only when compilation completion is itself a separate
persisted transition; OS must not emit gratuitous save-plus-compile duplicates
for one already-complete transition.

Reuse the Backend stream's globally increasing event `id`,
`Last-Event-ID` replay, and client-side id deduplication. Multiple events may be
coalesced into one Authoring GET; clients must not assume one event per file
write. A successful Draft PUT or Apply response already contains the complete
aggregate, so a later SSE event with the same
`workflow_revision`/`draft_hash`/`candidate_hash` tuple is ignored.

A clean frontend document rehydrates and may show `已同步外部修改`. A dirty
document does not load remote source or graph into its current buffers and
shows `本地草稿已在编辑器外发生变化，当前修改尚未覆盖。`; the existing hash
conflict and explicit comparison flow remains authoritative.

## D-081: the lab workspace owns runtime data and package Python owns the Draft

The unit of local isolation is one **lab workspace**. In the current deployment
stage, one domain-device package repository may be exactly one lab workspace;
the two concepts remain distinct so a future workspace can load several
packages without splitting Workflow, Task, Material, or event history.

`BasicConfig.working_dir` is selected once at OS startup and points to the
workspace's ignored runtime-data child, conventionally:

```text
<lab-workspace>/
├── package.yaml / deployment / package source
├── <python-package>/workflows/*.py
└── unilabos_data/                 # BasicConfig.working_dir
    ├── workflow.db
    ├── logs/
    ├── community_devices/
    └── .trash/
```

Switching to another lab workspace therefore selects another `working_dir`.
Adding, upgrading, or selecting another device package inside the same
workspace does not. A running process does not hot-mutate the directory. Never
scatter the new Workflow store across the legacy
`ULAB_WORKFLOW_HISTORY_DB`, `ULAB_DEVICE_STATE_DB`, or
`~/.unilabos/*.db` defaults.

For a registered editable domain package, its version-controlled
`workflows/*.py` file is the sole Authoring Draft. Do not create a second
editable copy under `working_dir`. SQLite `workflow.db` remains authoritative
for the Backend-shaped Applied Workflow, private applied-source/source-map
association, WorkflowTask snapshots, Jobs, results, durable frontend events,
and recovery metadata. A WorkflowTask always runs its SQLite snapshot and
never imports the package Draft.

The package registration fixes a one-to-one mapping from stable
`workflow_uuid` to one package-relative source path. Files keep meaningful
names, while their Python contains the confirmed stable Workflow and Node UUID
anchors. Expose a logical URI such as:

```text
package://szlab_poly_studio/workflows/magnetic_stirring.py
```

The URI is provenance, not an arbitrary filesystem path accepted from a Draft
request. Persistent Authoring endpoints resolve only pre-registered paths below
an explicitly loaded editable package root, require a regular UTF-8 file, reject
traversal and symlinks, and preserve the mapping in private Authoring state.
Package discovery must register specific sources; declaring only a compiler
codec or blindly scanning every `.py` file is not a source-identity contract.

Draft lifecycle follows the authority of each transition:

1. A frontend Draft PUT checks the actual package file hash and Workflow
   revision under the per-Workflow lock, compiles the proposed complete bytes,
   atomically replaces that package file, then commits the derived
   Draft/Candidate state and D-080 event. If the derived transaction fails
   after file replacement, immediate or startup reconciliation treats the file
   as the Draft authority; it never rolls the source back to stale bytes.
2. A coding-agent or Git change is debounced until stable, read and hashed under
   the same lock, compiled, and projected into SQLite plus the durable event in
   one transaction. Same-hash OS writes are ignored.
3. Apply first commits the Backend-shaped graph, normalized Applied Source,
   source map, revision, recovery marker, and event in SQLite. Only after commit
   may OS atomically write explicitly accepted normalized source back to the
   same package file. A failure keeps Apply successful and
   `draft_writeback_pending`; recovery may write only while the file is absent
   or still matches the pre-Apply hash, never after a coding agent changed it.
4. Startup reconciles every registered Workflow against its package file. A
   present file is hashed and compiled; an absent file yields
   `draft=null`, `candidate=null`, and `draft_missing`. GET is read-only and
   never creates or overwrites package source.
5. External deletion or rename does not delete the Applied Workflow or follow
   the renamed path. Restoring the canonical registered path recompiles it and
   emits `cause=recovered`. Missing or stale source never prevents explicit
   execution of the last Applied graph.
6. Explicit Workflow deletion must never recursively remove a package or
   workspace. Any recoverable source archival goes below
   `working_dir/.trash`; package Git history remains the source-level recovery
   mechanism.

Checked-in lab configuration may live in the same workspace, but credentials
and real hardware secrets remain environment or ignored local overrides.
Installed/read-only wheels are execution or import sources, not editable
Authoring workspaces.

## D-082: Workflow value schemas use one finite version-1 type set

Workflow Input and Output Contracts use the same version-1 value vocabulary.
The supported non-null Python forms are exactly:

```python
str
int
float
bool
dict[str, JSONValue]
ResourceSlot
list[str]
list[int]
list[float]
list[bool]
list[dict[str, JSONValue]]
list[ResourceSlot]
```

`JSONValue` means recursively valid JSON inside an object: null, boolean,
number, string, array, or object. The contract validates a
`dict[str, JSONValue]` as one opaque structured value; version 1 does not expose
or generate a field-by-field typed object model. The frontend edits it with a
JSON editor instead of synthesizing a nested form. A ResourceSlot must remain a
declared ResourceSlot and must not be hidden inside an opaque JSON object.

Every list form is ordered, homogeneous, and one-dimensional at the declared
type level. `list[dict[str, JSONValue]]` may contain opaque JSON objects, whose
contents may themselves be valid recursive JSON; this does not make a nested
Workflow list type. `list[ResourceSlot]` retains all D-070 representation and
resolution rules.

Version 1 does not support `Any`, bare `object`, heterogeneous lists, tuple,
set, nested declared lists, arbitrary unions, bytes/files, datetime, Decimal,
or Python/Pydantic custom object models. Nullable is a wrapper over a supported
type and its exact missing/default/null semantics are decided separately.

## D-083: Workflow values are strictly typed without convenience coercion

Authoring defaults, persistent Draft compilation, WorkflowTask input, resolved
Task snapshots, and Workflow output validation use the same strict value
validator. Strings are never parsed as numbers or booleans; `0`/`1` are not
booleans; a UUID string is not a ResourceSlot shorthand; and list elements are
never coerced independently.

The version-1 rules are:

- string accepts only a JSON string;
- boolean accepts only a JSON boolean;
- integer accepts a JSON number with no fractional component, explicitly
  excluding boolean, and normalizes a mathematical `3.0` to integer `3`;
- number accepts finite JSON integer or fractional numbers, explicitly
  excluding boolean and rejecting NaN or infinities;
- object accepts only an object with string keys and recursively valid JSON
  values;
- `list[T]` accepts only an array whose every element strictly validates as
  `T`; and
- ResourceSlot accepts only the declared reference object form and is then
  resolved under D-062/D-063. It never accepts a bare UUID string.

The only numeric widening is integer input satisfying number. Mathematical
integer normalization is JSON Schema integer semantics, not general type
conversion. The complete normalized value, including normalized integers, is
what enters immutable `WorkflowTask.input`.

A frontend control may parse its own textual editing state into the correct JSON
type before submission. CLI/MCP callers must likewise submit correctly typed
JSON. Server validation failure returns 422 before creating a WorkflowTask or
any Job and never falls back to string parsing or driver-specific conversion.

## D-084: top-level Task input null has the same meaning as omission

Only while normalizing the declared top-level Workflow inputs of
`POST /api/v1/workflow-tasks`, treat an explicit JSON `null` exactly as though
that key had been omitted. Apply this normalization before default filling and
missing-required validation:

- a required input with neither a default nor nullability rejects both omission
  and explicit null with 422;
- an optional input with a non-null default uses that default for both omission
  and explicit null; and
- a nullable input whose default is null resolves both omission and explicit
  null to null.

Version 1 therefore exposes only three useful declaration shapes: required
`T` without a default, optional `T` with a non-null default, and optional
`T | None` with a null default. Do not expose a required-nullable input or a
nullable input with a non-null default, because callers cannot select explicit
null under this rule.

The rule applies only to declared top-level Task input values. An unknown key
whose value is null remains an unknown key and fails validation. Null inside an
opaque JSON object retains its JSON meaning; null list elements are not
omissions and fail the non-null homogeneous item schema. Empty arrays, empty
objects, empty strings, zero, and false remain explicit values.

For example, `sample: ResourceSlot` rejects both null and omission;
`sample: ResourceSlot | None = None` resolves both to null; and
`samples: list[ResourceSlot] = []` resolves both to an empty list. Existing
ResourceSlot default restrictions remain: a contract never embeds a non-null
material UUID default, and a ResourceSlot-list default cannot preselect
materials.

Persist only the fully validated, default-filled canonical input in immutable
`WorkflowTask.input`; it intentionally does not retain whether the caller sent
null or omitted the key. Do not apply this equivalence to graph or authoring
PATCH semantics, where omission means unchanged and explicit null may clear a
nullable field, or to Workflow outputs, where every declared output key must be
present and a nullable output is explicitly null.

## D-085: Workflow schema constraints use one finite JSON vocabulary

Version 1 supports only the following validation keywords in Workflow Input
and Output Contracts:

- scalar values may use a non-empty, unique, strictly typed `enum`;
- integer and number values may use inclusive `minimum` and `maximum`;
- strings may use `minLength` and `maxLength`;
- lists may use `minItems` and `maxItems`; and
- ResourceSlot and `list[ResourceSlot]` may use the existing
  `allowed_resource_template_uuids` exact allowlist from D-063/D-070.

Each parameter descriptor may also carry `title` and `description` as
non-validating frontend presentation metadata. Keep them outside the value
schema and never use either as parameter identity. The parameter `name`
remains its contract and Python binding identity.

All bounds are finite, inclusive, non-negative where they describe length, and
internally consistent. Enum members must satisfy the declared base type and all
other constraints. Every non-null default is validated by the same strict
validator during compilation. Unsupported or contradictory keywords, an empty
enum, a default outside its schema, or a minimum greater than a maximum make
Compile Preview invalid and block Apply.

The same constraints validate Authoring defaults, WorkflowTask input, the
immutable Task snapshot, and Workflow output. A permitted nullable output whose
value is null bypasses its non-null value constraints; D-084 first translates
top-level Task input null to omission. Frontend controls may use the schema for
early feedback, but server validation remains authoritative.

Version 1 deliberately excludes `pattern`, `format`, exclusive numeric bounds,
`multipleOf`, `uniqueItems`, arbitrary object-property schemas, and every
unlisted JSON Schema keyword. In particular, ResourceSlot list order and
duplicate Material references remain legal under D-070. Opaque JSON objects
remain recursively JSON-valid values edited and validated as a whole rather
than acquiring a nested typed form.

## D-086: Workflow contracts and their top-level values are closed

Treat each version-1 Input/Output Contract envelope, parameter descriptor, and
value schema as a closed object. Only fields explicitly defined for that
object and contract kind are legal. An unknown field is a compile diagnostic
that prevents a Candidate and Apply; it is never ignored, round-tripped as an
uninterpreted extension, or interpreted as frontend-only metadata. A future
extension requires a reviewed contract version rather than an accidental
version-1 spelling.

WorkflowTask input is likewise a closed top-level mapping. Every submitted key
must name exactly one declared input parameter. Any unknown key, including one
whose value is null, returns 422 before a Task or Job is persisted. Empty input
is valid only when all declared inputs can be resolved after D-084 default/null
normalization.

The final Workflow output is a closed top-level mapping over the declared
Output Contract. A runtime producer that claims an unknown contracted output
key violates its output contract: attribute the failure to that producer,
block its downstream consumers, and never copy the unknown value into
`WorkflowTask.output`. The separate output required/null/default decision
determines how declared-but-missing keys behave.

The external ResourceSlot input object is also closed and contains only
`{"uuid": "<material_uuid>"}`. Reject caller-supplied
`resource_template_uuid`, names, children, flattened resources, or arbitrary
metadata. The selected Material Authority supplies canonical template identity
and other authoritative data after lookup; external callers cannot inject it.
Apply the same rule to every member of `list[ResourceSlot]`.

The deliberate exception is a value declared as
`dict[str, JSONValue]`: its contents are an opaque recursively valid JSON
object, so arbitrary string keys are data rather than schema extensions.
Closing `input_contract` and `output_contract` never removes or rejects other
separately defined siblings in `Workflow.meta_data.unilab`.

## D-087: every declared Workflow output resolves and outputs have no defaults

A version-1 Output Contract descriptor contains neither `required` nor
`default`. Every declared output key is required to resolve before a root
WorkflowTask can succeed. Non-null `T` must resolve to a non-null value that
strictly satisfies its schema; nullable `T | None` must still produce the key
but may explicitly resolve it to null. Missing and explicit null are therefore
distinct on the output side, and D-084 never applies to output.

Do not synthesize a default to conceal a missing producer result. A missing
key, null for a non-null output, unknown key, type mismatch, or constraint
violation is an output-contract failure. The Task must not transition to
`succeeded`, and no invalid final output may be committed or projected as a
successful result. Validate the complete output before the terminal success
transition.

A Workflow with no Output Contract succeeds with `{}`. D-068 ResourceSlot
pass-through is a produced Binding rather than a default: after successful
execution the runtime writes the same-name key explicitly, and an absent
optional ResourceSlot input produces an explicitly nullable same-name output
with value null.

This decision governs the shape of a successful final Workflow output. The
external ResourceSlot serialization and whether a failed or canceled Task may
expose separately identified partial results remain in their dedicated output
representation decision. Backend's non-null `WorkflowTask.output` field may
remain initialized to `{}` without treating that initial value as a completed
output.

## D-088: Workflow parameters share Action-style annotations and documentation

Workflow and Action function parameters use one annotation-to-schema parser.
The Python type annotation is the sole type authority. Pydantic `Field`
metadata may carry parameter `title`, `description`, and the finite constraints
accepted by D-085; it never substitutes for the type annotation. A parameter's
actual default remains the literal expression after `=`, not
`Field(default=...)`.

Support both Pydantic metadata and the existing Uni-Lab function-docstring
parameter syntax:

```python
def prepare(
    temperature: Annotated[
        float,
        Field(
            title="处理温度",
            description="目标处理温度，单位为摄氏度。",
            ge=20,
            le=100,
        ),
    ],
):
    """Args:
        temperature[反应温度]: 整个预处理阶段使用的温度，单位为摄氏度。
    """
```

After trimming whitespace, resolve parameter presentation metadata
independently:

- if only `Field` or only the docstring supplies a non-empty title or
  description, use that value;
- if both supply the same non-empty value, use it once; and
- if both supply conflicting non-empty values, the Pydantic `Field` value wins.

An absent docstring therefore does not weaken a complete Pydantic declaration,
while existing Action-style source remains usable and an explicit structured
Field remains authoritative over prose. Apply the same precedence in Action
catalog projection and Workflow compilation so their frontend forms do not
disagree. This decision covers parameter type/default ownership and presentation
metadata precedence; the exact accepted Field boundary arguments, enum
annotation rules, and final normalized Workflow syntax remain to be closed.

Workflow compilation remains AST-only and never executes authoring source,
Pydantic `Field`, imports, decorators, or annotations. Enhance the current
Action parser rather than copying its permissive fallbacks: an unsupported or
unparseable Workflow annotation is a diagnostic, not an inferred string or
object schema.

## D-089: accept both nullable spellings and normalize to `T | None`

The shared Action/Workflow annotation parser accepts both
`typing.Optional[T]` and `T | None` for every nullable wrapper over a
version-1 type. They have exactly the same contract meaning. Deterministic
normalized Workflow Python always emits `T | None`; it does not preserve the
author's spelling or generate an `Optional` import.

`Optional` describes value nullability, not call-site omission. Under D-084,
a nullable Workflow input is valid only in the optional declaration
`T | None = None`. Reject a required-nullable declaration such as
`sample: Optional[ResourceSlot]` without a default, and reject a null default
against a non-null annotation such as `sample: ResourceSlot = None`.

Keep nullable collection and empty collection distinct:

```python
samples: list[ResourceSlot] = []
samples: list[ResourceSlot] | None = None
```

The first resolves omitted/null Task input to an empty list through its
default; the second resolves it to null. Successful pass-through output is
respectively `[]` or explicit null. A frontend, CLI, MCP caller, compiler, and
runtime must observe the same distinction.

## D-090: Field bounds are optional and ResourceSlot constraints name resources

Pydantic `Field` is optional in an Action or Workflow parameter annotation. A
bare supported type declares no numeric or length boundary; it remains fully
strictly typed under D-083. When present, exactly one `Field` in `Annotated`
may use only:

- `ge`/`le` for inclusive integer or number `minimum`/`maximum`;
- `min_length`/`max_length` for string `minLength`/`maxLength` or list
  `minItems`/`maxItems`; and
- `title`/`description` under D-088.

Reject exclusive bounds, `multiple_of`, `pattern`, `strict`, aliases,
`default`, `default_factory`, and every other unapproved Field argument.
Integer bounds are mathematical integers, number bounds are finite JSON
numbers, lengths are non-negative integers, and every lower bound must not
exceed its upper bound. Absence of `Field` means absence of these boundaries,
not an alternate boundary syntax.

Do not put a `ResourceSlot(...)` instance in the type position and do not write
ResourceTemplate UUIDs through `Field.json_schema_extra`. Express the domain
constraint as separate standard `Annotated` metadata:

```python
sample: Annotated[
    ResourceSlot | None,
    AllowedResourceTemplates(corning_96_well_plate),
    Field(title="样品板", description="本次实验使用的微孔板。"),
] = None
```

`AllowedResourceTemplates(...)` accepts one or more symbols registered by
`@resource`, whether the decorated symbol is a factory function or a class.
The Workflow compiler resolves an imported symbol statically to its
`module:symbol`, registered resource id, and selected Template Catalog's
current ResourceTemplate UUID. It never imports or executes the Workflow
source or calls the resource factory. Missing, unregistered, stale, or
authority-mismatched symbols are compile diagnostics and prevent Apply.

The Draft retains readable imported symbols while the effective Applied
Contract stores the unique non-empty `allowed_resource_template_uuids`
allowlist required by D-063. An absent `AllowedResourceTemplates` means an
unconstrained ResourceSlot. Do not allow the symbolic annotation and a
hard-coded UUID allowlist simultaneously. For `list[ResourceSlot]`, the
resolved allowlist applies independently to every item; for a nullable slot it
applies whenever the value is non-null.

Deterministic normalized source orders `AllowedResourceTemplates` before
`Field`. Action catalog projection and Workflow compilation consume the same
metadata syntax, although the remaining Action contract decision still owns
its complete input/output Handle projection.

## D-091: scalar enum annotations use Literal with strict prevalidation

Use `typing.Literal[...]` as the only Python enum syntax for Workflow and
Action scalar values. Do not add `AllowedValues`, custom Enum classes, or
`Field.json_schema_extra["enum"]`. Version 1 accepts non-empty Literal values
that resolve to one strict scalar family:

- all strings produce a string enum;
- all booleans produce a boolean enum;
- all integers, explicitly excluding booleans, produce an integer enum; and
- finite integer/fractional numeric mixtures produce a number enum.

Reject every other mixture, null inside Literal, non-finite values, empty or
duplicate members, and Literal over ResourceSlot or opaque objects. Preserve
declaration order for frontend choice presentation. A non-null default must be
a member after D-083 normalization and must also satisfy any Field constraint.

Nullable remains a wrapper outside the Literal and is normalized under D-089:

```python
mode: Literal["fast", "safe"] | None = None
```

A homogeneous list may constrain every non-null item with Literal and constrain
the collection itself with Field:

```python
modes: Annotated[
    list[Literal["fast", "safe"]],
    Field(min_length=1, max_length=5),
] = []
```

Do not allow nullable list items; only the complete list may be nullable.

Pydantic's Literal validator is not the runtime authority: even its strict mode
can equate boolean `True` with integer `1`. Always validate the D-083 base type
first, normalize mathematical integers as already specified, and only then
test canonical enum membership. Action input, WorkflowTask input, defaults,
list items, snapshots, and output use this same ordering. Enhance the current
Action Literal parser, which incorrectly labels every Literal as string, so
Action and Workflow schemas agree.

## D-092: normalized Workflow Python is statically typed and template-directed

The sole normalized Workflow Python form is one deterministic AST-only
authoring language. A Workflow module imports its finite annotation vocabulary,
symbolic ResourceTemplates, device-template classes, and authoring markers;
declares exactly one decorated Workflow function; declares device selectors at
module scope; emits one anchored action/control statement per persisted Node;
and optionally ends with one top-level `workflow_output(...)`.

Use this declaration shape:

```python
from typing import Annotated, Literal

from pydantic import Field
from szlab_poly_studio.materials import beaker_500ml
from szlab_poly_studio.reactor import Reactor
from unilabos.registry.annotations import AllowedResourceTemplates
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.workflow.authoring import (
    device,
    workflow_definition,
    workflow_output,
)


reactor: Reactor = device()


@workflow_definition(
    workflow_uuid="8feecdda-3898-4afc-9735-4f1ac59553fd",
    displayname="样品制备",
    description="完成样品预处理并生成检测报告。",
)
def prepare_sample(
    *,
    sample: Annotated[
        ResourceSlot,
        AllowedResourceTemplates(beaker_500ml),
        Field(title="样品容器", description="本次工作流处理的样品容器。"),
    ],
    mode: Annotated[
        Literal["fast", "safe"],
        Field(title="运行模式", description="快速模式或安全模式。"),
    ] = "safe",
    cycles: Annotated[int, Field(ge=1, le=10)] = 3,
    note: Annotated[str | None, Field(max_length=200)] = None,
):
    # unilab:node_uuid=91558127-bc4a-495c-bb22-d9d1df4e4f7f
    result = reactor.prepare(
        sample=sample,
        mode=mode,
        cycles=cycles,
        note=note,
    )

    return workflow_output(report=result.report)
```

`workflow_definition` accepts `workflow_uuid`, `displayname`, and
`description`. Do not retain the old `workflow_id`, source-owned `revision`, or
`parameter_ui` fields. The Workflow-scoped route/registered source identity and
decorator `workflow_uuid` must match. `displayname` and `description` project
to the existing Backend Workflow name/description fields; tags and unrelated
Workflow metadata remain outside Python.

Root Workflow parameters are keyword-only. Types, constraints, presentation
metadata, nullability, enums, ResourceTemplate restrictions, and defaults
follow D-082 through D-091. Deterministic normalization emits built-in
`list[...]`/`dict[...]`, `T | None`, effective structured `Field` metadata,
absolute sorted imports, and `AllowedResourceTemplates` before `Field`.
Docstring parameter metadata remains accepted input under D-088, but
normalization materializes the effective parameter title/description into
`Field` so the canonical output has one structured representation.

Do not use a Workflow return annotation or custom output model. Compile exactly
one final top-level `return workflow_output(name=value, ...)` into the explicit
Output Contract and Bindings from D-066/D-071/D-072. Infer each output schema
from its authoritative Binding producer. D-068 implicit same-name ResourceSlot
pass-through remains absent from the explicit return call and is synthesized
by compilation.

### Device template annotations and instance selection

Every device authoring selector is a module-scope annotated assignment whose
annotation is the template and whose `device(...)` call only selects an
execution mode:

```python
# Each Node/Job independently selects one available Reactor instance.
reactor: Reactor = device()

# Every Node using this selector is pinned to one registered instance.
fixed_reactor: Reactor = device("reactor-1")
```

The annotation must statically resolve through imports to exactly one
`@device`-registered template symbol in the selected Template Catalog.
`device()` accepts either no argument or one non-empty string literal
`device_id`; reject missing annotations, template arguments, keywords, null,
computed values, and additional arguments. The compiler never imports or
executes the authoring module, decorators, device class, or driver. It resolves
the annotation and `@action` method statically, then verifies the Backend-issued
ResourceTemplate and WorkflowNodeTemplate identities against the selected Graph
Authority.

An unbound selector compiles only the template/action identity into the normal
`workflow_node_template_uuid`. At admission time the Scheduler independently
selects a compatible online/healthy/free instance for each WorkflowNodeJob.
Reusing the same Python selector does not create a hidden Task-level lease or
affinity and must not serialize otherwise independent Nodes.

A fixed selector must name a registered instance of the annotated template.
Copy its explicit constraint to every represented Node at
`WorkflowNode.meta_data.unilab.executor_binding` using
`{"mode": "fixed", "device_id": "<id>"}`; do not add a non-Backend top-level
WorkflowNode field. This reserved constraint is graph-semantic, is included in
the Task snapshot, and advances Workflow revision when changed. A busy fixed
instance waits rather than silently falling back to another instance. Parallel
Nodes pinned to one instance are serialized by the Scheduler's instance lock,
not by a hidden Python control dependency.

For either mode, record the admitted concrete instance in
`WorkflowNodeJob.meta_data.unilab.executor_assignment` and the durable runtime
event. Frontend-facing delivery follows D-025: assignment, ordinary Task/Job
state, and feedback are projected through the single
`GET /api/v1/events` SSE stream and re-hydrated through REST. Do not expose a
frontend WorkflowTask WebSocket. Operator decisions remain REST writes; any
attention-demanding allocation intervention is also announced through the same
SSE stream.

### Semantic completion uses the same catalog

The annotation makes the device template directly visible to Python language
tools, but source classes alone are not the completion authority. OS generates
an ephemeral, authority/fingerprint-scoped, action-only typing projection
(`.pyi` or an equivalent Python language-server view) from the same selected
Template Catalog used by compilation:

- expose only registered `@action` methods and hide driver internals and
  `@not_action` methods;
- project action parameters, defaults, `Literal` choices, bounds, ResourceSlot
  restrictions, titles, descriptions, and docstrings through the shared D-088
  parser;
- generate one named result view from authoritative output Handle Templates,
  including D-068 implicit ResourceSlot pass-through, so `result.output` and
  downstream variable types complete statically;
- offer fixed `device_id` completion filtered to registered instances of the
  annotated template; and
- regenerate/invalidate the projection when the selected catalog fingerprint
  changes, without committing it as Workflow source or business state.

The frontend Python view must use Monaco or equivalent Python language
intelligence supplied from OS; a plain text area or a frontend-only action-name
completion list is insufficient. The same projection must be consumable by
local IDEs, CLI/MCP callers, and coding agents. Static language diagnostics are
advisory; Compile Preview remains the fail-closed semantic authority and must
reject catalog, Handle, template, instance, UUID-anchor, and graph errors even
when the editor type checker reports no error.

Normalized source retains D-051/D-054 one-result-object and named-attribute
rules, keyword-only action/subworkflow arguments, immediate UUID anchors,
source-only parallel structure, and deterministic imports/names. It carries no
Workflow revision, source hash, Candidate hash, or catalog fingerprint.

## D-093: WorkflowTask Authority is also its Material Authority

For Workflow execution, do not expose an independently selectable Material
Authority. The authority that receives
`POST /api/v1/workflow-tasks` is the Task Authority and is also the sole
Material Authority for that Task. It resolves the referenced Workflow, creates
the immutable WorkflowTask snapshot, and resolves every external ResourceSlot
Material UUID from its own authoritative material state:

- a request sent to OS uses only OS-local Workflow and Material state;
- a request sent to Backend uses only Backend Workflow and Material state; and
- the Workflow, WorkflowTask, and all Task-input Material references therefore
  belong to one selected authority for that execution.

Do not add `material_authority`, a remote base URL, a cloud/local selector, or
an authority capability flag to the WorkflowTask request. A UUID that exists
only in another authority—or happens to identify a different record there—is
not a valid local Material. Task creation must not proxy, fall back to, or
perform a remote lookup. Cross-authority execution requires an explicit
synchronization/import step before Task creation; after that step the receiving
authority validates its own local record.

This execution rule does not alter D-031/D-032 Authoring Compilation. OS may
compile a Candidate Workflow for a Backend Graph Authority against a
synchronized Backend template catalog. Once a persisted Workflow is executed,
however, the authority receiving its WorkflowTask creation request owns the
entire Task operation, including Material lookup.

## D-094: OS persists Material truth and treats ResourceTreeSet as execution projection

The OS-local Material Authority is one durable Material module backed by
SQLite. Deepen the reviewed Inventory transaction engine into that module
rather than creating a Material table in Workflow storage. It owns stable
Material identity, ResourceTemplate identity, deletion and availability state,
the last confirmed material tree/content, reservations, versions, audit
ledger, and outbox. WorkflowTask preflight, frontend Material reads, Scheduler
admission, and runtime ResourceSlot resolution all use this same module.

`ResourceTreeSet` remains the canonical in-memory representation used by
drivers and PLR conversion, but it is a controlled execution projection—not a
second durable Material authority. Do not implement polling, last-write-wins,
or general bidirectional synchronization between SQLite and
`ResourceTreeSet`. Synchronization occurs only at owned points:

1. OS startup loads persisted Material aggregates and builds or refreshes the
   corresponding runtime subtrees. A graph file may be an explicit one-time
   import into an empty authority; it must not overwrite persisted runtime
   state on every restart.
2. WorkflowTask input validation reads only durable Material state. It does not
   consult an incidental in-memory tree.
3. Before dispatch, the Material module locks the referenced UUIDs, rechecks
   versions/availability, performs reservation or admission changes, refreshes
   affected runtime roots from the confirmed aggregate, and resolves the PLR
   resources passed to the action.
4. A confirmed successful action serializes and validates every affected
   ResourceSlot root, commits Material/content/relation changes with optimistic
   versions and ledger/outbox in an idempotent operation keyed by
   `job_uuid + attempt`, and only then permits the Job to become `succeeded`.
   The persisted normalized result refreshes the affected runtime roots.
5. A material command received through the local Backend-shaped Interface
   commits durable state first, then refreshes the affected runtime projection.
   If projection refresh fails, durable state remains authoritative; mark the
   projection stale and rebuild it before further affected actions.

Do not keep a SQLite transaction open across a physical action. A failure
before dispatch releases the admission state without inventing a material
change. A failure after dispatch, `dispatch_unknown`, or a post-action
persistence failure must not restore an old tree and pretend physical work did
not occur. Fence or quarantine the affected Materials, persist a reconciling
state, and block downstream consumers until device query or human
reconciliation establishes and commits the observed state. A successful
physical action whose Material commit is pending is not yet a successful Job.

The previous OS material-query implementation under
`Uni-Lab-Core/Uni-Lab-OS` is a source for ResourceTreeSet serialization,
PLR round-trip tests, rendering projection, and internal-node filtering only.
Do not migrate its authority rules: runtime graph checksums, graph-derived
Material UUIDs, in-memory idempotency records, schedule-WebSocket read
round-trips, and direct frontend mutation of `ResourceTreeSet` are retired.
Frontend Material reads come directly from the local durable Material module;
normal runtime notifications follow the D-025 SSE projection.

High-frequency device telemetry and joint poses are not Material persistence
and do not participate in this synchronization. If the durable Material module
is unavailable, Material-backed Task creation and execution fail closed; the
legacy behavior of warning and executing without authoritative material
validation/reservation is not allowed for the migrated Workflow Interface.

## D-095: OS uses one Material UUID and names Backend code as barcode internally

Use one canonical Material identity throughout OS. A Material row owns `uuid`;
every relationship, Workflow reference, Inventory row, runtime lock, and local
variable that refers to it uses `material_uuid`. These are the same UUID value,
not two identities. Remove `edge_uuid`, `legacy_cloud_id`, graph-derived
Material UUIDs, and `instance_uuid` aliases from the migrated Material path.
Explicit cross-authority import preserves the source Material UUID. Existing
local data is migrated once with all dependent references rewritten; no
compatibility column or runtime adapter remains afterward.

The Backend `Material.code` business value is the laboratory barcode. OS
retains the domain name `barcode` in its Material/Inventory implementation and
projects it one-to-one as public `Material.code` at the Backend-compatible HTTP
Interface. Persist one value only; do not keep `code` and `barcode` columns that
can diverge, and do not hide a second barcode in `Material.data` or
`Material.config`. Empty barcode remains allowed. A non-empty barcode is unique
among non-deleted, operationally active Inventory-managed Materials, preserving
the reviewed local scan/admission invariant.

The durable Material module contains Backend Material identities for both
device instances and business-resource instances. Their operational state is
not unified:

- Inventory companion state—lot, warehouse/reservation/consumption,
  quarantine, barcode uniqueness, and optimistic version—applies only to
  Inventory-managed business Materials and is keyed directly by
  `material_uuid`;
- device online/health/action-lock state remains in DeviceState and Scheduler,
  also keyed to the same device Material UUID; and
- a device Material must not acquire warehouse/reserved/consumed lifecycle
  states merely because both kinds share the `material` identity table.

Frontend Material DTOs continue to use Backend field spelling, including
`uuid`, `code`, and `resource_template_uuid`. Private SQLite layout and Python
domain naming may use `barcode` because D-058 requires frontend Interface
parity, not identical ORM implementation. ResourceSlot references business
Material UUIDs; `WorkflowNode.material_uuid`/executor assignment references a
device Material UUID. The context determines the role without changing the
identity type.

## D-096: every implementation round has an independent branch, test authors, and reviewers

One independently mergeable plan slice is one implementation round. Before
production work starts, create a fresh `migration/<round>-<topic>` branch from
the latest commit already merged into `integration/workflow-task-runtime`.
Never stack a later round on an unmerged round branch. The existing
`migration/01-backend-contract` branch is treated as one legacy-named round and
must pass this decision before it may merge.

Before implementation, assign at least two independent test-author subagents in
separate Git worktrees and `test/<round>-*` branches:

1. one writes frozen-Backend and accepted-decision contract tests; and
2. one writes adversarial, regression, restart, concurrency, and invalid-input
   tests appropriate to the round.

Each author commits tests that first fail for the intended missing behavior.
Merge those commits into the implementation branch without squashing their
provenance. Implementation must make both suites pass; it may not weaken,
delete, skip, or xfail an independently authored test merely to obtain a green
gate.

On one pinned candidate commit, run the round-target tests, cumulative phase
tests, the complete repository suite, configured lint/static checks, and
`git diff --check`. After all pass, assign at least three independent review
subagents who did not author the implementation:

1. decision and frozen-Interface compliance;
2. repository standards and module design; and
3. regression, transaction, recovery, concurrency, and security risk.

Every reviewer inspects production code and tests at the exact tested SHA.
Every blocking finding is fixed, the affected and complete gates are rerun, and
the relevant reviewer confirms the fix. Any production-code change, relevant
test change, rebase, or other SHA change invalidates the affected review.

The migration ledger records the round branch and base, test-author identities
and commits, red evidence, exact tested SHA, commands and results, reviewer
identities, findings and disposition, and final merge commit. Only after that
record is complete may the round merge locally into
`integration/workflow-task-runtime`. Preserve reviewable commits, do not
squash migration provenance, and do not push without explicit authorization.

## D-097: OS persists Backend-shaped Sites separately from Material composition

The OS durable Material module owns a `Site` model that mirrors the frozen
Backend frontend contract. A Site has one stable `uuid`, owning
`material_uuid`, `name`, `sort_order`,
`allowed_resource_template_uuids`, optional `occupied_material_uuid`, and the
Backend geometry fields. A locally created Site receives UUIDv4 once; an
explicit cross-authority import preserves the source Site UUID. Template
materialization creates the instance Sites deterministically from the selected
ResourceTemplate definition while allocating and then preserving their
instance Site UUIDs.

`Material.parent_uuid` and Site occupancy are deliberately independent:

- `Material.parent_uuid` expresses composition/ownership in the Material tree;
- `Site.material_uuid` identifies the Material that owns the position; and
- `Site.occupied_material_uuid` identifies the optional Material currently
  placed at that position.

Do not infer either relationship from the other. Moving an occupant between
Sites changes occupancy, not composition. Changing a Material's composition
parent does not silently place it into a Site.

Each Site holds at most one occupant and an occupied Material may occupy at
most one Site. Site writes reject self-occupancy, missing or deleted owners and
occupants, disallowed occupant templates, duplicate owner/name combinations,
and occupancy conflicts. They use stable Site/Material UUIDs and optimistic
versions inside the Material module's transaction and audit/outbox rules.

Retire `resource_relation(parent_uuid, slot_id, child_uuid)` as persistent
truth after a one-time migration. A legacy relation with a non-empty `slot_id`
is imported by resolving or creating the owner's named Site and assigning its
`occupied_material_uuid`; it must not remain a second writable placement
model. `Material.parent_uuid` is migrated independently as composition.

`ResourceTreeSet`/PLR remains the runtime projection. The projection maps
persisted `Site.name`/`sort_order` to PLR site lookup and ordering, and maps
occupancy by Material UUID rather than treating a PLR resource name as
identity. A confirmed post-action projection change is committed back to
`Site.occupied_material_uuid` through the Material module before Job success,
under D-094's failure and reconciliation rules. Internal driver helpers may
resolve a human-readable Site name within a known owner, but the shared
frontend Interface and persistent relationships use Site UUIDs.

The existing `lab_zone`/`lab_placement` tables remain a separate 2D laboratory
layout model. They do not become Material Sites and do not own material
occupancy. No Site scheduler lock or lease is implied by this persistence
decision; concurrent admission and lock lifetime are decided with Material
availability semantics.

## D-098: Task Material Reservations and Job Execution Claims are separate

Use two explicit, durable concurrency mechanisms rather than one overloaded
resource lock.

A Task Material Reservation is the Task-lifetime claim established atomically
with WorkflowTask creation after input validation. It reserves the business
Materials and inventory quantities required by the accepted Task input and
known execution plan, prevents a second Task from being accepted against the
same unavailable identity or quantity, and is owned by
`workflow_task_uuid`. It is not a device lock and does not lock a Site merely
because a Material currently occupies it.

A Job Execution Claim is the short-lived physical-execution right acquired
after a Node is ready and a concrete executor device has been selected, but
before runtime projection refresh or dispatch. Version 1 acquires one atomic
claim set containing:

- the selected executor device's Material UUID;
- every business Material UUID that the action may mutate; and
- every source or destination Site UUID whose occupancy the action may change.

The claim owner is `job_uuid + attempt`. Acquire the complete set in
deterministic UUID order or acquire none; a conflict keeps the Job pending for
later admission rather than partially dispatching it. Use stable UUIDs only.
Do not key the final mechanism by `device_id + action_name`, resource name,
barcode, parameter name, or PLR object identity. Version 1 treats one selected
device instance as exclusive while its Job claim is held. The exact
action-contract syntax that declares mutable ResourceSlots and occupancy Sites
is deferred to the action-side ResourceSlot decision; runtime value-shape
heuristics are forbidden.

Release a Job Execution Claim after a pre-dispatch failure, or only after a
dispatched action's affected Material/Site state has committed and the Job can
enter its final state. Cancellation does not release a claim while the
physical action may still be running. `dispatch_unknown`, post-action
persistence failure, or an unresolved physical result keeps the affected claim
fenced across process restart until device or human reconciliation determines
and commits reality. Task termination releases its Material Reservation only
after all dispatched/unknown Jobs and their fences are settled.

Site occupancy and a Site Execution Claim are different facts: occupancy says
what is placed there; the claim says which Job may currently change that
placement. SQLite transaction serialization, process mutexes, and ROS
resource-tree synchronization locks may remain implementation guards, but none
of them substitutes for these durable domain claims. Retire the current
fail-open `@action(lock_resource)` value guessing and volatile
`_job_resource_locks` authority when this mechanism is migrated.

## D-099: Material disposition is separate from claims and transient contention waits

> **Status: REFINES D-050, D-063, D-083, D-084, D-086, D-095, and D-098.**
> Backend-shaped WorkflowTask and Material requests use the latest committed
> Backend `400/404/409` error boundary rather than the earlier runtime-input
> `422` choice. D-099 does not change explicitly OS-only Authoring
> Compile/Apply diagnostics.

Persist one business-Material disposition with the closed values `active`,
`consumed`, `discarded`, `quarantined`, and `reconciling`:

- `active` is the only ordinarily schedulable disposition, subject to current
  quantity, placement, reservation, claim, and executor checks;
- `consumed` and `discarded` are terminal, non-runnable business states;
- `quarantined` is deliberately unavailable until an explicit business or
  human release; and
- `reconciling` is a durable fence while physical truth is unresolved.

Do not persist `reserved` or `in_use` as Material dispositions. Derive them
from the durable Task Material Reservation and Job Execution Claim records
defined by D-098. Device Materials continue to use DeviceState and execution
claims rather than acquiring business Inventory dispositions.

Backend-shaped HTTP requests use these boundaries:

- malformed JSON, unknown or wrongly typed WorkflowTask input, missing
  required values, invalid debug scope, invalid graph/parameter structure, or
  a ResourceSlot template mismatch returns `400 invalid_input`;
- a referenced Material UUID that does not exist or is soft-deleted returns
  `404 not_found`;
- a Material that is consumed, discarded, quarantined, or otherwise in a
  stable non-runnable state returns `409 conflict`; and
- deleting a Material protected by an active Reservation, Claim, uncertainty
  fence, or live Site/Material relationship returns
  `409 material_in_use`.

Normal reads and lists exclude soft-deleted Materials. Deletion is a soft
delete and must never silently clear a live execution reference or pretend an
unresolved physical action has settled. The current Backend repository's
recursive deletion and WorkflowNode/Site unlinking behavior is therefore not a
runtime-safety authority for OS.

Transient contention is admission state, not request invalidity and not Job
failure. After deterministic identity/schema/disposition validation succeeds,
`POST /api/v1/workflow-tasks` may acquire the complete Task Material
Reservation in its creation transaction. If the complete reservation cannot
be acquired because another Task holds it, a required quantity or Site is
temporarily unavailable, or the Material is reconciling, persist the Task as
`pending` without a partial reservation and let the sole coordinator retry the
atomic reservation. No Job may dispatch before the complete Task reservation
exists. Likewise, a Job Execution Claim conflict, executor lock conflict, or
temporarily offline executor keeps the Job `pending`; it does not return a
late HTTP error or transition the Job to failed.

This paragraph supersedes D-098 only where D-098 required every Task
Reservation to be established atomically with Task creation and therefore
rejected a second Task. The reservation itself remains all-or-none,
Task-lifetime, durable, and owned by `workflow_task_uuid`; only its acquisition
may be retried while the already-created Task remains pending.

For the shared Material DTO, Backend `code` remains the one laboratory barcode
value from D-095, but create and full update now require it to be non-blank and
case-insensitively unique among non-deleted Materials. This supersedes only
D-095's sentence allowing an empty barcode; it does not introduce a second
`code`/`barcode` value.

## D-100: Action signatures and named result records own the typed contract

> **Status: CLOSES THE FIRST P0-4 SUBDECISION.**

For a Workflow-capable Action, derive the input contract from its Python
function parameters, excluding framework-owned parameters such as `self`.
Parameter types, defaults, nullability, finite constraints, enum values,
ResourceTemplate restrictions, titles, and descriptions use the shared
D-088 through D-091 annotation parser. Parameter names are the stable input
binding names; do not infer a different business name from examples or runtime
values.

Support two first-class, statically typed forms for explicit named Action
outputs:

1. a standard `TypedDict` return annotation, preserving the Action's existing
   plain-dict runtime shape; and
2. a standard-library frozen dataclass return annotation, allowing constructor
   checking and attribute completion inside device code.

Both forms declare one output field per named result. Their field annotations
use the same finite type and `Annotated` metadata vocabulary as Action inputs.
Every declared field is present after successful execution. Express a
present-but-empty value with `T | None` and explicit `None`; do not use an
optional/missing result key to change the output shape. `-> None` declares no
explicit output.

Also accept this non-recommended, Uni-Lab-specific compatibility form:

```python
def transfer(...) -> {
    "sample": Annotated[
        ResourceSlot,
        AllowedResourceTemplates(corning_96_well_plate),
        Field(title="转移后样品"),
    ],
    "transferred_volume": Annotated[
        float,
        Field(title="转移体积", ge=0),
    ],
}:
    ...
```

The AST parser accepts only unique non-empty literal string keys and supported
annotation expressions. It rejects duplicate or computed keys, mapping
unpacking, and unsupported values. This form is compatibility syntax because
standard Python language servers do not treat a dict expression as a return
type. A bare `dict`/opaque-object return annotation does not acquire guessed
Workflow Handle fields.

Normalize all three accepted source forms into one ordered Action input/output
contract before template-catalog projection. They produce the same
WorkflowHandleTemplate business identities and catalog fingerprint, so
changing only the declaration form does not churn Handle UUIDs. The generated
authority-scoped typing projection still exposes one named result view for
Workflow authoring, independently of whether the device implementation returns
a TypedDict, frozen dataclass, or compatibility dict.

Keep D-068's implicit same-name ResourceSlot outputs outside the explicit
result declaration: Registry projection synthesizes them after parsing the
explicit contract. The remaining P0-4 decisions still own legacy
`@action(handles=...)` precedence/retirement, exact catalog field projection,
Action default/null behavior, mutation/Site declarations, and runtime result
normalization.

## D-101: bound untrusted Workflow JSON without narrowing canonical integers

> **Status: REFINES D-043, D-083, D-086, and D-096.**

The OS public Workflow HTTP adapter applies one explicit resource budget before
business validation. The body byte budget applies to every Workflow route that
declares a request body, regardless of the caller-supplied `Content-Type`;
JSON-specific integer and depth budgets apply when decoding JSON:

- a JSON request body is at most 8 MiB (`8 * 1024 * 1024` bytes);
- one external JSON integer token contains at most 4096 decimal digits,
  excluding its optional minus sign; and
- the existing maximum nesting depth of 10000 counts the complete JSON
  document, including every object and array wrapper.

Reject a declared oversized body before reading it. For chunked or missing
`Content-Length`, read the request stream incrementally and stop as soon as the
byte budget is exceeded. Check an integer token's digit count after lexical
recognition but before constructing a Python bigint. All three failures use the
frozen `400 invalid_input` envelope and create no Workflow, Candidate, Task, or
other side effect.

The 4096-digit boundary is an untrusted transport budget, not a new Workflow
value type or persistent numeric constraint. Trusted internal canonical JSON
continues to encode and decode arbitrary finite Python integers through the
chunked codec, without changing `sys.set_int_max_str_digits`. The public
adapter passes the external digit budget into that same decoder; do not fork a
second JSON parser.

Apply the complete-value depth rule to every Schema operation. A standalone
normalized value counts its own list/object root. A Contract canonical payload
counts its envelope, descriptor collections, descriptors, and embedded
defaults. Therefore a `list[object]` item or opaque Input default may have less
remaining subtree depth than the same object used as a standalone value. Reject
the complete value before returning it, using `invalid_value` for
`normalize_value` and `invalid_contract` for a Contract, with the full JSON
Pointer of the first over-budget container. A parser-created canonical value
must always be consumable by `to_dict()` under the same depth limit.

These limits protect the single OS event loop from adversarial conversion work
while retaining D-083's internal mathematical-integer semantics. They do not
modify Backend code or claim a new shared Backend product field.
