"""工作流发布后的目录依赖创作刷新深模块（Deep Module）。"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping

_RETRYABLE_CATALOG_DIAGNOSTICS = frozenset(
    {
        "composite_child_not_found",
        "composite_child_unapplied",
    }
)


def recover_catalog_dependent_authoring_fixed_point(
    *,
    registrations: Iterable[Mapping[str, object]],
    reconcile_source: Callable[[str], object],
    load_authoring_record: Callable[[str], Mapping[str, object]],
) -> None:
    """把冷启动来源编译推进到组合工作流依赖固定点。

    参数：``registrations`` 是当前活动软件包目录（Package Catalog）代际的完整
    工作流源码（Workflow Source）登记；``reconcile_source`` 强制按当前模板目录
    编译一个来源；``load_authoring_record`` 读取本次编译提交后的创作记录。
    返回：无；首轮编译全部来源，后续只重试仍因子工作流尚未进入当前目录而失败
    的来源，直到没有待重试项或连续两轮待处理事实完全相同。
    异常：回调异常原样传播，由工作流服务（WorkflowService）维持既有单项故障
    隔离语义；循环最多执行“来源数 + 1”轮，不会因永久缺失子来源而无界重试。
    """

    # ``workflow_uuids`` 保持持久注册顺序；首轮必须覆盖全部活动来源，不能只从
    # 已有诊断反推，因为全新数据库尚未产生任何创作投影。
    workflow_uuids = tuple(
        str(registration["workflow_uuid"])
        for registration in registrations
    )
    pending_workflow_uuids = workflow_uuids
    # ``previous_pending_facts`` 是上一轮仍未解析的身份及诊断集合，用于识别永久
    # 缺失，而非通过固定次数盲目重复同一确定性失败。
    previous_pending_facts: tuple[tuple[str, tuple[str, ...]], ...] | None = None
    for _pass_index in range(len(workflow_uuids) + 1):
        if not pending_workflow_uuids:
            return
        current_pending_facts: list[tuple[str, tuple[str, ...]]] = []
        for workflow_uuid in pending_workflow_uuids:
            reconcile_source(workflow_uuid)
            # ``authoring_record`` 是编译事务提交后的唯一诊断事实，不从异常消息或
            # 测试编译器内部状态猜测依赖是否已经收敛。
            authoring_record = load_authoring_record(workflow_uuid)
            retryable_codes = _retryable_catalog_diagnostic_codes(authoring_record)
            if retryable_codes:
                current_pending_facts.append((workflow_uuid, retryable_codes))
        frozen_pending_facts = tuple(current_pending_facts)
        if not frozen_pending_facts or frozen_pending_facts == previous_pending_facts:
            return
        previous_pending_facts = frozen_pending_facts
        pending_workflow_uuids = tuple(
            workflow_uuid
            for workflow_uuid, _diagnostic_codes in frozen_pending_facts
        )


def _retryable_catalog_diagnostic_codes(
    authoring_record: Mapping[str, object],
) -> tuple[str, ...]:
    """读取仍可能随子工作流目录就绪而消失的诊断集合。

    参数：``authoring_record`` 是工作流创作（Authoring）持久记录。
    返回：排序去重后的可重试目录诊断码；记录没有相关错误时返回空元组。
    异常：无；畸形诊断视为不可自动重试，继续由原创作投影公开。
    """

    diagnostics = authoring_record.get("diagnostics")
    if not isinstance(diagnostics, list):
        return ()
    # ``retryable_codes`` 只收录闭合集合内的错误码；语法或绑定错误不能因启动
    # 固定点机制被无意义地重复编译。
    retryable_codes = {
        str(diagnostic.get("code"))
        for diagnostic in diagnostics
        if isinstance(diagnostic, Mapping)
        and str(diagnostic.get("severity", "")).lower() == "error"
        and diagnostic.get("code") in _RETRYABLE_CATALOG_DIAGNOSTICS
    }
    return tuple(sorted(retryable_codes))


def refresh_catalog_dependent_authoring(
    *,
    registrations: Iterable[Mapping[str, object]],
    load_authoring_record: Callable[[str], Mapping[str, object]],
    reconcile_source: Callable[[str], object],
    warnings: list[dict[str, str]],
    mutated_workflow_uuid: str,
) -> None:
    """刷新可能依赖刚发布目录合同的工作流创作派生状态。

    参数：``registrations`` 是同一活动软件包目录（Package Catalog）代际的来源
    登记；``load_authoring_record`` 按工作流 UUID 读取创作记录；
    ``reconcile_source`` 强制重编译一个活动工作流源码（Workflow Source）；
    ``warnings`` 收集主应用提交后不可回滚的刷新警告；
    ``mutated_workflow_uuid`` 是刚发布的工作流（Workflow）身份。
    返回：无；只刷新具有候选版本（Candidate）或诊断的其他活动来源。
    异常：单项读取或重编译异常会被隔离并转成警告，绝不把已提交发布伪装成
    失败。
    """

    for registration in registrations:
        # ``dependent_workflow_uuid`` 是可能引用新发布合同的活动工作流身份。
        dependent_workflow_uuid = str(registration["workflow_uuid"])
        if dependent_workflow_uuid == mutated_workflow_uuid:
            continue
        try:
            # ``authoring_record`` 中候选和诊断会随模板目录变化而失效。
            authoring_record = load_authoring_record(dependent_workflow_uuid)
            if (
                authoring_record.get("candidate") is None
                and not authoring_record.get("diagnostics")
            ):
                continue
            reconcile_source(dependent_workflow_uuid)
        except Exception:  # noqa: BLE001 - 主应用已提交，只能隔离派生刷新故障。
            warnings.append(
                {
                    "code": "dependent_authoring_refresh_pending",
                    "message": "工作流已应用，但依赖创作草稿仍待重新编译",
                }
            )


__all__ = [
    "recover_catalog_dependent_authoring_fixed_point",
    "refresh_catalog_dependent_authoring",
]
