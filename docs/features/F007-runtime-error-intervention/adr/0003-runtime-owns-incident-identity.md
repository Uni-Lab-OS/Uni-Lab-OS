---
status: accepted
---

# Let Runtime issue the canonical incident identity

The Runtime Authority creates and durably stores one `incident_id` for every
Error Incident, and that identity follows all projected events, Resolution
Submissions, and Runtime acknowledgements. Backend `notify_uuid` remains only
the identity of a notification projection; `job_id` and `attempt` provide
execution context but do not identify the incident. The legacy `decision_id`
may temporarily alias `incident_id` during migration and is then removed.

## Consequences

Backend must preserve `incident_id` unchanged in both directions and must not
route a response using only `task_id + device_id`. Multiple incidents from the
same Job, attempt, Task, or device remain independently addressable.
