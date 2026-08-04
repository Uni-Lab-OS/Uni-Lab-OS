---
status: accepted
---

# Error abort terminates the Task as failed

An accepted `abort` Resolution means the operator declines further recovery
from the Error Incident and terminates the entire WorkflowTask because of that
failure. Runtime retains the failed Execution Attempt and its Incident, stops
admitting new Jobs, and settles already-dispatched sibling work. Once that work
is safely settled, the WorkflowTask reaches `failed` rather than `canceled`.

The Task does not enter its final failed state merely because the intent was
accepted. It remains in the existing termination-in-progress path while a
physical command is outstanding, fenced, or has an unknown outcome. A separate
ordinary Task cancel command remains the source of the `canceled` terminal
status.

## Consequences

History distinguishes a workflow abandoned because an Action failed from one
the user canceled without such a failure. Legacy Cloud may label the button
"terminate workflow," but its resulting projection must preserve the failure
reason after OS acknowledges the Resolution.
