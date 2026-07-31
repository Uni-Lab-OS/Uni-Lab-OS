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
unchanged. Completed rounds accumulate on
`integration/workflow-task-runtime`; every mergeable plan slice runs on a
fresh `migration/<round>-<topic>` branch and passes the independent
test-author/full-suite/multi-review gate in the concrete execution plan.

## Status

| Phase | Purpose | Status |
|---|---|---|
| 00 | Baseline, decisions, manifests, test evidence | complete |
| 01 | Backend-aligned Workflow/WorkflowTask contract | complete |
| 02 | Canonical Workflow and Python/JSON authoring | in progress; 02A complete, 02B production caller pending |
| 03 | Node-centric control-DAG scheduler | not planned in detail |
| 04 | Durable node/job/event runtime and reconciliation | not planned in detail |
| 05 | Run-scoped debugger semantics | not planned in detail |
| 06 | HostLink and device execution integration | not planned in detail |
| 07 | Material authority, leases, reservations, and ledger | not planned in detail |
| 08 | Frontend and Cloud integration | not planned in detail |
| 09 | Cleanup, security, complete regression, and release | not planned in detail |

Only the current phase and the immediately unblocked next phase are expanded.
Later phase details are deliberately deferred until their entry grill.

The lettered slices in the concrete plan are the engineering rounds. Historical
02A/02B hardening sub-rounds remain in Git for provenance, but new work does not
invent a numbered sub-round as a substitute for closing the active slice. After
each round passes its gate, write its Chinese trend/strategy report and proceed
directly to the next planned slice without a separate consent stop. A real
decision blocker or an expansion of authority still stops execution.

## Ledger

- [Baseline](00-baseline.md)
- [Phase 01 engineering plan](01-backend-contract-plan.md)
- [Phase 01 收尾与 Phase 02 Authoring/Schema 具体执行计划](02-authoring-schema-plan.md)
- [Decisions](decisions.md)
- [Decision status audit](decision_status_audit.md)
- [Backend design comparison](backend_design_comparison.md)
- [旧版 FE–OS 交互迁移矩阵](fe_os_interaction_migration_matrix.md)
- [Migration manifest](migration-manifest.md)
- [Test inventory](test-inventory.md)
- [每轮测试与评审门禁记录模板](round-gate-template.md)
- [当前 01 Backend contract 门禁记录（待完成）](rounds/01-backend-contract.md)

## Lifecycle

This directory is not permanent product documentation. Before phase 09 closes,
lasting invariants must be distilled into the repository `AGENTS.md`, maintained
module READMEs, and the formal Interface documentation. This temporary
migration directory is then removed.
