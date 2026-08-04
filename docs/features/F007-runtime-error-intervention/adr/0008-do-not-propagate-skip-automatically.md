---
status: accepted
---

# Do not propagate skip automatically

When a skipped Job leaves a required downstream input unavailable, the Runtime
does not automatically skip the dependent Job, invent an empty output, or
abort the WorkflowTask. It surfaces the unavailable input as a new
user-resolvable error when evaluating the dependent Job. Unaffected branches
remain eligible to execute, while each affected Job requires an explicit user
choice rather than inheriting the upstream Skip Resolution.

## Consequences

Repeated user choices may move through an affected dependency chain one Job at
a time. Each choice remains separately auditable, and the Runtime never hides
the downstream consequence of a skip behind automatic state propagation.
