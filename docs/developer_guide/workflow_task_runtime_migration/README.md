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
test-author/full-suite/independent-review gate in the concrete execution plan.

## Status

| Phase | Purpose | Status |
|---|---|---|
| 00 | Baseline, decisions, manifests, test evidence | complete |
| 01 | Backend-aligned Workflow/WorkflowTask contract | complete |
| 02 | Canonical Workflow and Python/JSON authoring；02H closes generic Task input preflight only | in progress; 02A/02B complete, 02C candidate pending final gate/merge |
| 02H+ | Action、Material、runtime、scheduler/device、subworkflow/output、frontend、debugger 和 integration functional slices | planned by functional owner in the FE–OS matrix |
| 03～09 | Historical source-inventory buckets | superseded as an execution order; retained only for migration provenance |

Only the current phase and the immediately unblocked next repository-local
slice are expanded into implementation detail. Cross-repository work after
02H follows the functional Wayfinder hierarchy and writes repository specs only
when the corresponding protocol and delivery child reach their entry gate.

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
- [FE–OS 交互迁移矩阵与 Phase 02H 起整体计划](fe_os_interaction_migration_matrix.md)
- [Migration manifest](migration-manifest.md)
- [Test inventory](test-inventory.md)
- [每轮测试与评审门禁记录模板](round-gate-template.md)
- [当前 01 Backend contract 门禁记录（待完成）](rounds/01-backend-contract.md)

## Lifecycle

This directory is not permanent product documentation. Before phase 09 closes,
lasting invariants must be distilled into the repository `AGENTS.md`, maintained
module READMEs, and the formal Interface documentation. This temporary
migration directory is then removed.
