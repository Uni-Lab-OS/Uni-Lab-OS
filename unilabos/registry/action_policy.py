"""@action 的异常处理策略声明式定义

用于配合 @action(error_policy=...) 参数,让 driver 作者以 TypedDict 形式声明
出错后允许用户选择哪些操作,以及 Edge 侧应如何处理。

与 unilabos.utils.exception 的 UserAction 关系:
    UserAction 是运行时对象,可以在异常构造时按具体上下文动态生成。
    ErrorPolicyOption 是声明式配置,在装饰器扫描阶段固化到 registry。
    二者最终在 decision loop 里合并 (优先级: 实例 suggested_actions
    > error_policy.options > 类默认 suggested_actions > 框架 fallback)。
"""
from typing import Any, Callable, Dict, List, Literal, Optional, TypedDict

try:
    from typing import NotRequired  # Python 3.11+
except ImportError:  # pragma: no cover
    from typing_extensions import NotRequired  # type: ignore


class ErrorPolicyOption(TypedDict, total=False):
    """一个可展示给用户的错误处理选项

    action:  回传给 Edge 的稳定 key,用户点击后 Edge 通过这个值决定分支
    label:   前端展示名 (如 "重试" / "关机")
    description:      前端 tooltip
    fallback_action:  用户选此项后 Edge 要调用的 driver 上函数名
    handler:  运行时可调用对象;若同时给了 handler 和 fallback_action,handler 优先
    then:    handler/fallback 执行完后的处理方式
             - "retry":    继续循环重新执行原 action (默认)
             - "continue": 直接把 handler/fallback 的返回值作为整个 action 的结果
             - "skip":     标记跳过,不再重试
    extra:   任意附加字段
    """

    action: str
    label: str
    description: NotRequired[str]
    fallback_action: NotRequired[str]
    handler: NotRequired[Callable[..., Any]]
    then: NotRequired[Literal["retry", "continue", "skip"]]
    extra: NotRequired[Dict[str, Any]]


class ErrorPolicy(TypedDict, total=False):
    """一个 @action 的异常处理策略

    enabled:      是否启用交互式决策,默认 True
    allow_retry:  是否允许用户选择重试,默认 True
    allow_skip:   是否允许用户选择跳过,默认 True
    severity:     报警等级
    category:     错误分类,仅当 driver 抛出的异常不是 DeviceException 时用作默认值
    options:      自定义额外选项 (retry/skip 由 allow_retry/allow_skip 单独控制)

    max_retries:  最多允许多少轮循环 (含 retry 与 handler 抛错触发的新一轮),
                  超过则强制以 DeviceException 终止,默认 10
    decision_timeout_seconds:  等待用户决策的超时秒数,默认 300
    default_on_decision_timeout: 等待超时后的默认动作,默认 "abort"
    sync_uncancellable:  同步 action 超时后是否放弃取消线程,直接抛出并接入决策
                         流程 (线程仍在后台运行,由 driver 侧保证幂等)。默认 False。
    """

    enabled: bool
    allow_retry: bool
    allow_skip: bool
    severity: Literal["warning", "error", "critical"]
    category: Literal["network", "hardware", "timeout", "parameter", "resource", "unknown"]
    options: List[ErrorPolicyOption]
    max_retries: int
    decision_timeout_seconds: float
    default_on_decision_timeout: Literal["abort", "retry", "skip"]
    sync_uncancellable: bool


# 默认策略常量,供 registry / decision loop 复用
DEFAULT_MAX_RETRIES: int = 10
DEFAULT_DECISION_TIMEOUT_SECONDS: float = 300.0
DEFAULT_ON_DECISION_TIMEOUT: str = "abort"


def normalize_error_policy(policy: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """将 error_policy 归一化为可序列化字典 (剔除 handler 等 callable 字段)

    - handler 是运行时 callable,不能进 AST cache / YAML,序列化时剔除
    - fallback_action / then / extra 保留
    - 其他字段原样透传
    """
    if not policy:
        return None
    result: Dict[str, Any] = {}
    for k, v in policy.items():
        if k == "options" and isinstance(v, list):
            options: List[Dict[str, Any]] = []
            for opt in v:
                if not isinstance(opt, dict):
                    continue
                opt_out = {ok: ov for ok, ov in opt.items() if ok != "handler"}
                options.append(opt_out)
            result["options"] = options
        else:
            result[k] = v
    return result


def collect_option_handlers(policy: Optional[Dict[str, Any]]) -> Dict[str, Callable[..., Any]]:
    """从 error_policy.options 中抽取 action -> handler 映射,供运行时使用"""
    if not policy:
        return {}
    handlers: Dict[str, Callable[..., Any]] = {}
    for opt in policy.get("options", []) or []:
        if not isinstance(opt, dict):
            continue
        handler = opt.get("handler")
        action_key = opt.get("action")
        if callable(handler) and isinstance(action_key, str):
            handlers[action_key] = handler
    return handlers
