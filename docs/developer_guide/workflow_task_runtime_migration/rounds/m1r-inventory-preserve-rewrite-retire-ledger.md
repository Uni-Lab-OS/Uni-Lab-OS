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

- Sole independent test-author base: `800fee7a678`; tests-only RED commit:
  `3d3b9f397` (preserved on this branch as `1fd9156`). The initial exact result
  was `6 failed`; after the retirement implementation the same suite reports
  `6 passed`.
- Canonical M1R targeted suites: `26 passed` before the final donor-test
  convergence; the final affected set reports `37 passed`.
- Full repository gate after convergence: `2214 passed, 4 skipped` in
  `97.30s`. The skips are the three opt-in networking process tests and the
  Phoenix executable integration test.
- Every changed Python file passes Ruff check and format. Changed production
  files pass `py_compile`; `git diff --check` is clean.
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
