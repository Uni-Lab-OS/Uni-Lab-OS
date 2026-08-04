---
status: accepted
---

# Retries create Attempts within one Job

An accepted `retry` creates a new immutable Execution Attempt under the existing
WorkflowNodeJob; it does not create another WorkflowNodeJob. The Job remains in
`intervention_required` while waiting, returns to execution after Runtime
acceptance, and ultimately reaches one terminal scheduler state. Every Attempt
retains its own outcome, timestamps, error, and linked Error Incident.

This preserves the current Core Workflow Intervention contract, whose decision
addresses one stable Job, while satisfying audit and crash-recovery requirements
that prohibit overwriting the failed execution try. Backend and frontends track
one Job UUID and may display its Attempt history.

## Consequences

Legacy Backend does not need to manufacture a new Job projection for each
operator retry. Attempt numbering and `error_policy.max_retries` are evaluated
by OS Runtime from durable Attempt records, not from repeated Job identities.
