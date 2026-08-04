# Uni-Lab Core Runtime Context

This context defines the authority and interaction boundaries shared by local
and cloud-hosted laboratory workflow execution.

## Language

**WorkflowTask Runtime**:
The durable domain that owns the lifecycle of one WorkflowTask, its Node Jobs,
execution attempts, incidents, reconciliations, and accepted resolutions.
_Avoid_: Edge DAG state, notification state, frontend runtime state

**Runtime Authority**:
The single WorkflowTask Runtime whose durable state is authoritative for one
execution and whose validation determines whether a requested resolution is
accepted.
_Avoid_: Cloud notification state, Backend forwarding state, UI state

**Interaction Adapter**:
A boundary that translates Runtime events for an existing user interface and
translates user intent back into Runtime commands without owning execution
state or deciding whether a command is safe.
_Avoid_: Runtime, scheduler, second state machine

**Legacy Interaction Adapter**:
An Interaction Adapter that preserves the existing Cloud notification,
Backend forwarding, and Edge messaging contracts during migration.
_Avoid_: Legacy Runtime, compatibility authority

**Resolution Submission**:
The recorded fact that an Interaction Adapter received and forwarded a user's
requested response to an Error Incident; it does not mean that the Runtime
accepted or executed that response.
_Avoid_: Resolution, processed incident, successful retry

**Accepted Resolution**:
An Error Incident response that the Runtime Authority validated and durably
accepted against the incident's current state.
_Avoid_: Submitted decision, notification acknowledgement, queued command

**Error Incident**:
The durable record of one exceptional condition owned by a WorkflowNodeJob
during input evaluation, admission, or an execution attempt that may require
an operator Resolution. It is represented by the existing WorkflowIntervention
persistence and public contract with kind `action_error`, not a parallel store.
_Avoid_: Exception class, notification, failed Job, device alarm

**Workflow Intervention**:
The existing durable OS record and REST/SSE contract for an operator-attention
wait. An Error Incident is its error-specific domain form and shares the same
identity, revision, options, decisions, and lifecycle.
_Avoid_: Error projection copy, Backend approval state, second Incident API

**Waiting Intervention**:
The existing Core control state in which a WorkflowTask has one or more Jobs in
`intervention_required`. It replaces the proposed synonymous waiting-decision
states in the first delivery.
_Avoid_: WAITING_DECISION, resolving Incident, Backend pending notification

**Incident Identity**:
The stable Runtime-issued identity of one Error Incident across Runtime state,
Interaction Adapters, user submissions, and acknowledgements.
_Avoid_: Notification UUID, Task-and-device lookup, Job UUID

**Incident Version**:
The monotonic Runtime-issued version of an Error Incident used to prove which
incident state a Resolution Submission was based on.
_Avoid_: Notification update time, Backend queue order, Job attempt

**Resolution Request Identity**:
The stable client-issued identity of one exact Resolution Submission, retained
unchanged across transport retries and bound permanently to its first request
content and Runtime result.
_Avoid_: Incident Identity, each delivery attempt, reusable request token

**Abort Intent**:
A user's request that a WorkflowTask admit no further execution and settle all
already-dispatched work safely; it does not retract a device command or prove
that physical execution stopped.
_Avoid_: Physical stop, command recall, immediate terminal state

**Physical Stop**:
Confirmed evidence that an already-dispatched physical action will no longer
continue or produce further real-world effects.
_Avoid_: Abort Intent, cancel request, disconnected device

**Skip Resolution**:
An accepted Job-scoped response that ends the current WorkflowNodeJob without
re-executing its failed action while leaving the WorkflowTask eligible to
evaluate other work.
_Avoid_: Successful Job, WorkflowTask abort, disabled WorkflowNode

**Input Incident**:
An Error Incident owned by a downstream WorkflowNodeJob whose required input
cannot be resolved before dispatch.
_Avoid_: Reopened upstream incident, propagated skip, device failure

**Incident Cause**:
One of zero or more direct links from an Error Incident to earlier Error
Incidents whose accepted outcomes created the current exceptional condition.
_Avoid_: Embedded ancestry, Backend notification link, mutable causal graph

**WorkflowTask Record Lifetime**:
The common retention lifetime of a WorkflowTask and its Incidents, Resolution
Submissions, accepted Resolutions, Attempts, and relevant Journal records. OS
does not hard-delete an individual member while retaining the WorkflowTask;
the aggregate is archived or removed as a unit.
_Avoid_: Backend notification retention, frontend cache lifetime

**Superseded Incident**:
An open Error Incident that no longer accepts a Job-scoped resolution because
the Runtime has accepted a WorkflowTask-wide Abort Intent. Supersession
preserves the original error and is not a successful resolution.
_Avoid_: Resolved Incident, deleted notification, implicit abort resolution

**Retry Attempt**:
A new execution Attempt created after the Runtime accepts a Retry Resolution.
It remains under the same WorkflowNodeJob and does not reopen or reuse the
failed Attempt or its Error Incident.
_Avoid_: New WorkflowNodeJob, reopened Incident, transport retry

**Execution Attempt**:
One immutable execution try within a WorkflowNodeJob. A Job may own several
Attempts across operator-approved retries while retaining one public Job identity.
_Avoid_: Job copy, mutable retry counter without history, Resolution delivery attempt

**Incident Resolution Option**:
One error-specific action offered for the current version of an Error Incident,
with user-facing meaning supplied by the reporting error logic. Options may
differ between errors even when their technical execution state is similar.
_Avoid_: Global retry policy, frontend-invented button, exception class mapping

**Standard Resolution Action**:
One of `retry`, `skip`, or `abort`, the only Resolution semantics certified for
the first delivery. The wire action identifier remains extensible text, but an
unknown identifier is not executable merely because an adapter can carry it.
_Avoid_: Closed wire enum, arbitrary Backend command, certified custom action

**Open-ended Decision Wait**:
The paused period in which an Error Incident remains open until an explicit
Resolution is accepted. Elapsed time alone does not choose `retry`, `skip`, or
`abort` and does not impersonate a user decision.
_Avoid_: 300-second auto-abort, timeout retry, expired notification

**Resolution Result Projection**:
The accepted or rejected Runtime result sent back through Backend to Cloud for
a previously pending Resolution Submission. It reflects Runtime authority but
does not replace Runtime's durable record.
_Avoid_: HTTP submission success, Redis enqueue acknowledgement, local UI state

**Incident Assignee**:
An authenticated user allowed to submit a Resolution for an Error Incident,
following the permission snapshot recorded by Runtime. In the first delivery
this is only the WorkflowTask Trigger Actor; future assignees may be derived
from laboratory-level permission configuration.
_Avoid_: Anyone who knows a notification UUID, notification recipient as authority

**WorkflowTask Trigger Actor**:
The stable authenticated user identity recorded when a WorkflowTask is created.
Legacy Backend supplies its existing WorkflowTask user; direct OS ingress uses
its authenticated principal.
_Avoid_: Workflow author, lab owner fallback, untrusted request-body user ID

**Online Resolution Delivery**:
A Legacy Adapter submission forwarded only while its target OS Edge session is
currently reachable. An offline or failed delivery is rejected and must be
resubmitted against fresh Incident state after reconnection.
_Avoid_: Delayed Redis command, indefinite pending submission, accepted offline abort

**Outdated Interaction Client**:
A legacy Cloud page that submits without Runtime Incident identity, revision,
option identity, or client request identity. Backend requires it to refresh
rather than synthesizing correctness-critical fields.
_Avoid_: Server-generated client request ID, task/device lookup, silent fallback

**Assignee Notification Projection**:
One user-specific Legacy Backend notification for a shared Error Incident. Many
assignees may receive separate notifications, but they reference one Runtime
Incident and converge on its single accepted outcome.
_Avoid_: Incident copy, independent decision, notification-owned lifecycle

**Minimized Incident Prompt**:
A persistent, non-dismissing Cloud indicator for an open interactive Incident
after its blocking prompt is minimized. It lets the operator inspect other UI
state without treating the Incident as handled or resuming the WorkflowTask.
_Avoid_: Closed modal, read notification, accepted Resolution

**Interactive Error Policy**:
An explicit declaration by error-handling logic that a matching failure should
open an Error Incident and wait for an operator Resolution. Absence of this
policy means the failure follows ordinary Job/Task failure handling.
_Avoid_: Every exception is interactive, frontend popup rule, implicit pause

**Simulated Interactive Action**:
A virtual-device Action run by a virtual Workflow to exercise the complete
Incident and Resolution path without laboratory hardware. It uses the real
Runtime and adapter boundaries but is not evidence that a physical device
recovery policy is safe.
_Avoid_: Fabricated Incident, mocked Runtime authority, hardware acceptance

**Physical Recovery Extension**:
Future real-device functionality for physical-state evidence, reconciliation,
safe-retry policy, and scheduled fallback Actions. It is not part of the first
interactive-Incident delivery.
_Avoid_: First-phase requirement, generic error popup, simulated safety proof

**Durable Runtime Event**:
A Runtime-owned event committed with the authoritative state change and exposed
through the canonical OS event cursor. Consumers may replay it or rehydrate the
current state without changing the authoritative result.
_Avoid_: Best-effort socket send, Backend-owned event truth, duplicate Resolution

**Invalidation Event**:
A narrow durable SSE notification that identifies which Runtime resource
changed. It prompts a client to rehydrate the latest authoritative representation
through REST rather than reconstructing state from rich event payloads.
_Avoid_: Full Incident snapshot, frontend event-sourced state, Legacy Notify payload
