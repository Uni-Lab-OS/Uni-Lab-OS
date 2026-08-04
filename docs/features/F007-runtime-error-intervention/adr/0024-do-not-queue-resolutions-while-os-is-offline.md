---
status: accepted
---

# Do not queue Resolutions while OS is offline

Legacy Backend forwards `retry`, `skip`, and `abort` only while the target OS
Edge session is online. If OS is already offline, Backend rejects the submission
instead of placing it in a long-lived Redis queue. If delivery fails during a
disconnect race, the submission becomes delivery-failed rather than remaining
indefinitely pending for later execution.

Cloud keeps the Incident open and tells the operator that OS is unavailable.
After reconnection it refreshes authoritative Incident state; a new user click
creates a new `client_request_id`. `abort` remains visible as an available
intent, but an unreachable Runtime cannot be represented as having accepted it.

## Consequences

A stale physical-control decision cannot execute unexpectedly minutes after the
operator clicked it. Existing Redis remains an online handoff mechanism, not a
durable delayed Runtime command queue. Supporting intentional offline commands
would require a separately designed TTL, visible queued state, cancellation,
and Runtime revalidation policy and is outside the first delivery.
