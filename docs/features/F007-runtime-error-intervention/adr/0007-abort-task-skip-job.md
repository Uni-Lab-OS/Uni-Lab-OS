---
status: accepted
---

# Scope abort to the Task and skip to the Job

`abort` always expresses an Abort Intent for the entire WorkflowTask: no new
Job is admitted and already-dispatched physical work is settled without
assuming it can be recalled. `skip` is the Job-scoped alternative: it closes
the current WorkflowNodeJob without re-executing the failed action and lets the
Runtime evaluate unaffected or otherwise executable work. A skipped Job is
recorded as `skipped`, never disguised as `succeeded`.

As a deliberate first-delivery compatibility exception, the old Backend wire
continues receiving its existing `status=success` result with
`return_info.suc_type=skip`. This encoding exists only in the Legacy Adapter;
it does not change the OS Runtime Job state or define the future Core REST
projection.

## Consequences

Cloud labels `abort` as terminating the workflow and `skip` as skipping the
current step. Whether downstream Jobs remain executable depends on their input
contracts and the skipped Job's output semantics; that decision is not hidden
inside the meaning of abort. Native Backend `skipped` scheduler convergence is
deferred until migration rather than expanded in the legacy delivery.
