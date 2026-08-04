---
status: accepted
---

# Require new Resolution fields from Legacy Cloud

The existing Legacy Cloud decision endpoint is extended in place, but every new
submission must carry Runtime Intervention identity, expected revision, selected
option identity or action, and a Cloud-generated `client_request_id`. Backend
does not infer these from Task/device lookup or generate the client identity on
behalf of an Outdated Interaction Client.

If a cached old page omits a required field, Backend rejects it with an explicit
client-upgrade/refresh response and leaves the Intervention open. The three
components are released in a coordinated rollout and operators refresh old
tabs. No second compatibility idempotency scheme is built for a frontend being
retired.

## Consequences

Runtime version checks and end-to-end idempotency remain meaningful through the
Legacy Adapter. The endpoint path can stay stable with additive DTO changes,
while old request bodies fail visibly instead of being accepted under weaker
semantics.
