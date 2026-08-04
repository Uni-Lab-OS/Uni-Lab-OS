---
status: accepted
---

# Defer physical recovery and fallback

The first delivery does not add a generic `physical_state` model,
`RecoveryPolicy`, reconciliation states or commands, device-status confirmation
UI, or scheduled fallback Recovery Actions. It implements only the existing
Action `error_policy` choices `retry` and `skip`, plus Runtime-provided `abort`,
against virtual-device failures with deterministic retry safety.

Existing OS locks, fences, restart handling, and `execution_unknown` behavior
remain in force and are not rewritten by this feature. The delivery does not
claim that a virtual retry test proves a physical device can be retried safely.
Physical Recovery Extensions are designed with the owner when a concrete real
Action is onboarded.

## Consequences

The original plan's reconcile UX, safe-retry decision table, fallback attempts,
and related acceptance cases leave the first-phase backlog. This prevents an
unvalidated generic physical-safety framework from dominating a delivery whose
production Drivers are intentionally unchanged.
