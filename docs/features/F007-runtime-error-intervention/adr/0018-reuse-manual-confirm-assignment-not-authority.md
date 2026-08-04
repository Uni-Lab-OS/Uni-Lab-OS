---
status: accepted
---

# Reuse Manual Confirm identity and audit but not selection or authority

Error Incident access reuses the existing Manual Confirm identity, permission
check, and audit pattern. The authenticated user identity is taken from the
trusted ingress rather than the request body; only an Incident Assignee may
submit a Resolution; the durable audit records the actor identifier, action,
time, and optional comment.

The reuse stops at this identity, permission, and audit boundary. Error
Incidents do not reuse Manual Confirm's node-level assignee selector because it
is not a reliable general Incident assignment source. They also do not inherit
its deadline or expiry because they remain open until an explicit Resolution is
accepted. Legacy Backend does not inherit Manual Confirm's authority to
transition the Job: it checks the assignee and forwards the authenticated actor,
while OS Runtime atomically accepts or rejects the Resolution and owns all Task,
Job, and Incident state changes.

For direct uni-lab-fe operation, the OS authentication ingress applies the same
assignment rule and records the same actor fields without requiring the old
Backend.

## Consequences

The first delivery reuses established authenticated-actor and audit behavior
without depending on an unreliable selector or introducing another RBAC model.
Existing Backend Manual Confirm selection state, timeouts, and Redis completion
mechanics cannot be treated as the Error Incident state machine.
