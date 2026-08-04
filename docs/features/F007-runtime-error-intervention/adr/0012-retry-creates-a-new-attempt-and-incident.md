---
status: accepted
---

# Retry creates a new Attempt and a new Incident on failure

When the Runtime accepts a Retry Resolution, it permanently records that
resolution on the current Error Incident and creates a new execution Attempt.
The WorkflowNodeJob identity remains unchanged. The failed Attempt and its
Incident remain immutable. If the new Attempt also fails, Runtime creates a new
Incident owned by that Attempt and links it to the earlier Incident through
`caused_by_incident_ids`.

The new Incident has its own identity, version, available resolutions, and
Resolution Submissions. A user responding to it creates a new
`client_request_id`; transport retries of that exact response continue to reuse
that new ID.

The first delivery reuses the existing `error_policy.max_retries` field and its
current default of three; it does not add another recovery-budget setting. OS
evaluates that limit from durable Job Attempts, so restart does not reset it.
After the configured retry count is exhausted, a later Incident omits `retry`
while retaining any configured `skip` and the Runtime-provided `abort`.

## Consequences

Repeated execution is represented as a sequence of Attempts and Incidents
instead of mutations to one error record. Runtime can audit exactly which user
choice caused each Attempt, while idempotency results and optimistic versions
remain unambiguous.
