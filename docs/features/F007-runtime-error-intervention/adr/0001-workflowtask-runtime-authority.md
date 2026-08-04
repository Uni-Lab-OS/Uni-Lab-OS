---
status: accepted
---

# Keep WorkflowTask Runtime authoritative behind legacy interaction adapters

The durable WorkflowTask Runtime in Uni-Lab-OS is the final state authority for
execution attempts, error incidents, reconciliation, and error resolution. The
first delivery must retain Uni-Lab-Cloud `feat/lixinyu/dev` and
uni-lab-backend `test` as mandatory Legacy Interaction Adapters: they may
display incidents, notify users, and forward user intent, but they must not
independently decide or persist the authoritative retry, skip, abort, fallback,
or reconciliation result. This preserves the current delivery path with
localized additive changes while allowing the same Runtime mechanism to move
into the actively developed Uni-Lab-Core pairing of Uni-Lab-OS and uni-lab-fe.

## Considered Options

- Make the current Cloud notification and Backend Redis/WS flow the new Runtime
  authority. Rejected because it would create a second recovery state machine
  and make local Core execution depend on the legacy cloud stack.
- Replace the current Cloud and Backend contracts in the first delivery.
  Rejected because those projects are required delivery dependencies and may
  not be substantially rewritten.

## Consequences

Cloud may only claim that a decision was submitted until the Runtime accepts
it. Backend and Edge compatibility messages must carry stable incident/job
identity, concurrency version, and request idempotency data without evaluating
the requested resolution themselves.
