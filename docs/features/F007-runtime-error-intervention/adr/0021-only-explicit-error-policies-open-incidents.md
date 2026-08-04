---
status: accepted
---

# Only explicit error policies open Incidents

Runtime creates an interactive Error Incident only when the Action's
error-handling logic explicitly opts a matching failure into an Interactive
Error Policy and supplies at least one supported recovery option. Runtime then
adds `abort` if necessary and durably pauses the affected workflow for a
Resolution.

An exception without a matching Interactive Error Policy follows the existing
ordinary Job and WorkflowTask failure path. It is logged and projected as a
failure where appropriate, but does not open a user-decision prompt or wait
indefinitely.

The first delivery preserves the existing exception-class-name and MRO matching
implemented by Action `error_policy`, including its explicit `"*"` fallback.
The virtual device defines its own test exception. No global
`CommunicationError`, error-code registry, or common device-exception hierarchy
is introduced before a real Action supplies concrete requirements.

## Consequences

Programming errors, validation failures, and unknown exceptions do not silently
become permanent operator waits. Device and operation owners explicitly choose
which failures are recoverable through `retry` or `skip`, keeping the first
delivery finite and making interactive behavior discoverable in the Action
contract.
