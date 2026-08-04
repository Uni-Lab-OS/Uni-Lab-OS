---
status: accepted
---

# Durably expose Resolution results

Runtime persists a Durable Runtime Event in the same durable state change that
accepts or rejects a Resolution Submission. The canonical OS REST/SSE contract
exposes this committed result through the durable global event cursor; a client
replays after its last cursor and rehydrates authoritative state through REST.
The event and result survive transient failure, reconnection, and process
restart.

For the first-delivery Legacy Adapter only, the adapter translates that event
to `device_exception_decision_result` and retains its delivery progress until
the old Backend acknowledges the projection. Backend handles repeated result
messages idempotently using `client_request_id`. This adapter-specific ACK is
not part of the Runtime domain or the future uni-lab-fe contract.

## Consequences

If OS commits a Resolution result and crashes before or during transmission,
consumers can still observe it after recovery. Redelivery never creates another
Resolution or changes which concurrent submission won. The old Backend bridge
can be deleted during migration without changing Runtime persistence, event
identity, REST state, or the direct uni-lab-fe interaction model.
