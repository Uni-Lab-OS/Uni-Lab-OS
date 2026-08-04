---
status: accepted
---

# Error logic defines each Incident's resolution options

The error-handling site that understands the failed operation defines the
Incident's user-facing title, explanation, and available Resolution Options.
Different errors may offer different actions even when they share a technical
state such as `execution_unknown`. Runtime does not infer a global set of
`retry`, `skip`, or reconciliation choices from an exception category.

Runtime durably records the options for the current Incident version and
accepts only a currently recorded option. It provides optimistic versioning,
idempotency, audit history, and execution of the registered resolution
semantics. Runtime always adds the WorkflowTask-wide Abort Intent if the error
logic did not supply it. Cloud renders the supplied content and options, and
Backend forwards them without inventing or reinterpreting choices.

The first delivery certifies only the existing generic recovery actions
`retry`, `skip`, and `abort`. It does not implement the proposed group of
"inspect device state," "confirm completed," or "confirm not executed"
interactions. Some devices cannot be queried, and queryable state does not
necessarily prove whether a physical operation completed. Future
device-specific options may still be supplied by error logic without changing
the Runtime interaction boundary.

## Consequences

Device- and operation-specific recovery remains close to the code that knows
what failed, while Runtime retains final authority over whether a submitted
choice is current and accepted. Adding a new error interaction does not require
a corresponding decision table in Cloud or Backend.
