# Migration decisions

Confirmed decisions are append-only during the migration. If one changes, add
a later decision that explicitly supersedes it.

## D-001: preserve the reviewed target

The branch `feat/edge-networking-and-scheduler@7bbfd38` is an immutable reviewed
baseline. Migration work starts from it on a new integration branch.

## D-002: migrate the union of tests

Preserve every target test and migrate every current-source unit test. Tests
move with their owning functional phase, before its implementation. Phase 09
must account for every source test as migrated, manually merged, or explicitly
superseded by a stronger Interface test.

## D-003: functional migration, not Git history synthesis

The repositories have no common Git ancestor. Do not merge or whole-branch
cherry-pick the source mirror. Classify source code as direct migration,
semantic migration, manual merge, or superseded. Record source commit and path.

## D-004: contract authority

User-confirmed migration decisions are authoritative. Unchanged contract
details follow Backend review documents 10-12. Supplemental protocols require
explicit adoption. Existing target and source code are implementation evidence,
not contract authority.

## D-005: mirror Backend identity and naming

Use Backend model types, singular snake-case tables, `uuid` entity primary keys,
Backend relationship fields, Backend path variables, and the same identity
names in local variables. Do not retain `run_id`, old camel-case wire aliases,
or `/api/v1/runtime/runs`. Do not maintain a compatibility adapter for the old
Run vocabulary.

## D-006: stage branches and reviewable history

Completed phases accumulate on `integration/workflow-task-runtime`. Each phase
uses `migration/NN-*`, keeps reviewable functional commits, and merges only
after the phase gate. Do not squash migration provenance. Do not push without
explicit authorization.

## D-007: bounded baseline debt

Phase-target tests must be green. Existing collection blockers are registered
debt with an owning phase and cannot become permanent skips/xfails. Phase 09
requires a fully collectable and passing suite.

## D-008: temporary and permanent documentation

Migration decisions, manifests, and progress live temporarily under
`docs/developer_guide/workflow_task_runtime_migration/`. Permanent invariants go
to `AGENTS.md`; maintained module behavior goes to each module README. Remove
the temporary migration directory after phase 09 has distilled lasting facts.

## D-009: phase 00 scope

Phase 00 may create branches, temporary ledgers, baseline evidence, and
permanent `AGENTS.md` invariants. It must not change production code, tests,
frontend code, Backend code, or database schemas, and it must not repair known
baseline failures.
