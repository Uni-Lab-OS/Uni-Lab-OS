# Uni-Lab OS Workflow Context

This context distinguishes editable workflow definitions from their transient
authoring states and immutable execution records.

## Language

**Workflow**:
A persisted, editable static definition of laboratory intent and its control
graph.
_Avoid_: Run, execution

**Authoring Source**:
A human-editable representation of a Workflow whose logical nodes retain stable
identity across ordinary edits.
_Avoid_: Standalone script, execution script

**Authoring Draft**:
The current authoring text, which may already be written to a draft file but
may still be incomplete, invalid, or unapplied to the persisted Workflow.
_Avoid_: Workflow, applied source

**Applied Authoring Source**:
The latest successfully compiled OS source artifact associated with a specific
persisted Workflow Revision.
_Avoid_: Current draft, WorkflowTask snapshot

**Stale Authoring Source**:
An Applied Authoring Source whose recorded revision no longer matches the
current persisted Workflow Revision.
_Avoid_: Invalid draft, current Workflow

**Candidate Workflow**:
The latest valid interpretation of an Authoring Draft, available for review but
not yet persisted.
_Avoid_: Persisted Workflow, running workflow

**Authoring Compilation**:
The interpretation of an Authoring Draft as a Candidate Workflow with
diagnostics.
_Avoid_: Execution, WorkflowTask creation

**Graph Authority**:
The selected system whose persisted Workflow graph and template identities are
authoritative for an authoring operation.
_Avoid_: Execution target, compiler

**Task Authority**:
The one selected system that receives a WorkflowTask creation request and owns
the referenced Workflow, immutable Task snapshot, execution records, and all
Task-input Material resolution for that execution.
_Avoid_: Driver, independently selected Material service, remote fallback

**Template Catalog Snapshot**:
The authority-specific WorkflowNodeTemplate and WorkflowHandleTemplate
identities and contracts, identified by the fingerprint observed during
Authoring Compilation.
_Avoid_: Driver importability, capability list

**Action Contract**:
The ordered typed inputs derived from an Action's function parameters and the
ordered named outputs derived from its `TypedDict`, frozen dataclass, or
compatibility inline-dict return declaration, normalized before catalog
projection.
_Avoid_: Runtime example inference, parameter-name heuristics, opaque dict
field guessing

**Action Result Record**:
A fixed-shape successful Action result whose declared fields are all present;
nullable fields carry explicit `None`. `TypedDict` and frozen dataclass are the
first-class Python forms.
_Avoid_: Optional missing result key, positional tuple protocol, untyped dict

**Material Authority**:
For Workflow execution, the Material-owning part of the selected Task
Authority. It authoritatively resolves a Material UUID, its ResourceTemplate
identity, deletion state, availability, and current resource tree without
cross-authority fallback.
_Avoid_: Independently selected service, Template Catalog, frontend selector
cache, remote lookup

**Material Module**:
The OS-local durable module that owns Material identity, the last confirmed
tree/content and availability, reservations, versions, audit, and conversion
to and from the runtime projection.
_Avoid_: Workflow store, frontend query cache, Inventory-only facade

**Runtime Material Projection**:
The controlled `ResourceTreeSet`/PLR in-memory form used while resolving and
executing device actions, rebuilt and committed only through the Material
Module's owned synchronization points.
_Avoid_: Durable Material authority, second writable store, graph-file truth

**Material UUID**:
The sole stable `Material.uuid`; referred to as `material_uuid` by every
relationship, Workflow field, Inventory state row, and runtime operation.
_Avoid_: Edge UUID, cloud alias, instance UUID, graph-derived UUID

**Material Barcode**:
The optional laboratory scan identifier named `barcode` in both OS and the
Backend Material contract. An empty string means that no barcode has been
assigned; non-empty values are case-insensitively unique among non-deleted
Materials.
_Avoid_: `code`, second barcode field, UUID, value hidden in config/data

**Material Disposition**:
The durable business lifecycle value `active`, `consumed`, `discarded`,
`quarantined`, or `reconciling`. It is separate from scheduling ownership.
_Avoid_: Reserved, in use, device online state, arbitrary MaterialState status

**Material Composition**:
The parent/child relationship stating that one Material is structurally part
of another Material, represented by the child's `parent_uuid`.
_Avoid_: Site occupancy, laboratory layout, scheduler reservation

**Site**:
A stable, named position owned by one Material, with an optional Material
occupant and an allowlist describing which ResourceTemplates may occupy it.
_Avoid_: Material child, PLR resource name, laboratory zone, scheduler lock

**Site Occupancy**:
The placement relationship from one Site to at most one occupying Material;
one Material may occupy at most one Site at a time.
_Avoid_: Material Composition, ResourceTreeSet identity, 2D lab placement

**Task Material Reservation**:
A durable WorkflowTask-lifetime claim on accepted business Materials or
inventory quantities. Acquisition is all-or-none; transient contention leaves
the already-created Task pending and is retried by the coordinator.
_Avoid_: Job lock, Site occupancy, device selection

**Job Execution Claim**:
The durable, atomic, Job-attempt-owned right to use one selected device and to
mutate specified Materials or Site occupancies during physical execution.
_Avoid_: Task Material Reservation, process mutex, device-action queue key

**Fenced Execution Claim**:
A Job Execution Claim deliberately retained after an unknown physical result
until reconciliation establishes and commits observed reality.
_Avoid_: Released lock, failed Task, stale in-memory busy flag

**WorkflowNode Identity**:
The stable identity of one logical Workflow node across source, graph, and
debugging views.
_Avoid_: Line number, list index, display name

**Workflow Revision**:
The monotonic version of a persisted Workflow graph used to identify the graph
state observed by an editor.
_Avoid_: Entity update time, WorkflowTask version

**WorkflowTask**:
One execution created from a snapshot of a Workflow.
_Avoid_: Workflow, Run

**WorkflowTask Snapshot**:
The immutable Workflow definition observed when a WorkflowTask is created.
_Avoid_: Live Workflow, editor state

**Disabled WorkflowNode**:
A WorkflowNode intentionally excluded from execution by its editable Workflow definition until it is re-enabled.
_Avoid_: Task skip, out-of-scope Node, branch skip

**Out-of-scope WorkflowNode**:
A WorkflowNode excluded from one debug execution because it belongs to neither that execution's start frontier nor any frontier Node's reachable descendants.
_Avoid_: Disabled WorkflowNode, skipped execution

**Skipped WorkflowNode Execution**:
A WorkflowTask Node execution that does not run because runtime control flow makes its path inactive.
_Avoid_: Disabled WorkflowNode, out-of-scope Node

**Debug Start Frontier**:
The one or more WorkflowNodes selected as the entry set of a debug WorkflowTask.
_Avoid_: Single start Node, Workflow roots, topological cutoff

**Debug Launch Configuration**:
The proposed Debug Start Frontier and breakpoint Nodes selected before creating a debug WorkflowTask; it does not select individual Nodes to skip.
_Avoid_: Workflow definition, runtime state

**WorkflowTask Debug Configuration**:
The Task-scoped debug scope and source mapping captured when a debug
WorkflowTask is created.
_Avoid_: Workflow metadata, editor markers

**Breakpoint Hold**:
A Task-scoped admission hold on one breakpoint Node while unrelated executable Nodes may continue.
_Avoid_: Frozen branch, global pause, Edge state

**Converging WorkflowNode**:
A WorkflowNode whose multiple active predecessors must complete before it can execute.
_Avoid_: Synthetic Join Node, source-only parallel boundary

**Exclusive Condition**:
An ordered set of alternatives in which the first satisfied alternative
selects the active path, with an optional fallback when none is satisfied.
_Avoid_: Independent multicast selection, Python runtime execution

**Branch-local Workflow Value**:
A Workflow value available only within the exclusive alternative that
produces it and not at the alternatives' convergence.
_Avoid_: Merged value, Workflow parameter

**Conditional Join**:
The control point that closes one Exclusive Condition after its active
alternative finishes and before common continuation begins.
_Avoid_: Data merge, parallel Fork/Join, branch result

**ResourceSlot**:
A typed Workflow value representing one complete business material whose
identity and meaning remain unchanged across Workflow input, Node data flow,
and inserted subworkflow boundaries.
_Avoid_: MaterialRef, generic UUID, executor device binding

**ResourceSlot Collection**:
An ordered collection of independent ResourceSlot roots, each retaining its
own nested material tree.
_Avoid_: ResourceSlotList, one flattened material tree

**Workflow Input Contract**:
The ordered public values that a caller may or must supply for one Workflow
execution.
_Avoid_: Node parameters, arbitrary Task metadata

**Workflow Input Binding**:
A graph-semantic link from one Workflow Input Contract parameter to one real
target WorkflowHandleTemplate UUID on a consuming WorkflowNode.
_Avoid_: Handle display name, runtime placeholder, incoming WorkflowEdge

**Workflow Output Contract**:
The ordered named values that a Workflow promises to produce for its caller or
root Task.
_Avoid_: Job feedback, arbitrary return data

**Workflow Output Binding**:
A graph-semantic link from one Workflow Output Contract name to the persisted
producer value that a subworkflow call or root WorkflowTask resolves.
_Avoid_: Recompiling the live Draft at Task completion, arbitrary Job result

**Implicit Resource Pass-through**:
The same-name ResourceSlot output guaranteed for a Workflow or action material
input when no compatible explicit output replaces it.
_Avoid_: Hidden action return, copied Material

**Executor Material**:
The Material selected by a WorkflowNode to identify the device that executes
its action.
_Avoid_: ResourceSlot, sample input, processed material
