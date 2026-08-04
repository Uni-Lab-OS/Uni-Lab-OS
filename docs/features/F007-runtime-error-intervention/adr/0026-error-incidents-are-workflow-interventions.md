---
status: accepted
---

# Error Incidents are Workflow Interventions

An Error Incident is the `action_error` specialization of the existing durable
Workflow Intervention model and public contract. It shares one identity,
revision, option set, decision history, state, and terminal outcome with that
Intervention; OS does not create a parallel `error_incident` authority or API.

The canonical frontend surface remains the frozen Intervention contract:
`GET /api/v1/workflow-interventions?status=open`,
`POST /api/v1/workflow-interventions/{uuid}/decisions`, and the global
`GET /api/v1/events` stream with `intervention.required`,
`intervention.resolved`, and `intervention.superseded`. Incident version maps to
Intervention revision, a Resolution Option maps to option ID, and
`client_request_id` maps to the decision idempotency key.

Legacy Adapter projects the same record into `device_exception_alarm` and
translates old Cloud submissions back into Intervention decisions. It does not
create another open/resolved lifecycle.

## Consequences

uni-lab-fe can consume the already selected Core API instead of migrating from
a newly invented `/runtime/runs` protocol. Runtime code may use Error Incident
language where the error domain matters, while persistence and transport retain
one Intervention authority.
