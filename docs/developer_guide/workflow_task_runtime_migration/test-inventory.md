# Test migration inventory

Inventory commands:

```bash
cd /home/changjunhan/Uni-Lab-Core/Uni-Lab-OS
rg --files tests | sort

cd /home/gaojing/Uni-Lab-OS
rg --files tests | sort
```

Counts at phase 00:

| Category | Count | Disposition |
|---|---:|---|
| Source test files/assets | 121 | all must be accounted |
| Target test files/assets | 75 | all preserved |
| Common paths | 48 | 43 identical; 5 require manual merge |
| Source-only paths | 73 | migrate with owning phase |
| Target-only paths | 27 | preserve through all phases |

The 43 byte-identical common paths are already present and require no copy.
Their membership is reproducible with `comm` and `cmp` from the commands above.

## Common paths with different content

All five require manual merge in phase 02:

```text
tests/registry/test_community_alias.py
tests/registry/test_external_package_full_chain.py
tests/registry/test_external_registry_discovery.py
tests/registry/test_initializer.py
tests/registry/test_registry_setup_external_paths.py
```

## Target-only paths to preserve

```text
tests/app/test_device_state.py
tests/app/test_edge_monitor.py
tests/app/test_edge_scheduler_api.py
tests/app/test_edge_scheduler_backend.py
tests/app/test_edge_scheduler_cloud_api.py
tests/app/test_edge_scheduler_dag.py
tests/app/test_edge_scheduler_estimation.py
tests/app/test_edge_scheduler_param_resolver.py
tests/app/test_edge_scheduler_service.py
tests/app/test_inventory_commands.py
tests/app/test_inventory_scheduler_link.py
tests/app/test_inventory_store_service.py
tests/app/test_inventory_sync.py
tests/app/test_lab_layout.py
tests/app/test_material_lock_and_error_decision.py
tests/app/test_workflow_history.py
tests/hostlink/__init__.py
tests/hostlink/test_doctor.py
tests/hostlink/test_link.py
tests/hostlink/test_protocol.py
tests/hostlink/test_resolver.py
tests/hostlink/test_ros_assist.py
tests/networking/__init__.py
tests/networking/test_networking.py
tests/registry/test_ast_action_and_status_names.py
tests/resources/test_tracker_state_promotion.py
tests/test_action_policy.py
```

## Source-only paths to migrate

### Phase 01: Interface contract

```text
tests/contracts/test_workflow_revision_contract.py
tests/app/test_runtime_service_boundary.py
tests/app/test_unified_runtime_api.py
```

### Phase 02: authoring, canonical workflow, and registry

```text
tests/app/test_offline_os.py
tests/app/test_runtime_bind_security.py
tests/app/test_runtime_legacy_delegation.py
tests/app/test_runtime_profile_startup.py
tests/app/test_runtime_workflow_submission.py
tests/app/test_workflow_authoring_api.py
tests/app/test_workflow_to_dag.py
tests/devices/ptlc/__init__.py
tests/devices/ptlc/fixtures/multi_band_legacy_recipe.json
tests/devices/ptlc/fixtures/real_operations/02_develop/develop_execute.yaml
tests/devices/ptlc/fixtures/real_operations/02_develop/develop_prepare.yaml
tests/devices/ptlc/fixtures/real_operations/02_develop/develop_standby.yaml
tests/devices/ptlc/fixtures/real_operations/08_rail/rail_move_safe.yaml
tests/devices/ptlc/fixtures/real_operations/PROVENANCE.yaml
tests/devices/ptlc/fixtures/single_sample_golden.json
tests/devices/ptlc/test_existing_recipe_files.py
tests/devices/ptlc/test_legacy_recipe_full_runtime.py
tests/devices/ptlc/test_plc_protocol.py
tests/devices/ptlc/test_python_workflow_migration.py
tests/devices/ptlc/test_real_operation_files.py
tests/devices/ptlc/test_real_operation_runtime.py
tests/devices/ptlc/test_recipe_importer.py
tests/devices/ptlc/test_recipe_runtime.py
tests/devices/ptlc/test_resource_projection.py
tests/registry/test_action_contract.py
tests/registry/test_action_contract_compat.py
tests/registry/test_profile_v1.py
tests/registry/test_profile_v1_conformance.py
tests/registry/test_ptlc_profile_resource_holds.py
tests/test_imported_subworkflows.py
tests/workflow/authoring_test_support.py
tests/workflow/test_canonical_roundtrip.py
tests/workflow/test_canonical_schema.py
tests/workflow/test_python_projection_source_map.py
tests/workflow/test_python_roundtrip.py
tests/workflow/test_pythonic_bindings.py
tests/workflow/test_resource_holds.py
tests/workflow/test_workflow_parameters.py
```

### Phase 03: Node-centric scheduler

```text
tests/scheduler/__init__.py
tests/scheduler/fake_scheduler.py
tests/scheduler/test_control_flow.py
tests/scheduler/test_dag_executor.py
tests/scheduler/test_dag_invariants.py
tests/scheduler/test_python_fallback.py
tests/scheduler/test_ready_policy.py
tests/scheduler/test_resource_lock.py
tests/scheduler/test_runtime_bindings.py
tests/scheduler/test_task_dag_runner.py
```

### Phase 04: durable runtime

```text
tests/app/test_local_api.py
tests/app/test_runtime_production_composition.py
tests/app/test_schedule_ws.py
tests/app/test_ws_job_start_deadpaths.py
tests/runtime/test_canonical_runtime_contracts.py
tests/runtime/test_estimated_timeline.py
tests/runtime/test_event_journal.py
tests/runtime/test_message_processor_runtime_driver.py
tests/runtime/test_production_result_binding.py
tests/runtime/test_profile_loader.py
tests/runtime/test_reconcile_atomicity_contract.py
tests/runtime/test_reconcile_resume.py
tests/runtime/test_run_terminal_ownership.py
tests/runtime/test_runtime_composition.py
tests/runtime/test_runtime_contract_hardening.py
tests/runtime/test_runtime_orchestration.py
tests/runtime/test_runtime_profile_composition.py
tests/runtime/test_runtime_projection_integrity.py
tests/runtime/test_runtime_safety_regressions.py
tests/runtime/test_split_process_reconcile.py
```

### Phase 05: debugger

```text
tests/scheduler/test_debug_controller.py
```

### Phase 07: material projection

```text
tests/app/test_material_api.py
```

No source-only path may disappear from this ledger without an explicit
superseding decision and replacement Interface test.
