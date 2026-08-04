---
status: accepted
---

# Keep the action wire extensible but certify three actions

The first delivery implements and tests only `retry`, `skip`, and `abort` as
Resolution semantics. Each Incident may expose an error-specific subset, and
Runtime always makes `abort` available.

The action identifier remains an extensible string in the Interaction Adapter
contract rather than becoming a closed enum. This preserves compatibility with
the earlier Cloud and Backend shape, which can carry `manual_fix` and arbitrary
action names, and leaves room for future Core development. Extensibility of the
wire format does not authorize execution: Runtime publishes only supported
options and rejects an unknown or unregistered action even if a legacy adapter
forwards it.

## Consequences

Cloud and Backend require no redesign to carry future options, while the first
delivery has a finite behavior and test matrix. Existing custom-action hooks are
not part of the first-delivery guarantee and cannot bypass Runtime validation.
