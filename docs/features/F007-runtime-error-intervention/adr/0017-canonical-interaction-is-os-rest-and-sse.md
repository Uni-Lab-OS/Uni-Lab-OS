---
status: accepted
---

# Make OS REST and SSE the canonical interaction contract

The long-lived interaction contract is owned by OS: REST reads and Resolution
writes operate on authoritative Runtime records, while `GET /api/v1/events`
provides durable cursor-based invalidation and replay. The actively developed
uni-lab-fe uses this contract directly and rehydrates state through REST after
events or reconnects.

Error interaction uses the frozen Workflow Intervention routes, including open
Intervention reads and
`POST /api/v1/workflow-interventions/{intervention_uuid}/decisions`; it does not
introduce `/api/v1/runtime/runs*`, a per-run event stream, or a generic Runtime
command endpoint.

The mandatory first delivery continues to use the old Uni-Lab-Cloud and
uni-lab-backend, but only through a thin Legacy Interaction Adapter. It maps
canonical Incident events to `device_exception_alarm`, forwards old Cloud
submissions as Runtime commands, and maps canonical Resolution results to
`device_exception_decision_result`, Notify updates, and the existing Backend
SSE. Adapter delivery may use a small idempotent ACK but does not introduce a
Backend-owned Runtime, general message platform, or recovery engine.

The old Cloud browser does not connect directly to OS. Doing so would add a
second client stack plus Edge discovery, network reachability, TLS, CORS, and
authentication work to a frontend being retired.

## Consequences

The Runtime model, idempotency, durable events, REST contract, and SSE behavior
migrate unchanged to uni-lab-fe. Old Redis, Edge message names, Notify records,
and Cloud UI wiring remain explicitly disposable delivery adapters. Correctness
does not depend on preserving those legacy components after migration.
