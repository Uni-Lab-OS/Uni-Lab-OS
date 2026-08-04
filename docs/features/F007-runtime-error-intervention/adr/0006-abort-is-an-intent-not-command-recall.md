---
status: accepted
---

# Treat abort as an intent rather than command recall

Every open Error Incident offers an `abort` resolution representing the user's
Abort Intent. Accepting it prevents further WorkflowTask admission and begins
settlement, but it does not retract device commands that were already sent or
claim that their physical effects stopped. Only Runtime evidence or explicit
reconciliation establishes Physical Stop.

## Consequences

An accepted abort may leave the Task `canceling` and a Job
`execution_unknown`, with execution claims fenced, until physical completion,
cancel acknowledgement, or reconciliation settles reality. Cloud presents
that intermediate state instead of immediately claiming a terminal Task. The
Runtime always supplies the abort option for an open incident; Interaction
Adapters do not invent it when Runtime state is unavailable.

When abort is accepted as an Error Incident Resolution, it means the operator
declines recovery from that error. After in-flight work safely settles, the
WorkflowTask terminates as `failed`, not `canceled`; ordinary Task cancellation
remains the source of `canceled`.
