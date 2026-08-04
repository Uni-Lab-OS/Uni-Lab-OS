---
status: accepted
---

# Let the downstream Job own an unavailable-input incident

When Runtime evaluation finds that a downstream Job lacks a required input
because an upstream Job was skipped, it creates a new Input Incident owned by
the downstream Job. The upstream Job remains `skipped` and its accepted
resolution remains immutable. The new incident has its own Incident Identity,
version, options, submissions, and acknowledgement lifecycle.

## Consequences

The user resolves the condition in the context of the Job that cannot proceed,
for example by skipping that Job, supplying a permitted substitute, or
aborting the WorkflowTask. Runtime may retain a direct reference to the
upstream incident for explanation and audit, but never reopens it to revise the
already accepted Skip Resolution.
