"""工作区工作流源码（Workflow Source）固定点激活协调测试。"""

from unilabos.workflow.workspace_activation import WorkspaceActivationCoordinator


class PublicError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def test_activation_reaches_fixed_point_after_catalog_change():
    """应用子工作流后，父来源必须在后续目录代际继续推进。"""

    candidates = {"child": "child-hash", "parent": None}
    applied = []

    def apply_candidate(workflow_uuid, candidate_hash):
        applied.append((workflow_uuid, candidate_hash))
        candidates[workflow_uuid] = None
        if workflow_uuid == "child":
            candidates["parent"] = "parent-hash"
        return {"apply_result": {"warnings": []}}

    coordinator = WorkspaceActivationCoordinator(
        recover_sources=lambda: None,
        list_registrations=lambda: [
            {"workflow_uuid": "parent"},
            {"workflow_uuid": "child"},
        ],
        reconcile_source=lambda _workflow_uuid: None,
        load_authoring_record=lambda workflow_uuid: {
            "candidate": (
                {"candidate_hash": candidates[workflow_uuid]}
                if candidates[workflow_uuid]
                else None
            )
        },
        apply_candidate=apply_candidate,
        require_apply_complete=lambda _result: None,
        record_isolated_failure=lambda _workflow_uuid, _error: None,
        public_error_type=PublicError,
        public_error_code=lambda error: error.code,
        isolated_error_codes={"candidate_invalid"},
        error_factory=PublicError,
    )

    coordinator.activate_to_fixed_point()

    assert applied == [("child", "child-hash"), ("parent", "parent-hash")]


def test_activation_isolates_invalid_candidate():
    """单个无效候选成为来源诊断，不阻断其余来源。"""

    failures = []
    coordinator = WorkspaceActivationCoordinator(
        recover_sources=lambda: None,
        list_registrations=lambda: [{"workflow_uuid": "invalid"}],
        reconcile_source=lambda _workflow_uuid: None,
        load_authoring_record=lambda _workflow_uuid: {
            "candidate": {"candidate_hash": "hash"}
        },
        apply_candidate=lambda _workflow_uuid, _candidate_hash: (_ for _ in ()).throw(
            PublicError("candidate_invalid")
        ),
        require_apply_complete=lambda _result: None,
        record_isolated_failure=lambda workflow_uuid, error: failures.append(
            (workflow_uuid, error.code)
        ),
        public_error_type=PublicError,
        public_error_code=lambda error: error.code,
        isolated_error_codes={"candidate_invalid"},
        error_factory=PublicError,
    )

    coordinator.activate_to_fixed_point()

    assert failures == [("invalid", "candidate_invalid")]


def test_activation_does_not_recompile_or_reapply_completed_source():
    """同一次冷启动中已成功应用的来源不得在下一轮再次产生空变更候选。"""

    candidates = {"leaf": "leaf-hash"}
    reconciled = []
    applied = []

    def reconcile_source(workflow_uuid):
        reconciled.append(workflow_uuid)
        if len(reconciled) > 1:
            raise AssertionError("已完成来源被固定点循环重复编译")
        candidates[workflow_uuid] = "leaf-hash"

    def apply_candidate(workflow_uuid, candidate_hash):
        applied.append((workflow_uuid, candidate_hash))
        candidates[workflow_uuid] = None
        return {"apply_result": {"warnings": []}}

    coordinator = WorkspaceActivationCoordinator(
        recover_sources=lambda: None,
        list_registrations=lambda: [{"workflow_uuid": "leaf"}],
        reconcile_source=reconcile_source,
        load_authoring_record=lambda workflow_uuid: {
            "candidate": (
                {"candidate_hash": candidates[workflow_uuid]}
                if candidates[workflow_uuid]
                else None
            )
        },
        apply_candidate=apply_candidate,
        require_apply_complete=lambda _result: None,
        record_isolated_failure=lambda _workflow_uuid, _error: None,
        public_error_type=PublicError,
        public_error_code=lambda error: error.code,
        isolated_error_codes={"candidate_invalid"},
        error_factory=PublicError,
    )

    coordinator.activate_to_fixed_point()

    assert reconciled == []
    assert applied == [("leaf", "leaf-hash")]
