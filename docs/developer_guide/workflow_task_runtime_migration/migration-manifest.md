# Functional migration manifest

This is a phase-level inventory, not a promise to copy paths byte-for-byte.
Exact destination paths are confirmed only when the owning phase is entered.

| Capability/source | Source commit | Mode | Owning phase | Initial disposition |
|---|---|---|---:|---|
| Backend Workflow/Task frontend Interface | `09609a2` (frozen, read-only) | semantic | 01 | mirror frozen frontend paths/DTOs/behavior; documents 10-12 are historical only |
| `unilabos/workflow` canonical model | `4ec146f` | direct/semantic | 02 | migrate immutable revision and validation |
| Python AST authoring and generation | `4ec146f` | direct/semantic | 02 | preserve no-exec construction and source maps |
| Atomic graph storage | `4ec146f` | semantic | 02 | align with Backend Workflow/Node/Edge models |
| Source `DagWalk` control semantics | `4ec146f` | semantic | 03 | retain useful traversal only; use derived Edge resolution, persistent `disabled`, start frontier, and explicit published Conditional Join—no Task skip or implicit Fork/Join |
| Target `WorkflowRun` | `7bbfd38` | manual-merge | 03 | deepen in place; replace mutable pending-parent authority |
| Source ready/resource policies | `4ec146f` | semantic | 03/07 | select only policies not already stronger in target |
| Source Runtime journal | `4ec146f` | direct/semantic | 04 | adapt to Backend WorkflowTask/WorkflowNodeJob identity |
| Source feedback/result projection | `4ec146f` | semantic | 04 | Node-centric durable events |
| Source cancel/unknown/reconcile | `4ec146f`, `a80314f` | semantic | 04 | preserve OS-private physical uncertainty fences while exposing only frozen frontend Task/Job behavior |
| Source `DebugController` | `a80314f` | semantic | 05 | task-scoped admission gate before resources/dispatch |
| Source debugger frontend Interface | `a80314f` | semantic | 05/08 | rename completely to WorkflowTask contract |
| Target ordering and duration estimation | `7bbfd38` | keep/deepen | 03/06 | retain reviewed policy implementations |
| Target HostLink | `7bbfd38` | keep/deepen | 06 | device execution transport, not workflow authority |
| Target action error policy | `7bbfd38` | keep/deepen | 06 | integrate with durable node attempts/decisions |
| Target device-state projection | `7bbfd38` | keep/deepen | 06 | projection only |
| Target Inventory command/ledger/outbox | `7bbfd38` | keep/deepen | 07 | expose only after authority decision |
| Source ResourceTree material projection | `4ec146f` | semantic | 07 | evidence for live-state authority decision |
| Source local bridge | `4ec146f`, `a80314f` | semantic | 01/02/04/05 | frontend-facing adapter only; do not retain Run routes, old field names, or Backend-to-Edge parity |
| Frontend authoring/runtime/debugger | `2efb442` | semantic | 08 | switch directly to WorkflowTask Interface |
| Cloud/frontend authority adapter | frozen frontend Interface | semantic | 08 | switch the frontend's selected authority only; never proxy or reproduce `/api/v1/edge/*` |
| Source mutable `EdgeState` | `4ec146f` | superseded | 03 | replace with pure derived edge resolution |
| Target runtime `/api/v1/workflows` routes | `7bbfd38` | superseded | 01/08 | `/workflows` is static definition only |
| Target `WorkflowHistoryStore` as parallel truth | `7bbfd38` | superseded | 04 | replace with authoritative event journal/projection |
| Target in-memory `MonitorBus` as replay source | `7bbfd38` | superseded | 04 | transport may project durable events only |
| Old source `/api/v1/runtime/runs` | `4ec146f`, `a80314f` | superseded | 01/08 | no compatibility adapter |

Every row must be resolved by phase 09. `superseded` requires replacement tests
at the maintained Interface.
