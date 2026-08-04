---
status: accepted
---

# Task abort supersedes other open Incidents

When the Runtime atomically accepts an Abort Intent through one Error Incident,
the WorkflowTask enters its termination flow and every other open Incident in
that Task becomes `superseded_by_task_abort`. A superseded Incident retains its
original error and history; it is not considered successfully resolved and it
no longer accepts `retry` or `skip` requests.

The Runtime acceptance order is authoritative. A Job-scoped resolution accepted
before the Abort Intent retains its result. Once the Abort Intent is accepted,
later competing Job-scoped submissions are rejected because the Task is
terminating. Backend queue order and Cloud display order do not determine the
winner.

## Consequences

Cloud can distinguish "the user handled this error" from "the whole Task made
this choice irrelevant." Audit history remains truthful, and concurrent
submissions have a deterministic result even when they arrive through different
Interaction Adapters.
