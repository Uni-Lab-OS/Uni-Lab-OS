---
status: accepted
---

# Reuse Core Intervention states

The first delivery uses the existing Core states rather than introducing the
original plan's synonymous state machine. A running WorkflowNodeJob that opens
an Error Incident becomes `intervention_required`; its WorkflowTask control
status becomes `waiting_intervention`. An accepted retry returns the same Job to
execution, while skip or Task termination moves it toward the corresponding
existing terminal path.

The shared Workflow Intervention lifecycle is only `open`, `resolved`, or
`superseded` for this phase. Pending and delivery-failed belong to Resolution
Submissions, not the Intervention. A Runtime rejection leaves it open. There is
no `expired` state because waits are open-ended, and no reconciliation state
because physical recovery is deferred.

## Consequences

Core REST, SSE, uni-lab-fe, and Runtime use one vocabulary. The delivery does
not add `WAITING_DECISION`, `WAITING_RECONCILIATION`, `RECONCILING`, or
`RESOLVING` aliases that later require migration.
