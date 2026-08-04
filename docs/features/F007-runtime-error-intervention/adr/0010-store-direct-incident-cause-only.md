---
status: accepted
---

# Store direct Incident causes rather than a causal graph

An Error Incident may store zero or more `caused_by_incident_ids` referencing
earlier Incidents in the same WorkflowTask, together with stable cause and
missing-input details. A downstream Input Incident aggregates all required
inputs found unavailable during one evaluation instead of opening one Incident
per binding. Referenced Incidents remain immutable and are not reopened.
Runtime stores only these direct edges; it does not duplicate ancestor lists
or introduce a general-purpose causal graph into Cloud or Backend.

The referenced Incidents, their Resolution records, execution Attempts, and
relevant Journal records share the WorkflowTask record lifetime. OS does not
hard-delete one of these records while retaining the WorkflowTask; archival or
deletion applies to the aggregate as a unit. Backend notification projections
may use an independent retention policy because they are not causal evidence.

## Consequences

OS can reconstruct a user-facing causal tree by following direct references
through durable Incident records and the immutable WorkflowTask snapshot. The
projection may enrich each link with its owning Job, Node, attempt, accepted
resolution, missing input, and timestamp. Traversal rejects cycles and uses a
bounded depth; Interaction Adapters receive the resulting explanation and do
not reconstruct or own the causal tree themselves. Retaining the aggregate as
a unit prevents a surviving Incident from pointing to an already-deleted
cause.
