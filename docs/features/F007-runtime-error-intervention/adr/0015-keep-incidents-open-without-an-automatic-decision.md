---
status: accepted
---

# Keep Incidents open without an automatic decision

An Error Incident requiring interaction remains open and its WorkflowTask
remains paused until Runtime accepts an explicit Resolution. The first delivery
does not automatically select `retry`, `skip`, or `abort` when a decision timer
elapses.

This replaces the legacy OS behavior that defaults to a 300-second wait and can
then automatically choose a configured action. Adapter retries, notification
expiry, disconnected users, and elapsed wall-clock time do not resolve the
Incident.

## Consequences

The system never presents an automatic timeout choice as user intent. Long-lived
open Incidents and paused Tasks must survive process restart and reconnection.
A future operational timeout policy may resolve an Incident only as an explicit,
auditable `system_policy` actor and requires a separate decision.
