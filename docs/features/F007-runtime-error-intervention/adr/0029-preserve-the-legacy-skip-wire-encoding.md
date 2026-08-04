---
status: accepted
---

# Preserve the legacy skip wire encoding

OS Runtime records an accepted Skip Resolution as the canonical
WorkflowNodeJob state `skipped`, with no normal output. For the mandatory old
Backend delivery only, Legacy Adapter preserves the existing callback encoding
`status=success` plus `return_info.suc_type=skip`. It does not attempt to repair
the old Backend scheduler's incomplete native skipped convergence in this phase.

The adapter encoding is knowingly lossy and must not be read back as Runtime
truth. The canonical Core REST projection and uni-lab-fe expose `skipped`
directly after migration; the compatibility mapping and its Backend assumptions
are then removed.

## Consequences

Legacy success/failed scheduler paths remain untouched, reducing regression
risk in a frontend/backend stack being retired. Tests must still prove that OS
keeps `skipped`, creates no normal output, and emits the exact legacy
`suc_type=skip` marker rather than silently converting the domain outcome.
