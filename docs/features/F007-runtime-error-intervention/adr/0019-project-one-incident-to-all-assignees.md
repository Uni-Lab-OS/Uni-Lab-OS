---
status: superseded by ADR-0023
---

# Project one Incident to all assignees

Legacy Backend creates an Assignee Notification Projection for each Incident
Assignee, following the existing Manual Confirm notification fan-out. Every
projection references the same Runtime `incident_id`; notification UUIDs do not
create separate Incidents or separate resolution authority.

Any assignee may submit a Resolution. After OS Runtime accepts the first valid
submission, its Resolution Result Projection updates every notification for the
shared Incident. Each assignee can see the accepted action, actor, and time.
Backend does not settle only the submitting user's notification or let the
remaining projections continue to appear open.

A pending submission does not globally lock the Incident. All assignees may see
who submitted which action while waiting for Runtime, and another assignee may
still submit. The submitting client suppresses duplicate clicks for its exact
request, but only Runtime acceptance settles the Incident and disables competing
choices. This prevents a lost or indefinitely pending first delivery from
blocking every other assignee.

## Consequences

The first delivery supports operational handoff without duplicating errors.
Notification creation and final updates may reuse Manual Confirm's fan-out and
bulk-update patterns, while Runtime identity, concurrency, and accepted outcome
remain singular and portable to direct uni-lab-fe operation.
