---
status: accepted
---

# First delivery assigns Incidents only to the Task trigger actor

The first delivery records a first-class WorkflowTask Trigger Actor and makes
that actor the sole Incident Assignee. Legacy Backend supplies the existing
WorkflowTask user identity; future direct OS ingress records its authenticated
principal. The actor identity is not accepted from an untrusted Resolution body
or hidden in generic Task metadata.

Additional assignee selection and notification fan-out are deferred. The
existing Manual Confirm node selector is not reused because it is not currently
a reliable general-purpose selection path. Future expansion derives assignees
from laboratory-level permission configuration and snapshots the resulting list
on the Incident; the previously designed first-Runtime-accepted concurrency
semantics can then apply without changing Incident identity.

## Consequences

The old Cloud creates one notification for the Task trigger actor in the first
delivery. No new legacy user picker or multi-user notification workflow is
built. OS must add the trigger actor as a durable WorkflowTask field so the same
default remains available after migration away from Backend.
