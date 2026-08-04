---
status: accepted
---

# Preserve resolution idempotency end to end

Cloud creates one `client_request_id` for each exact user Resolution
Submission, Backend preserves it unchanged, and the Runtime Authority durably
stores the first processing result for `(incident_id, client_request_id)`.
Replaying the same request content returns that original result. Reusing the
same request identity with different content is rejected as an idempotency
conflict; the first request and its result remain unchanged.

## Consequences

Transport retries caused by HTTP, Redis, WebSocket, Backend restart, or Runtime
restart reuse the original request identity. After Runtime rejection, Cloud
refreshes the incident. If the incident remains open and the user confirms a
new selection against its latest version, that new submission receives a new
`client_request_id`; an automatic retry must not silently change either the
selection or the observed Incident Version.
