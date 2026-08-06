"""工作流发布后的目录依赖创作刷新深模块（Deep Module）。"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping


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


__all__ = ["refresh_catalog_dependent_authoring"]
