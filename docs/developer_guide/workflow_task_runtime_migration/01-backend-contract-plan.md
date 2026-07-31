# Phase 01 engineering plan: Backend-shaped Workflow contract

## Entry gate

- Phase 00 is complete on `integration/workflow-task-runtime`.
- Backend frontend Interface is frozen at `feat/workflow@09609a2`.
- D-073 through D-081 close the P0-1 persistent Authoring Interface.
- The target branch is `migration/01-backend-contract`.

No additional product grill is required for the work listed below. P0-2 is
closed by D-082 through D-092 and P0-3 by D-093 through D-099, but their
production schema/compiler/Material work belongs to the Phase 02 plan. P0-4
and P0-5 continue to gate action ResourceSlot projection and external output
semantics; Phase 01 must not invent those contracts.

## Outcome

Phase 01 establishes one local SQLite `workflow.db` and one frontend-facing
HTTP seam for the Backend-shaped Workflow definition and execution identity
model:

- static `Workflow`, `WorkflowNode`, and `WorkflowEdge` definitions;
- immutable `WorkflowTask.workflow_snapshot`;
- pre-created `WorkflowNodeJob` rows;
- graph revision concurrency and Backend response envelopes;
- the Workflow-scoped Authoring state/resource selected by D-073 through
  D-081;
- durable frontend events with global cursor replay.

The existing `EdgeScheduler` remains an execution implementation to be
deepened in later phases. It does not own the new definition store and its old
`workflow_runs/job_runs` history cannot become a compatibility authority.

## Module boundary

Production code is split into a deep workflow module and thin HTTP adapters:

```text
unilabos/workflow/models.py
    Backend-shaped public records and request-independent value types

unilabos/workflow/store.py
    SQLite schema, transactions, graph reconciliation, Task snapshot/Job
    creation, Authoring state, and durable event cursor

unilabos/workflow/service.py
    graph revision rules, source registration/containment, Draft/Apply
    orchestration, state derivation, and stable domain errors

unilabos/app/workflow_api.py
    FastAPI DTO validation, exact routes, response envelope, and SSE framing
```

Callers depend on `WorkflowService`, not SQLite queries. The service accepts an
injected Authoring compiler/catalog provider; Phase 02 supplies the production
Python compiler. Phase 01 tests use a deterministic fake provider so the
persistence and public contract can be completed without leaking the legacy
Canonical wire model.

## Implementation slices

### 01A — definition and graph authority

- Create singular SQLite tables `workflow`, `workflow_node`,
  `workflow_edge`, `workflow_node_template`, and
  `workflow_handle_template`.
- Implement Workflow create/list/get/update/delete.
- Implement Graph GET and revision-guarded full Graph PUT.
- Reconcile Nodes/Edges by UUID and soft-delete omitted entities.
- Advance Workflow revision and `update_time` atomically for graph-semantic
  writes.
- Apply D-045 root-parameter fallback without recursive Schema defaults.
- Return Backend `code/data/error` envelopes.

Gate:

```bash
/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q \
  tests/workflow/test_backend_contract_store.py \
  tests/app/test_workflow_contract_api.py
```

### 01B — immutable Task and Job identity

- Create `workflow_task` and `workflow_node_job`.
- `POST /api/v1/workflow-tasks` snapshots one committed Graph and pre-creates
  every planned `pending` Job in the same transaction.
- Expose Task list/detail, Task Job list, and Job detail.
- Keep Task creation separate from every graph/authoring write.
- Mirror frozen Task/Job/run-mode/status spelling; do not retain Run aliases.

Gate: Task snapshot remains unchanged after later graph edits, all planned
Jobs are immediately visible, and no public `run_id/task_id/node_id/job_id`
wire fields exist.

### 01C — persistent Authoring resource

- Register each editable `workflow_uuid` to one
  `package://<package-id>/<relative-path>` source below an explicitly loaded
  package root.
- Implement Authoring GET, Draft PUT double-CAS, and Apply three-token CAS.
- Persist invalid Draft source successfully while retaining the last Applied
  Graph.
- Persist only a server-owned Candidate selected by opaque hash.
- Commit graph/source/source-map/provenance/revision/event atomically on
  graph Apply; retain revision on proof-equivalent source-only Apply.
- Treat post-commit normalized-source writeback failure as a recoverable
  warning.
- Implement missing/deleted/restored source lifecycle and startup
  reconciliation.

Gate: D-073 through D-081 route/DTO/error/lifecycle contract tests.

### 01D — durable frontend event stream and composition

- Create a durable global event ledger in `workflow.db`.
- Implement `/api/v1/events` replay with `Last-Event-ID`.
- Emit only `workflow.authoring.changed` for Authoring invalidation.
- Mount the new router from the main OS composition root when the local
  Workflow authority is configured.
- Stop mounting the old execution-shaped `/workflows` routes on the same
  public path; keep legacy scheduler internals private until their owning
  migration phase replaces them.

Gate: restart/replay/deduplication tests plus the preserved target baseline.

## Verification gate

Phase 01 closes only after:

1. all new Phase 01 tests pass with the required Python 3.11 interpreter;
2. the target baseline suites remain green;
3. `rg` finds no new Run vocabulary or old runtime route in the new module;
4. SQLite restart tests prove graph, Task snapshot, Jobs, Authoring state, and
   event replay durability;
5. `git diff --check` is clean;
6. the README phase ledger records the exact completed scope and remaining
   blockers.

## Explicit non-goals

- No P0-2 schema/compiler implementation in Phase 01; D-082 through D-092 are
  implemented by the Phase 02 plan.
- No Material Authority implementation in Phase 01; its P0-3 contract is
  frozen for Phase 02 by D-093 through D-099.
- No P0-4 action ResourceSlot declaration inference.
- No P0-5 final external Task output contract.
- No breakpoint Hold, Conditional Join catalog, or `tool_call` executor.
- No Backend source changes and no Backend-to-Edge protocol migration.
- No compatibility adapter for `/api/v1/runtime/runs` or the old use of
  `POST /api/v1/workflows` as execution submission.

## Implementation checkpoint — 2026-07-30

Implemented on `migration/01-backend-contract`:

- workspace-scoped `workflow.db` composition and singular Backend-shaped
  definition, Task, Job, Authoring, and frontend-event tables;
- Workflow CRUD, graph GET/full PUT with UUID reconciliation and revision CAS,
  immutable Task snapshots, and atomic pending-Job precreation;
- D-073 through D-081 Authoring aggregate, Draft double CAS, server-owned
  Candidate, three-token Apply, source-only Apply, exact Draft lifecycle,
  recoverable normalized-source writeback, and durable small invalidations;
- `/api/v1/events` SSE framing and `Last-Event-ID` replay over the durable
  global event cursor;
- main-web composition that mounts the new Workflow authority from
  `BasicConfig.working_dir/workflow.db` and does not mount the old scheduler's
  execution-shaped `/workflows` routes on the same public app.

Evidence:

```text
16 passed, 1 warning
336 passed, 3 skipped, 5 warnings
ruff E/F/I: clean
git diff --check: clean
```

Before Phase 01 is closed, expand the shared frozen Backend route surface that
is not part of this first contract slice (individual Node/Edge administration
and ordinary Task commands), add its parity tests, and repeat the full phase
gate. Phase 02 still owns the production Python compiler, action/template
catalog import, and package source discovery; Phase 01 uses the injected
compiler seam proven by deterministic contract tests.
