# Phase 00 baseline

## Immutable anchors

| Role | Repository | Branch/commit |
|---|---|---|
| Migration target | `/home/gaojing/Uni-Lab-OS` | `feat/edge-networking-and-scheduler@7bbfd38` |
| Capability source | `/home/changjunhan/Uni-Lab-Core/Uni-Lab-OS` | `codex/private-github-snapshot-20260725@a80314f` |
| Source feature history | same repository | `4ec146f`, `a80314f`; based on grafted snapshot `0812ec9` |
| Frontend evidence | `/home/changjunhan/Uni-Lab-Core/uni-lab-fe` | `kernel-workbench-20260720@2efb442` |
| Backend implementation | `/home/xiongyanfei/uni-lab-backend-github` | local reviewed implementation |
| Backend review documents | `/home/xiongyanfei/tangshaodong/backend-api-review` | documents 01-12 |

The target and capability-source repositories have no Git commit in common.
The target remote is `deepmodeling/Uni-Lab-OS`; the source mirror remote is
`Uni-Lab-OS/Uni-Lab-OS`. Migration therefore uses reviewed functional commits,
not merge or whole-branch cherry-pick.

The source repository has pre-existing untracked `docs/test-cases/`. It belongs
to the source workspace owner and is not a migration input.

## Backend naming facts

The reviewed Backend uses GORM v1.31.1 for queries and explicit
`golang-migrate` SQL for schemas. Workflow models implement explicit singular
snake-case table names:

| Model | Table |
|---|---|
| `Workflow` | `workflow` |
| `WorkflowNode` | `workflow_node` |
| `WorkflowEdge` | `workflow_edge` |
| `WorkflowTask` | `workflow_task` |
| `WorkflowNodeJob` | `workflow_node_job` |
| `WorkflowNodeTemplate` | `workflow_node_template` |
| `WorkflowHandleTemplate` | `workflow_handle_template` |

Entity JSON primary keys are `uuid`. Relationships use `workflow_uuid`,
`workflow_task_uuid`, `workflow_node_uuid`, `source_node_uuid`, and
`target_node_uuid`. HTTP path variables use `workflow_uuid`, `task_uuid`,
`node_uuid`, `edge_uuid`, and `job_uuid`.

## Required interpreter

All Python evidence below was produced with:

```text
/home/changjunhan/.micromamba/envs/unilab/bin/python
Python 3.11.14
```

## Target baseline

Command:

```bash
/home/changjunhan/.micromamba/envs/unilab/bin/python -m pytest -q \
  tests/app \
  tests/hostlink \
  tests/networking \
  tests/resources/test_tracker_state_promotion.py \
  tests/test_action_policy.py
```

Result:

```text
320 passed, 3 skipped, 5 warnings in 23.38s
```

Full collection command:

```bash
/home/changjunhan/.micromamba/envs/unilab/bin/python \
  -m pytest --collect-only -q tests
```

Result:

```text
372 tests collected, 1 collection error
ModuleNotFoundError: No module named 'unilabos.registry.community_alias'
```

The collection error predates this migration. It is assigned to phase 02 and
must not become a permanent skip or xfail.

## Capability-source baseline

Scheduler:

```text
52 passed in 1.42s
```

Runtime:

```text
90 passed, 1 warning in 1.31s
```

App tests excluding the single authoring test that imports the missing
external fixture:

```text
106 passed, 1 warning in 3.91s
```

Full Workflow and App collection is blocked by:

```text
/home/changjunhan/Uni-Lab-Core/Uni-Lab-Templates/
packages/ptlc_station/ptlc_station/workflows/develop_prepare.py
```

The fixture is not present. Phase 02 must make the migrated test fixture
self-contained rather than preserving an absolute host path.

## Frontend E2E baseline

The frontend has four Playwright specifications:

- `e2e/workflow-runtime.spec.ts`
- `e2e/workflow-debug-scenarios.spec.ts`
- `e2e/workflow-debug-actions.spec.ts`
- `e2e/material-scene.spec.ts`

`workflow-debug-actions.spec.ts` starts an isolated loopback local bridge and is
reproducible without an external OS:

```bash
pnpm test:e2e:workflow-actions
```

Result:

```text
4 passed in 30.2s
```

The other workflow and material specifications require a real local OS URL and
are recorded for their owning phases. E2E results must observe real HTTP/WS and
OS projections; page-level API success mocks are not valid evidence.

## Baseline debt policy

- A phase's migrated and newly added tests must have zero failure and zero
  collection error.
- A phase may not add failures, skips, or xfails to the global baseline.
- Existing collection blockers are temporary debt owned by a named phase.
- Phase 09 requires complete collection and a green unit/E2E suite with no
  unexplained baseline exception.
