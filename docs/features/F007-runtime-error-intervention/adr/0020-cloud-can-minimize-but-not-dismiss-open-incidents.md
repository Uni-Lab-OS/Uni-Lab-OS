---
status: accepted
---

# Cloud can minimize but not dismiss open Incidents

Legacy Cloud initially presents an interactive Error Incident as a blocking
prompt. The operator may minimize the prompt in order to inspect logs, device
state, workflow context, or other pages, but cannot dismiss the Incident as if
it were handled. Minimizing produces a persistent, prominent indicator such as
"workflow paused: one error requires action" that reopens the same Incident.

The WorkflowTask remains paused while the prompt is expanded or minimized. The
indicator survives navigation and is restored after refresh by rehydrating open
Incident state. It disappears only when OS reports that the Incident was
resolved, superseded by Task abort, or its WorkflowTask reached a terminal
state. A pending Backend submission does not remove it.

The old Cloud implementation remains deliberately small: reuse the existing
exception prompt and notification drawer, add a minimize action and one global
open-Incident indicator, and deduplicate by Runtime `incident_id`. This is UI
projection behavior, not Runtime state.

## Consequences

An unresolved laboratory error cannot silently disappear, but it also does not
prevent the operator from gathering the information needed to choose a safe
Resolution. The same interaction can later be reproduced in uni-lab-fe without
carrying over old Notify or SSE internals.
