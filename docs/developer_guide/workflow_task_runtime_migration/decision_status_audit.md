# Workflow migration decision status audit

## Scope and evidence rule

This is a status audit of D-001 through D-102. It does not introduce
another product design. Its purpose is to distinguish historical decisions,
closed decisions, remaining contract decisions, and ordinary implementation
work before phase 01 starts.

The evidence order is:

1. the frozen Backend frontend contract at
   `uni-lab-backend@09609a27e652c9e56ede636a2883a4fd241e4400`;
2. later product decisions in `decisions.md`, especially D-058 through D-102,
   plus the D-096 implementation-round gate;
3. the current Gaojing OS implementation, which is implementation evidence but
   is not allowed to redefine the target contract.

This follows D-058's source and scope rule
(`decisions.md:1107-1140`). The Backend checkout has since moved beyond the
frozen commit and contains unrelated working-tree changes, so every Backend
source citation below means `git show 09609a2:<path>` rather than the checkout's
current file contents.

Status terms used here:

- **active**: still normative as written;
- **active remainder**: only the stated remainder survives a later decision;
- **superseded**: must not be implemented;
- **closed deferral**: a question left open by this decision was answered later;
- **route-level exception**: a later decision intentionally differs from a
  generally shared Backend route;
- **open blocker**: a business or wire contract is still insufficiently
  specified to implement and test the complete migration;
- **implementation work**: the contract is already knowable and no more grill is
  needed before coding.

## Executive result

There are three different populations in the ledger:

1. D-010 through D-057 contain a substantial historical tail. The final rules
   are already recoverable, but implementations must not treat all paragraphs as
   simultaneously active.
2. D-058 removes Backend-to-Edge transport, process topology, locking and replay
   from the parity gate. Several old comparison/manifests still describe those
   internals as if they were frontend migration decisions.
3. D-059 through D-095 close Workflow input/ResourceSlot schema, normalized
   Python questions, add one intentional shared-route semantic exception, and
   expose a missing atomic Apply seam.
4. D-096 refines D-006: every independently mergeable implementation round now
   has a fresh branch, independent test authors, the complete test gate, and
   multiple independent reviewers before merge.
5. D-097 closes Material Site identity, composition/occupancy separation, and
   the durable-to-runtime Site projection.
6. D-098 separates durable Task Material Reservations from atomic,
   restart-safe Job Execution Claims over devices, mutable Materials, and
   occupancy-changing Sites.
7. D-099 closes Material disposition, soft-delete, barcode uniqueness,
   Task/Job contention handling, and the frontend-visible 400/404/409 boundary.
8. D-100 closes the first P0-4 subdecision: Action input annotations and named
   result records are the typed contract source.
9. D-101 freezes the OS public Workflow JSON resource budget while preserving
   arbitrary trusted canonical integers and makes nesting depth a
   complete-document/value limit.
10. D-102 freezes all OS Authoring source coordinates as one-based,
    end-exclusive UTF-16 code-unit ranges and closes the compiler/Monaco
    interoperability ambiguity.

The complete Workflow migration still has five decision groups that need a
contract-level answer:

1. the action-side ResourceSlot declaration and catalog-projection contract;
2. external `WorkflowTask.output` representation;
3. the OS-only debugger launch/hold command contract;
4. explicit Conditional Join source syntax and finite Handle arity;
5. the OS `tool_call` execution/isolation boundary.

Node/Task/Job status enums, Job precreation, Node-only runtime state, common
Task commands, SSE transport, and ResourceTemplate deletion behavior are not
unanswered design questions under D-058. They are direct frozen-source
implementation work, unless D-058 itself is deliberately reopened.

## 1. Supersession ledger

The following table is the effective replacement graph. “Active remainder”
means later implementation must read the replacement and must not copy the old
paragraph wholesale.

| Earlier decision | Effective status | Replaced by | Evidence |
|---|---|---|---|
| D-006 | active remainder: completed work accumulates on the integration branch, provenance is preserved, and pushing still requires authorization | D-096 replaces phase-sized implementation branches with one fresh branch and independent test/review gate per mergeable round | `decisions.md` D-006 and D-096 |
| D-010 | active remainder: atomic full-graph PUT only | D-013 removed “only save”; D-034 fixed the actual CRUD/PUT routes | `decisions.md:100-109`, `:133-152`, `:464-491`; Backend routes are `09609a2:internal/http/handler/workflow.go:22-49` |
| D-013 | active remainder: save and Task creation are separate; graph PUT remains | D-034 retires graph PATCH | `decisions.md:133-152`, `:464-491`; frozen Backend has graph PUT but no graph PATCH at `09609a2:internal/http/handler/workflow.go:22-49` |
| D-014, D-015, D-016 | superseded; do not implement timestamp concurrency variants | D-033 | `decisions.md:154-182`, `:445-462`; frozen model has `Workflow.revision` at `09609a2:internal/model/workflow.go:47-55` |
| D-017 | superseded; do not implement graph PATCH changesets | D-034 | `decisions.md:184-192`, `:464-487` |
| D-018 | active remainder: graph reads expose templates and graph writes never own them | D-034 removes the embedded Workflow write model; PUT is revision/nodes/edges only | `decisions.md:194-209`, `:464-491`; frozen graph DTOs are at `09609a2:internal/http/handler/workflow.go:83-101`, `:189-203` |
| D-019 | active remainder: graph PUT entities use real stable UUIDs | D-034 retires graph PATCH references | `decisions.md:211-224`, `:464-491` |
| D-020 | active remainder: real entity UUIDs and response-only timestamps | D-034 fixes graph PUT body and D-058 freezes the Handler DTO | `decisions.md:211-223`, `:464-487`, `:1107-1125`; request body is exactly revision/nodes/edges at `09609a2:internal/http/handler/workflow.go:189-203` |
| D-021 | superseded as a contract baseline | D-044, then D-058 | `decisions.md:225-237`, `:733-753`, `:1107-1140` |
| D-022 | superseded envelope | D-043 | `decisions.md:239-249`, `:701-731`; generic frozen envelope is `09609a2:internal/http/handler/response.go:17-30` |
| D-023 | superseded; no frontend WorkflowTask WebSocket | D-025 | `decisions.md:251-261`, `:276-289` |
| D-024 | active remainder: interventions are durable REST writes | D-025 retires its WebSocket reference | `decisions.md:263-274`, `:276-289` |
| D-026 | superseded; no recursive Schema-default materialization | D-045 | `decisions.md:291-322`, `:755-774` |
| D-027 | active remainder: one selected authority and parity for genuinely shared frontend capabilities | D-031, D-046 and D-058 retire compile/debug/Edge clauses; D-059 adds an input-semantics exception | `decisions.md:324-346`, `:413-424`, `:776-796`, `:1107-1140`, `:1158-1180` |
| D-028 | active remainder: no shared capability endpoint | D-031 and D-046 retire the claim that OS-only compile/debug extensions must exist on Backend | `decisions.md:348-367`, `:413-424`, `:776-796` |
| D-011, D-029, D-051 | active remainder: persisted action/control Nodes retain UUID anchors | D-054 removes UUID-bearing implicit Fork/Join nodes | `decisions.md:111-123`, `:369-390`, `:906-940`, `:942-977` |
| D-033 | active remainder: revision CAS and ordinary metadata PUT behavior | D-060/D-066 make reserved OS contract/binding metadata graph-semantic and revision-advancing through Apply | `decisions.md:454-471`, `:1207-1261`, `:1435-1490` |
| D-035 | active remainder: Backend has no Workflow-level Python source field | D-041 makes SQLite authoritative for an OS-local applied graph/source record and leaves `.py` as Draft | `decisions.md:493-518`, `:634-670` |
| D-036 | active remainder: Draft compile/Preview never auto-applies; Apply remains explicit | D-041 makes OS-local Apply one SQLite transaction rather than a separate graph PUT request | `decisions.md:529-554`, `:644-680` |
| D-039 | active remainder: stale detection and explicit graph-wins/code-wins reconciliation | D-041 makes OS-local code-wins use the atomic SQLite Apply transaction | `decisions.md:592-614`, `:644-680` |
| D-044 | active remainder: Backend is read-only | D-058 replaces `f352f54` with `09609a2` and limits parity to the frontend Interface | `decisions.md:733-753`, `:1107-1140` |
| D-047, D-048, D-049 | active remainder: Task-scoped debug config, no Task skip, directed reachability | D-052 replaces singular `start_node_uuid` with `start_node_uuids` | `decisions.md:798-879`, `:979-1007` |
| D-050 | active remainder: validate required inputs after scope pruning | D-059/D-060 add an explicit Workflow input binding as a third legal provider | `decisions.md:881-904`, `:1158-1238` |
| D-052 | active remainder: multi-node start frontier | D-059/D-060 refine the cut-input provider rule | `decisions.md:979-1007`, `:1158-1238` |
| D-054 | active remainder: ordinary convergence creates no Join | D-057 permits one explicit Conditional Join represented by a published compute template | `decisions.md:942-977`, `:1073-1105` |
| D-055 | active remainder: one first-match condition Node | D-057 permits an explicit control-only Conditional Join after the condition | `decisions.md:1021-1046`, `:1073-1105` |

No other D-001 through D-058 decision is superseded merely because the current
Gaojing code has not implemented it. In particular, D-001 through D-009 remain
migration-process constraints, while D-012, D-030 through D-032, D-034,
D-037 through D-038, D-040 through D-042, D-045 through D-046, D-053,
D-056 through D-058 retain
their effective rules subject to the replacements above.

## 2. D-059 through D-102 deferral closure

Later ResourceSlot decisions are cumulative. A phrase such as “decided
separately” in an earlier item is not automatically an open blocker.

| Deferral origin | Later closure | Remaining open part | Evidence |
|---|---|---|---|
| D-059: persistence location, type surface, binding representation | D-060 closes persistence/binding; D-061 through D-070 close ResourceSlot representations; D-082 through D-091 close the finite type set, coercion, Input/Output null/default behavior, finite constraints, closed-object rules, annotation/docstring ownership, nullable spelling, Field bounds, symbolic template restrictions, and enum syntax; D-092 closes normalized Python, typed device selectors, and semantic completion; D-093 closes Task/Material Authority selection and cross-authority fallback; D-094 closes OS-local persistence and runtime-projection ownership; D-095 closes canonical UUID and barcode/code naming; D-097 closes Site identity, composition/occupancy separation, and runtime projection; D-098 closes Task/Job concurrency ownership and lifetime; D-099 closes disposition, deletion, contention, and HTTP errors | none | `decisions.md` D-059～D-070, D-082～D-095, and D-097～D-099 |
| D-061: external reference, canonical form, materialization time | D-062 and D-065 | none for input ResourceSlot | `decisions.md:1271-1275`, `:1277-1308`, `:1384-1422` |
| D-062: template constraints and snapshot | D-063 and D-065 | none for input ResourceSlot | `decisions.md:1305-1308`, `:1310-1351`, `:1384-1422` |
| D-063: composition and snapshot | D-064 and D-065 | none | `decisions.md:1349-1351`, `:1353-1382`, `:1384-1422` |
| D-064/D-065: ResourceSlot output constraints | D-067 | external output serialization remains open | `decisions.md:1380-1382`, `:1424-1425`, `:1481-1523` |
| D-066: output inference/compatibility/serialization | D-067 closes inference and compatibility | external output serialization | `decisions.md:1477-1479`, `:1481-1523` |
| D-067: same-name pass-through/serialization | D-068 closes pass-through | external output serialization | `decisions.md:1522-1523`, `:1525-1581` |
| D-068: Handle identity, collections, serialization | D-069 closes Handle identity; D-070 closes collections | external output serialization | `decisions.md:1579-1581`, `:1583-1632`, `:1634-1690` |
| D-069: collections/serialization | D-070 closes collections | external output serialization | `decisions.md:1631-1632`, `:1634-1690` |
| D-070 | no later closure | external `WorkflowTask.output` representation | `decisions.md:1690` |
| D-066: persisted Output Binding location and variants | D-071 fixes `Workflow.meta_data.unilab.output_bindings` and snapshot inclusion; D-072 fixes `workflow_input` and `node_output` | none for persisted Binding representation | `decisions.md` D-066, D-071, and D-072 |

## 3. Clauses made inapplicable by D-058

These clauses must not become phase gates or public OS routes.

| Retired or narrowed clause | Effective interpretation | Evidence |
|---|---|---|
| D-021's old execution authority, including Backend local/Edge implementation details | only frozen frontend Handler/Service/Model behavior matters | `decisions.md:225-237`, `:1107-1140` |
| D-027's statement that Backend-to-OS synchronization and execution use `/edge/*` | retired for this migration; OS-local Scheduler-to-driver transport is private | `decisions.md:339-346`, `:1127-1140` |
| D-025's mention of `/api/v1/edge/ws` | a boundary warning only; it creates no OS migration task | `decisions.md:285-289`, `:1127-1140` |
| D-028's `capability_revision` discussion | informational only; it is neither frontend discovery nor a parity requirement | `decisions.md:355-367`, `:1127-1140` |
| D-041's difference between Backend PostgreSQL and OS SQLite | private implementation difference, not a parity blocker; local durability is still required by OS decisions | `decisions.md:634-670`, `:1127-1140` |
| Backend Edge Command/Inbox, Job token, ACK/replay, session reconciliation, execution/advisory locks, and process split | explicitly out of scope | `decisions.md:1127-1140` |

This audit also corrected the affected planning text:

- `migration-manifest.md` now names frozen `09609a2` as the Backend frontend
  Interface source and labels documents 10-12 as historical
  (`decisions.md:1107-1115`).
- Its former open-ended “Cloud Adapter” row is now limited to switching the
  frontend's selected authority and explicitly excludes `/api/v1/edge/*`
  (`decisions.md:1117-1140`).
- `migration-manifest.md:16-17` remains valid as OS-private reliability work,
  but feedback/cancel/reconciliation mechanisms are not Backend Edge parity
  gates (`decisions.md:1127-1140`).
- `backend_design_comparison.md` now moves Job write ownership, Job
  precreation, Node-only edge resolution, status/command/SSE behavior and
  ResourceTemplate deletion into the direct-implementation table rather than
  the grill backlog. Frozen Backend precreates planned pending Jobs
  (`09609a2:internal/service/workflow/execution.go:53-83`), and Edge activation
  remains derived (`decisions.md:789-792`, `:1005-1019`).
- The comparison now consistently calls `09609a2` the frozen reviewed
  authority rather than the Backend worktree's current HEAD.

D-002 still protects the reviewed target tests. Excluding Backend's Edge
protocol from parity does not authorize deleting Gaojing's Scheduler, HostLink,
Inventory, or action-policy behavior (`AGENTS.md:660-663`).

## 4. Early decisions that require D-059 through D-101 refinement

### 4.1 D-027/D-028 parity has a deliberate D-059 route-level exception

D-027 says shared capabilities preserve path, DTO and business meaning
(`decisions.md:332-337`), while D-059 says the same
`POST /api/v1/workflow-tasks` input field has effective OS-only behavior
(`decisions.md:1173-1180`). Frozen Backend indeed accepts and forwards `input`
in the Handler (`09609a2:internal/http/handler/workflow.go:350-357`,
`:713-723`) but writes `{}` in the Service
(`09609a2:internal/service/workflow/execution.go:53-64`).

Recommended status: keep D-027 for genuinely shared semantics, but treat D-059
as the explicit route-level exception. Do not describe effective Workflow input
as Backend parity, and fail Backend authority selection before Task creation
when the Workflow depends on it. This is already a decided exception, not a new
blocker.

`AGENTS.md:416-420` is too broad when it says both authorities must implement
the agreed “authoring, execution, event, and debugging Interface.” Read it
together with its compile exception at `AGENTS.md:421-425`, debug exception at
`AGENTS.md:156-165`, and input exception at `AGENTS.md:287-297`; otherwise it
can incorrectly reopen D-031, D-046, and D-059.

### 4.2 D-050's two-provider wording is narrowed by D-060

D-050 originally allows only persisted non-null `param` or an in-scope Edge
(`decisions.md:888-900`). D-060 defines mutually exclusive static-param,
incoming-Edge, and Workflow-input-binding providers
(`decisions.md:1224-1230`). Therefore debug-scope validation must include the
third provider after validating Task input. It must still reject arbitrary Task
keys, earlier Task/Job output, and frontend memory. D-050's status note already
records this refinement at `decisions.md:883-886`.

Frozen Backend required-input planning only knows static data and Edges
(`09609a2:internal/service/workflow/planner.go:200-225`), so the third provider
is part of the intentional OS extension rather than an observed Backend rule.

### 4.3 D-034 conflicts with D-060/D-066 unless an OS-only persistent Authoring seam is added

D-034 freezes shared graph PUT to exactly `revision`, `nodes`, and `edges`
(`decisions.md:480-487`). Frozen Backend confirms that DTO
(`09609a2:internal/http/handler/workflow.go:189-203`) and exposes Workflow
metadata as a separate Workflow PUT (`09609a2:internal/http/handler/workflow.go:22-32`,
`:66-80`).

D-060 and D-066 then make these reserved values graph-semantic:

- `Workflow.meta_data.unilab.input_contract`;
- `WorkflowNode.meta_data.unilab.input_bindings`;
- `Workflow.meta_data.unilab.output_contract`.

D-071 fixes final Workflow Output Bindings at
`Workflow.meta_data.unilab.output_bindings` and requires them to enter the
immutable snapshot. A root WorkflowTask therefore never reads or recompiles the
current Python Draft. D-072 fixes the two discriminated v1 variants.

They must advance `Workflow.revision` and Apply atomically with graph, source,
and source map (`decisions.md:1232-1238`, `:1471-1475`). D-041 establishes that
SQLite can provide the local transaction (`decisions.md:649-655`), but no
public request currently carries this complete Apply intent.

This is an open blocker. The OS-only seam must also own Draft state, hash/CAS
and source-only Apply. It must be resolved without adding reserved fields to
the shared Backend graph PUT, because doing so would violate D-034/D-058.

### 4.4 D-043 route exceptions are resolved and documented

D-043 now explicitly records two frozen route exceptions:

- invalid `/events` `Last-Event-ID` returns a naked error object;
- ResourceTemplate DELETE returns HTTP 200 and an empty data object.

See `decisions.md:701-708`,
`09609a2:internal/http/handler/event.go:28-34`,
`09609a2:internal/http/handler/template.go:107-117`, and the public expectation
at `09609a2:internal/http/router_test.go:137-141`.

`AGENTS.md` now states the generic envelope and explicitly records both frozen
route exceptions. Under D-058, the route exceptions win; neither is an open
product decision.

The same rule applies to ResourceTemplate deletion business behavior. Frozen
repository code soft-deletes related Material and Workflow graph data
(`09609a2:internal/repository/template_graph.go:181-228`). D-058 says to mirror
that frontend-observable behavior, and the comparison now records it as direct
implementation work rather than a safety-policy choice. It is not an
unanswered parity rule unless the team deliberately reopens D-058.

### 4.5 D-057 lacks a finite source/Handle contract

D-057 requires one distinct optional target Handle for every alternative
branch (`decisions.md:1081-1087`). Frozen Backend rejects two Edges into one
target Node/Handle pair
(`09609a2:internal/service/workflow/planner.go:185-195`). D-042 and D-069 also
require Handles to be published catalog identities, not compiler-created UUIDs
(`decisions.md:672-699`, `:1612-1623`).

A static compute template cannot support an arbitrary number of `if/elif/else`
alternatives unless the contract chooses a maximum arity, a family of
arity-specific templates, or another already-supported finite representation.
D-057 also does not freeze the normalized Python syntax/source mapping of the
explicit Conditional Join. This is an open blocker for compiler, catalog and
runtime tests.

The question “may Preview succeed when the selected authority lacks the
template?” is not open: D-032 and D-042 require compilation to fail with a
catalog diagnostic (`decisions.md:429-443`, `:692-699`).

### 4.6 D-069 is viable locally but not generally publishable by frozen Backend registry input

Backend `WorkflowHandleTemplate` inherits `BaseModel.meta_data`
(`09609a2:internal/model/base.go:10-17`,
`09609a2:internal/model/workflow.go:31-45`), so the persistence model can hold
D-067/D-069 output constraints. However, frozen registry action Handle input
does not carry Handle metadata or an allowlist
(`09609a2:internal/service/template/model.go:152-163`).

Therefore D-069 can be implemented by the OS-local catalog projection, but a
Backend-bound compile cannot assume that the frozen Backend registry can
publish the same implicit output contract. D-059 already allows input-dependent
workflows to be OS-only (`decisions.md:1173-1180`). The remaining requirement is
an explicit authority-support check: a selected catalog either contains the
real compatible Handle UUID/metadata or compilation fails. No Backend change or
local UUID substitution is permitted (`decisions.md:1617-1623`).

There is also no confirmed author-facing declaration contract for an OS action
to state `ResourceSlot`/`List[ResourceSlot]`, nullable/default behavior, and
input/output allowlists before catalog projection. D-067 through D-069 define
the required persisted Handle shape and implicit output behavior, but not the
Registry annotation/YAML/Python form from which it is produced. That syntax is
a real compiler/catalog decision, not something the projection layer may
guess from names or runtime examples.

## 5. True remaining blockers, ordered by dependency

### P0-1: OS-only persistent Authoring Interface — closed

**Depends on:** D-034, D-041, D-060, D-066.

Decisions D-073 through D-081 have closed the Workflow-scoped routes, Draft and
Apply CAS requests, server-owned Candidate selection, aggregate, success
responses, source-only Apply result, error codes, conflict order, dirty-editor
conflict flow, durable SSE invalidation protocol, package-source identity,
workspace/runtime-data separation, path containment, missing-file behavior, and
startup/writeback recovery lifecycle. P0-1 has no remaining contract question.

Do not alter the shared graph PUT body. Evidence is the conflict in section
4.3: `decisions.md:480-487`, `:649-655`, `:1232-1238`, `:1471-1475`, and
`09609a2:internal/http/handler/workflow.go:189-203`.

### P0-2: version-1 Workflow schema and normalized Python — closed

**Depends on:** none.

**Blocks:** compile diagnostics, default filling, strict Task preflight,
provider validation, snapshot serialization, output validation, CLI/MCP
atomicity, and E2E fixtures.

D-082 through D-091 close the finite
scalar/object/list/ResourceSlot type set, strict coercion behavior, top-level
Task input null/default behavior, validation keywords, and unknown-key
behavior, output required/default/explicit-null behavior, and parameter
type/default/presentation metadata ownership, nullable spelling, Field boundary
syntax, symbolic ResourceTemplate restrictions, and enum syntax. D-092 closes
the deterministic normalized Workflow form, template-annotated dynamic/fixed
device selectors, AST/Catalog validation, Action-result typing projection, and
frontend/IDE semantic completion while reusing that one vocabulary for D-060
input and D-066 output contracts. D-101 adds only an OS transport resource
budget and complete-value depth accounting; trusted canonical integer semantics
remain unchanged. P0-2 has no remaining contract question.

### P0-3: freeze Material Authority identity and lookup ownership

**Depends on:** P0-2 for schema validation shape.

**Blocks:** ResourceSlot Task preflight, canonical Task input, runtime
resolution, constrained output validation, and material-backed E2E.

D-062/D-063 repeatedly require a “selected Material Authority”
(`decisions.md:1277-1308`, `:1342-1347`) but do not say:

1. ~~whether it must be the same selected authority as Graph/Execution
   Authority, and how an OS-local request selects or derives it~~ — D-093
   resolves this as the authority receiving `POST /workflow-tasks`;
2. ~~which local store/service owns UUID lookup and deleted/unavailable
   state~~ — D-094 assigns durable truth to the OS Material module based on the
   reviewed Inventory transaction engine and makes `ResourceTreeSet` an
   execution projection;
3. ~~whether any cross-authority Material lookup is forbidden or explicitly
   configured~~ — D-093 forbids request-time lookup/fallback and requires
   explicit synchronization/import first;
4. ~~how Site identity, Material composition, occupancy, and the PLR projection
   relate~~ — D-097 persists Backend-shaped Sites, separates
   `Material.parent_uuid` composition from Site occupancy, and retires
   `resource_relation.slot_id` after migration;
5. ~~whether material, device, and Site concurrency share one lock and who owns
   it~~ — D-098 separates Task Material Reservations from atomic Job Execution
   Claims and fixes their owner/lifetime;
6. ~~what frontend-visible 404/409/422 distinctions are~~ — D-099 removes
   422 from WorkflowTask/Material preflight and fixes 400/404/409 semantics.

D-093 applies D-027's no-split rule to Task creation and Material lookup
without changing D-032's authoring-time Graph Authority. D-094 closes local
truth ownership and the runtime projection seam. D-095 closes canonical
identity and barcode/code naming. D-097 closes Site mapping. D-098 closes
admission-claim ownership and lifetime. D-099 closes the remaining disposition,
soft-delete, barcode uniqueness, contention, and frontend error semantics.
P0-3 is complete.

### P0-4: freeze the action-side ResourceSlot declaration contract

**Depends on:** P0-2.

**Blocks:** deterministic Registry projection, implicit action outputs,
ResourceSlot producer guarantees, action authoring documentation, and
material-flow E2E.

D-100 closes the typed source forms: parameters own inputs; `TypedDict` and
frozen dataclass own first-class named outputs; an inline dict return
annotation is compatibility syntax. The remaining decision must retire or
constrain legacy `@action(handles=...)`, close Action null/default behavior,
declare mutable ResourceSlots and occupancy-changing Sites, normalize runtime
results, and map the normalized contract deterministically to the real
Backend-shaped WorkflowNodeTemplate/WorkflowHandleTemplate catalog. D-067
through D-069 already fix the projected Handle semantics; no remaining step
may infer declarations from argument names or runtime examples.

### P0-5: freeze external `WorkflowTask.output`

**Depends on:** P0-2 and P0-3.

**Blocks:** root Workflow completion, result APIs/SSE, CLI/MCP consumption, and
output-contract E2E.

D-070 explicitly leaves this open (`decisions.md:1690`). The decision must
specify scalar, nullable, single ResourceSlot and `List[ResourceSlot]`
serialization; whether ResourceSlots are canonical
`{uuid, resource_template_uuid}` references; ordering/duplicate behavior;
failure behavior when final resolution or output contract validation fails; and
the exact `WorkflowTask.output`/event projection.

The frozen model already has a JSON `WorkflowTask.output` field
(`09609a2:internal/model/workflow.go:95-115`), so no parallel output model is
needed.

### P1-1: freeze the OS-only debugger launch and Breakpoint Hold command contract

**Depends on:** P0-1 for Task/source snapshot association and P0-2 for cut-input
preflight.

**Blocks:** debugger REST/SSE, restart recovery, frontend state rendering, and
debug E2E.

D-053 explicitly leaves one-or-many Hold step/resume behavior open
(`decisions.md:1011-1019`). The decision must specify:

1. debug launch route/DTO for `start_node_uuids` and
   `breakpoint_node_uuids`;
2. persisted Hold identity/state;
3. step/resume one Hold, all Holds, and an omitted target;
4. interaction between Node-local Hold and Task-global pause;
5. event names/payloads and REST rehydration;
6. out-of-scope Node projection distinct from disabled and runtime skipped.

Backend common run modes and Task commands require no new design: they are
listed in D-046 (`decisions.md:776-781`) and frozen source
(`09609a2:internal/service/workflow/planner.go:13-19`,
`09609a2:internal/service/workflow/task_command.go:14-20`).

### P1-2: freeze Conditional Join source syntax and catalog arity

**Depends on:** the authority-scoped template catalog from D-042; independent
of P0-2 through P0-4.

**Blocks:** complete conditional Python round trip and condition/join E2E.

Resolve the finite Handle issue in section 4.5 and define deterministic
compile/generate/source-map rules. Evidence:
`decisions.md:1073-1105`,
`09609a2:internal/service/workflow/planner.go:185-195`.

### P2: freeze the OS `tool_call` execution and isolation boundary

**Depends on:** base Task/Job state implementation; manual confirmation also
depends on frontend REST/SSE projection.

**Blocks:** claiming full frozen-Backend Workflow compatibility, but does not
block implementing Workflow definition, authoring, device-action execution, or
the debugger skeleton.

Frozen Backend planner includes `tool_call` and `manual_confirm`
(`09609a2:internal/service/workflow/planner.go:21-31`) and exposes manual
confirmation routes
(`09609a2:internal/http/handler/workflow.go:51-64`). D-058 puts those
frontend-visible semantics in scope (`decisions.md:1117-1125`).
`manual_confirm` therefore directly mirrors Backend and is not a new decision.
OS must also support `tool_call` rather than hiding it behind a capability
flag; the remaining decision is its local tool allowlist, credentials,
side-effect boundary, process isolation, cancellation and restart recovery.
This can follow the core device-action migration, but full frozen-Backend
Workflow compatibility cannot be claimed until it is implemented.

## 6. Items that should proceed without another design grill

These are substantial implementation tasks, but their contract is already
fixed:

| Implementation work | Why no new decision is needed | Evidence |
|---|---|---|
| Backend-shaped Workflow/Node/Edge/Task/Job persistence and UUID vocabulary | D-005/D-033/D-034/D-041 already fix it | `decisions.md:65-72`, `:445-491`, `:634-670`; frozen models at `09609a2:internal/model/workflow.go:47-169` |
| Task/Job/control/cleanup statuses and legal transitions | mirror frozen Service values | `09609a2:internal/service/workflow/status.go:5-126`, `:170-176`; D-058 at `decisions.md:1117-1140` |
| precreate planned pending Jobs in Task creation | frozen Service already does it transactionally | `09609a2:internal/service/workflow/execution.go:53-83` |
| no persisted WorkflowEdge execution state | D-046/D-053 require Node-only/derived behavior | `decisions.md:789-792`, `:1005-1019` |
| common Task commands and REST-only authoritative writes | frozen command contract plus D-024/D-025 | `decisions.md:263-289`; `09609a2:internal/service/workflow/task_command.go:14-111` |
| one frontend `/api/v1/events` SSE stream and REST rehydration | D-025/D-058 are final | `decisions.md:276-289`, `:1117-1140` |
| exact frozen response quirks | D-043 now names the route exceptions | `decisions.md:701-731`; `09609a2:internal/http/handler/event.go:28-34`, `09609a2:internal/http/handler/template.go:107-117` |
| ResourceTemplate aggregate deletion behavior | D-058 says frozen frontend behavior wins | `decisions.md:1107-1125`; `09609a2:internal/repository/template_graph.go:181-228` |
| local catalog must contain real stable Handle UUIDs before compile | D-042/D-069 already say compile fails on stale/missing catalog | `decisions.md:672-699`, `:1583-1629` |

The current Gaojing code demonstrates that this work has not yet been migrated;
it does not create new design choices:

- Against D-005 (`decisions.md:65-72`),
  `unilabos/app/scheduler/models.py:80-141` still uses `id`, `workflow_id` and
  `task_id`, and defaults Task identity to Workflow identity.
- `unilabos/app/scheduler/api.py:175-204` still makes
  `POST /api/v1/workflows` submit an execution and exposes old Job completion
  writes, contrary to the final D-027/D-034 split
  (`decisions.md:324-346`, `:464-491`).
- `unilabos/app/scheduler/api.py:217-263` still uses `/monitor/events` backed by
  an in-memory monitor bus rather than the D-025 `/events` contract
  (`decisions.md:276-289`).
- `unilabos/app/scheduler/history.py:37-75` stores `workflow_runs/job_runs`;
  `:170-192` converts active runs to `interrupted` and overwrites by
  `INSERT OR REPLACE`, rather than restoring WorkflowTask/WorkflowNodeJob facts.
  D-041 explicitly retires that store as Workflow authority
  (`decisions.md:634-670`).
- `unilabos/app/scheduler/dag_state.py:33-127` still uses mutable in-memory
  `WorkflowRun`, pending-parent and result maps instead of D-046/D-053's
  Node-only persisted Task/Job facts (`decisions.md:789-792`, `:1009-1019`).
- `unilabos/app/scheduler/service.py:104-151`, `:175-224`, and `:415-457`
  retain in-memory WorkflowRun/Job ownership and old identifiers, which is
  implementation debt against D-005/D-041
  (`decisions.md:65-72`, `:634-670`).
- `unilabos/workflow/from_python_script.py:17-29`, `:189-241` still allocates
  integer Node IDs and only walks top-level Assign/Expr statements, before
  D-029/D-051/D-054's UUID-anchored static authoring subset
  (`decisions.md:369-390`, `:906-940`, `:942-977`).
- `unilabos/registry/placeholder_type.py:6-32` still allows the legacy flattened
  single-ResourceSlot internal form, and
  `unilabos/registry/utils.py:560-608` does not yet project D-069 Handle
  metadata (`decisions.md:1583-1629`, `:1634-1690`).

This matches the phase ledger: only phase 00 is complete and phase 01 onward is
not yet planned in detail
(`docs/developer_guide/workflow_task_runtime_migration/README.md:21-37`).

## 7. Recommended next decision order

Do not reopen the supersession or Edge-scope questions. Grill only the current
blocker, then implement against the agreed phase gate:

P0-1 through P0-3 are closed. Continue from the first unresolved blocker:

1. P0-4 action ResourceSlot declaration contract;
2. P0-5 external Task output;
3. P1-1 debugger launch and Hold commands;
4. P1-2 Conditional Join syntax/arity;
5. P2 `tool_call` execution/isolation boundary.

P0-1's public seam is now chosen, so Phase 01 common Backend-shaped models/routes
can start; direct frozen-contract work does not need to wait for P1/P2. Complete
input/output execution cannot close until the remaining P0-4 and P0-5 are
resolved.
Debugger and conditional-join E2E should enter only after their respective P1
decisions are frozen.
