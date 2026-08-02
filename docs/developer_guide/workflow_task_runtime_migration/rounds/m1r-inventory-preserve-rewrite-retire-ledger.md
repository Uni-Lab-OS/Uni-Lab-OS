# M1R Inventory preserve / rewrite / retire ledger

This ledger records the reviewed donor provenance used by OS #14. The source
blob is retained by Git; the final implementation does not ship a compatibility
facade, legacy tables, or a second Material identity.

## Production provenance

| Source (pre-convergence blob) | Disposition | Final target and invariant |
|---|---|---|
| `inventory/store.py@0af1a2721c0e9eb743896373b9d50127292dd8e2` | preserve + rewrite | `inventory/store.py`: RLock, WAL, foreign keys, busy timeout, `BEGIN IMMEDIATE`, rollback and durable outbox helpers remain; schema is exact v4 and old/mixed databases fail closed. |
| `inventory/service.py@f7a0662ed4e0edc0e0b87ff0ddc196d1145a536c` | preserve + rewrite | `InventoryService`: lot quantity/FIFO ordering, audited adjustment, ledger/outbox atomicity and reopen behavior remain behind the public service; Material/Site/Task Reservation UUIDs replace the old public identity and node-scoped allocator. |
| `inventory/commands.py@4b458ed01b384faaa51c421cb2baf80fa0ae7d52` | rewrite | Only closed, versioned Task-wide `material.admit` and `material.release` commands remain. Their idempotency/result persistence is owned by `InventoryService`. |
| `inventory/sync.py@7ce7bddc1d2d05af96dfce51b7b4e5b3bea520a8` | preserve + rewrite | Ordered replay, partial ACK, durable cursor, retry and snapshot recovery remain, but consume `InventoryService` instead of Store rows. |
| `inventory/layout.py@351277384bd5acc081dc9a7d918d1d8966e3a666` | preserve + rewrite | Lab profile/zone/visual-placement CRUD remains. Visual placement is explicitly independent of Site occupancy; assembly follows canonical `Material.parent_uuid`. |
| `inventory/warehouse.py@a2f17ac6572498774e1397ddd8611ef8633fe891` | preserve + rewrite | Lot/zone aggregation remains and groups by Registry-owned `resource_template_uuid`; no mutable template copy or second instance identity remains. |
| `app/scheduler/service.py@b7f1c15a513771c412adb8e8eb789c1992e708e3` | preserve + rewrite | `EdgeScheduler` remains the sole coordinator and `dag_state.py` the sole DAG engine. WorkflowTask admission/release/replay stays; legacy WorkflowSpec material inference now fails closed before dispatch. |
| `resources/authority/__init__.py@db56b8b914cea31ab651d756671c92201c57c22a` | migrate + retire | Material/Site records and behavior moved to `app.scheduler.inventory.domain/service`; the package and facade are removed. |
| `resources/authority/models.py@7b629d631fbb1221f779f4e66e65fa1966bbe1bb` | migrate + retire | Backend-aligned records/errors moved to the canonical Inventory package; borrowed Workflow UoW contracts are removed. |
| `resources/authority/sqlite.py@515c6f8fde7867ac9bc46f3c82ec75c5d9b04551` | migrate + retire | Exact fields, constraints and ResourceSlot behavior moved into `inventory.db`; shared-connection and runtime-authority adapter paths are removed. |

## Test provenance and replacements

| Source test blob | Disposition | Replacement evidence |
|---|---|---|
| `tests/app/test_inventory_store_service.py@57918de50bba776203104f620e9935c0bbe60742` | rewrite | Same path now covers public lot invariants/FIFO order, audited versioned adjustment, atomic failure, casefold barcode, Material parent composition and reopen. Task contention/idempotency moved to the M1R admission/release suites. |
| `tests/app/test_inventory_sync.py@0bda2a1cc2bfcb24e7b76a2b316636ffa01eb6c9` | rewrite | Same path now covers failed send retention, partial ACK suffix replay, snapshot/event convergence and cursor reopen through `InventoryService`. |
| `tests/app/test_inventory_commands.py@12175fa8f5efb33e5bd8f4177152bdefdaa507b0` | rewrite | Same path now covers closed admission/release replay plus private HTTP/WS projection. Mutable-template, second-identity and node-scoped commands are retired. |
| `tests/app/test_inventory_scheduler_link.py@f718603feb23ea596950dedc6ae4e9055d7bd359` | rewrite | Same path freezes fail-closed legacy material input, unchanged plain DAG dispatch and missing-authority dispatch denial. Full W1/W2 behavior lives in `test_m1r_scheduler_*`. |
| `tests/app/test_lab_layout.py@4e3d3b98df06b59127ad058466392e536fce34a6` | rewrite | Same path covers profile, zones, visual placement isolation, canonical parent assembly, lot-derived storage and Registry-UUID warehouse aggregation. |
| `tests/app/test_edge_monitor.py@3b9985b8ffb5c225937da5948f29cb805443777d` | partial rewrite | Material monitor tests now open the public service and use registered template UUIDs; non-Inventory monitoring tests are unchanged. |
| `tests/app/test_material_lock_and_error_decision.py@97acb8122aea441ed5a5bb548f7733fec72c4d0c` | partial rewrite | Explicit `@action(lock_resource=...)` coverage remains; implicit old Material locks/quarantine are replaced by fail-closed WorkflowTask admission evidence. |
| `tests/resources/authority/test_material_module_v1.py@816afef686e47c47997b7ae66b273cd6383762b5` | replace + retire | `test_m1r_inventory_service_material.py`, `..._site.py`, `..._resource_slot.py`, v4 schema tests, and rewritten donor tests. |
| `tests/resources/authority/test_material_module_review_fixes.py@de5b946cf7010cd4955d04c706311426d6b28a3f` | replace + retire | Material/Site validation and casefold/conflict tests in the canonical public-service suites. |
| `tests/resources/authority/test_material_module_list_sites.py@71f9c6791537197770ffda7f4ce850c4d8d03076` | replace + retire | `test_m1r_inventory_service_site.py` covers normalized allowlist and stable active Site order. |
| `tests/resources/authority/test_resource_slot_resolver_v1.py@30c2b04d5f40c0d484be15262225e4952be5b4c9` | replace + retire | `test_m1r_inventory_service_resource_slot.py` and M2A production-composition tests cover 400/404/409 and template compatibility. |
| `tests/resources/authority/test_task_material_reservation_v1.py@794f7a242e8dc1a6c7895fdaa50864d294bab132` | replace + retire | Admission, blocked retry, contention, release, replay seam, dispatch proof and W1/W2 crash-window suites cover the independent-DB saga. |

## Explicit retirements

- Mutable ResourceTemplate rows belong to Registry/Package Catalog and are not
  copied into `inventory.db`.
- Alternate edge/cloud/instance identities collapse to canonical Material UUID.
- The old relation table is replaced by `Material.parent_uuid` composition and
  Site occupancy; 2D lab placement remains a separate visual projection.
- The old workflow/node/attempt reservation API is replaced by Task-wide durable
  commands/results. Claim/fencing and complete stock-selector policy remain M1EF
  and M2B work, respectively.
- Old database upgrade-in-place tests are replaced by exact-schema creation and
  legacy/mixed database fail-closed tests because the user explicitly abandoned
  legacy data.
- Content state remains an internal canonical table until the #146 protocol;
  no replacement public mutation contract is invented in M1R.

## Verification evidence

- Frozen spec/test base: `a243e54690e0ecd6477a128bfd5acdb88645383f`.
  The sole independent test-author's tests-only RED is the first delivery
  commit, `c0883c0`; its exact pre-production run reported `6 failed`. The same
  suite reports `6 passed` after convergence. The earlier branch that placed
  the identical test blob late remains recoverable but is not the final
  candidate history.
- Latest review-fix implementation tree: `068ecdf`. The M1R owner/waiter,
  stale-terminal and UUID-alias suites report `40 passed`; the adjacent
  M2A/R1B/persistent-authoring suites report
  `323 passed`.
- Full repository gate after the fifth exact-SHA review fix: `2229 passed, 4
  skipped` in `98.38s`. The skips are the three opt-in networking process tests
  and the Phoenix executable integration test.
- The full gate exposed a pre-existing timing race in the round-9 SSE cursor
  ASGI harness: its fixed 10 ms disconnect could win before the first
  `list_events` call under suite load. Test-only commit `57e8774` replaces the
  timer with an observable first-read handshake while retaining the outer
  one-second timeout; no SSE production behavior changed.
- The 28 M1R-owned modified Python files pass Ruff check and format; the three
  broad pre-existing Workflow files have no net-new Ruff debt against the
  frozen base (`runtime 37→37`, `service 150→149`, `store 97→97`). Changed
  production files pass `py_compile`; `git diff --check` is clean.
- Static production scans report no `unilabos.resources.authority`,
  `MaterialModule`, `InventoryModule`, borrowed Workflow UoW, retired
  WorkflowSpec allocation calls, configurable `edge_inventory_db`, or
  `ULAB_INVENTORY_DB` path.
- The exact-v4 schema gate verifies the Backend-aligned `material` and `site`
  columns, normalized `site_allowed_resource_template`, Task-wide reservation
  tables, no cross-database foreign key, fixed workspace `inventory.db`, WAL,
  reopen, and legacy/mixed database rejection.

The final candidate SHA and exact-SHA Standards/Spec review are recorded only
after the candidate commits are immutable; no intermediate checkpoint is an
integration merge candidate.

## Exact-SHA review disposition

The sole reviewer checked the first immutable candidate
`1b5d59e44c7eadcacfbce93d8b0cc81763ff3fe6` on both axes. Standards found one
blocking provenance-order issue; Spec found five blockers: duplicate production
Inventory composition, a shared Scheduler/cloud outbox cursor with no saga
serialization, ignored candidate Site filters, non-durable deterministic
admission rejection, and incomplete exact-v4 reopen validation.

The final history moves the unchanged independent RED before all production
changes. Review-fix commits `6e9c799` and `71a2637` close the first five behavior
findings with production composition/startup/Task-ingress wiring, separate
Scheduler and cloud cursors, command serialization, candidate Site
revalidation, durable `rejected` results, and exact SQLite DDL-object checking.

The same reviewer then checked `01579cf3617e4834577abcbd7be569a9d2f9058f`.
Standards passed with `0B / 0NB`; Spec found four remaining blockers: no
production terminal-release trigger/recovery, transient Site occupancy escaping
instead of a durable `blocked` result, a conflicting Workflow projection on
`blocked → admitted`, and a Scheduler mutex held across SQLite calls.

Commits `8f2955f`, `1891c3f`, and `0c6e890` close those four findings. The
existing runtime worker now triggers and retries Task Material reconciliation;
startup replays terminal releases; transient Site occupancy is durable
`blocked`; the same command may advance only monotonically from `blocked` to
`admitted`; and a per-Task logical saga slot releases its Condition mutex before
any Inventory/Workflow durable operation.

The same reviewer then checked `621ffe68bca9c586b31d806d20c83bd8036e06dd`.
Standards passed with `0B / 0NB`; Spec found one remaining blocker: startup
admitted pending Tasks before releasing terminal owners, and online terminal
release did not retry another Task whose durable `blocked` result was caused by
that Reservation. This could strand a waiter after the contention disappeared.

The tests-only review regression `00a0e85` freezes both startup and live
owner/waiter cases. Implementation `3db90ef` makes startup reconcile terminal
releases before pending admissions and makes the production terminal callback
retry pending admissions only after release projection and acknowledgement.

The same reviewer then checked `ef0760fa18c09c71f236709c4029b9ca05df9695`.
Standards passed with `0B / 0NB`; Spec found one remaining release-before-admit
race. A pending scan could retain a stale Task row, terminal reconciliation
could persist a stable empty release, and the stale scan could then admit a
Reservation whose Workflow projection conflicts. Replaying the already
processed empty release could not clean that late Reservation.

The tests-only review regression `7a4ecc5` freezes both the direct terminal
admission guard and stale pending-page routing. Implementation `ab1f89b` makes
every pending scan item re-enter latest-state reconciliation, and rechecks Task
terminal state inside the same per-Task saga slot before any direct Inventory
admission. Therefore release-first observes terminal state and cannot be
followed by a late admission; admission-first retains the slot until its
Inventory/Workflow attempt ends, after which terminal reconciliation releases
the current Reservation.

The same reviewer then checked `1a43de78667bdd2ead6d0740179918ae7b576e64`.
Standards passed with `0B / 0NB`; Spec found one equivalent-identifier bypass:
uppercase and lowercase spellings resolved to one WorkflowTask but occupied
different raw Scheduler saga keys, allowing the empty-release/late-admission
race to reappear.

The tests-only review regression `21855ea` freezes that concurrent alias
interleaving. Implementation `068ecdf` canonicalizes the saga key through UUID
parsing before entering the Condition, so every equivalent Task UUID spelling
shares one logical slot without changing the persisted identity or command
payload.

The same reviewer must re-review the new final immutable SHA before acceptance.
