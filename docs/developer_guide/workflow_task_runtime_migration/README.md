# WorkflowTask runtime migration

This temporary directory is the working ledger for migrating the current
workflow authoring, scheduler, runtime, and debugger capabilities onto the
reviewed Edge baseline.

The migration uses phase gates:

1. inspect facts;
2. resolve implementation decisions one at a time;
3. record the agreed phase contract;
4. implement only that phase;
5. run the agreed unit and E2E gates;
6. close the phase before planning the next one.

The reviewed source branch `feat/edge-networking-and-scheduler` remains
unchanged. Completed phases accumulate on
`integration/workflow-task-runtime`; active work is isolated on
`migration/NN-*`.

## Status

| Phase | Purpose | Status |
|---|---|---|
| 00 | Baseline, decisions, manifests, test evidence | complete |
| 01 | Backend-aligned Workflow/WorkflowTask contract | not planned in detail |
| 02 | Canonical Workflow and Python/JSON authoring | not planned in detail |
| 03 | Node-centric control-DAG scheduler | not planned in detail |
| 04 | Durable node/job/event runtime and reconciliation | not planned in detail |
| 05 | Run-scoped debugger semantics | not planned in detail |
| 06 | HostLink and device execution integration | not planned in detail |
| 07 | Material authority, leases, reservations, and ledger | not planned in detail |
| 08 | Frontend and Cloud integration | not planned in detail |
| 09 | Cleanup, security, complete regression, and release | not planned in detail |

Only the current phase is expanded. Later phase details are deliberately
deferred until their entry grill.

## Ledger

- [Baseline](00-baseline.md)
- [Decisions](decisions.md)
- [Migration manifest](migration-manifest.md)
- [Test inventory](test-inventory.md)

## Lifecycle

This directory is not permanent product documentation. Before phase 09 closes,
lasting invariants must be distilled into the repository `AGENTS.md`, maintained
module READMEs, and the formal Interface documentation. This temporary
migration directory is then removed.
