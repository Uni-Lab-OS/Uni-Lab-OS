---
status: accepted
---

# Defer real-device error policies

The first development phase implements the Runtime mechanism, canonical OS
contract, Legacy Interaction Adapter, and automated behavior using a virtual
device with a Simulated Interactive Action inside a virtual Workflow. The test
executes the real Action-to-Runtime-to-Backend-to-Cloud path; it does not create
an Incident by bypassing Action execution. The phase does not add `error_policy`
to a production device Action or require a human-supervised hardware run for
acceptance.

Production Drivers remain unchanged and therefore do not begin opening new
interactive Incidents merely because the framework is deployed. A later device
rollout must choose concrete exceptions and options with the device owner and
perform hardware-specific safety and recovery acceptance.

## Consequences

The initial delivery can validate Action exception matching, persistence,
concurrency, idempotency, restart, Backend bridging, SSE, Cloud interaction, and
the resulting Workflow state deterministically without risking laboratory
equipment. It must be described as mechanism delivery rather than a completed
production-device rollout.
