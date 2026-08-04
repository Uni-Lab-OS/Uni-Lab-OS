---
status: accepted
---

# Let the first Runtime-accepted resolution win

Every Resolution Submission carries the Incident Version observed by its
caller. The Runtime Authority atomically accepts the first valid submission
whose expected version still matches and advances the incident version; any
later submission based on the previous version is rejected as stale and cannot
replace the accepted resolution. Ordering at Cloud, Backend, Redis, or the user
interface does not determine the winner.

## Consequences

Conflict responses project the current incident and accepted resolution so the
user interface can refresh and identify that another actor already handled the
incident. Interaction Adapters preserve the expected version but do not
evaluate it.
