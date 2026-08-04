---
status: accepted
---

# Require Runtime acknowledgement for error resolutions

Submitting an error response to Cloud or Backend records only a Resolution
Submission. An incident is processed only after the Runtime Authority validates
and durably accepts the response; Runtime rejection is also projected back to
the user with its reason. This prevents a successful HTTP response, a queued
Redis message, or a read notification from claiming that retry, skip, abort, or
fallback occurred when the Edge is offline or the incident is stale or unsafe.

## Consequences

The Legacy Interaction Adapter needs an Edge-to-Backend acknowledgement that
is projected to Cloud. Cloud presents a submitted/pending state until that
acknowledgement arrives and labels the incident processed only after Runtime
acceptance.

The first delivery reuses the existing paths in both directions:

1. Runtime persists an open Incident and sends it through the existing Edge
   upstream path; Backend stores a notification projection and uses existing
   SSE to display it in Cloud.
2. Cloud creates a `client_request_id` and submits the selected action together
   with `incident_id` and `incident_version`. Backend records it as pending and
   returns only a submission acknowledgement.
3. Backend uses its existing Redis control queue and Edge downstream path to
   deliver the intent to OS.
4. Runtime atomically accepts or rejects it, persists the result, and sends an
   additive `device_exception_decision_result` message over the existing Edge
   upstream path.
5. Backend correlates the result by `client_request_id`, updates its projection,
   and publishes the final state through existing SSE. Cloud then shows
   accepted/processed or rejected and refreshes the current Incident.

No Cloud-to-OS connection or new transport is introduced. Backend and Cloud
remain projections even though they carry the Runtime result.
