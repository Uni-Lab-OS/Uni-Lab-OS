---
status: accepted
---

# Core uses narrow SSE and REST rehydration

The canonical OS `GET /api/v1/events` stream publishes durable Invalidation
Events for Workflow Intervention changes. An event identifies the changed
Intervention and its attention transition; it does not carry the complete error,
traceback, options, actor, or decision history. uni-lab-fe responds by reading
the current authoritative Intervention through REST, and page refresh restores
all open Interventions through the REST list.

The first delivery does not build a rich Runtime Event Reducer in the frontend
or require the UI to reconstruct state by folding event payloads. Legacy
Uni-Lab-Cloud may continue receiving a complete `device_exception` Notify SSE
payload so its existing prompt can be reused; that payload is a disposable
Legacy Adapter projection, not the Core event contract.

## Consequences

Duplicate, delayed, or missed SSE notifications cannot become state truth. The
new frontend follows the already frozen Core interaction pattern, while the old
Cloud avoids a broad RuntimeClient rewrite during its remaining delivery life.
