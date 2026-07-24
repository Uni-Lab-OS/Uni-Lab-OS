"""Safe evaluator for data-only workflow expressions."""

from __future__ import annotations

import operator
from collections.abc import Callable, Mapping
from typing import Any


class StructuredExpressionError(ValueError):
    """A structured workflow expression is malformed or unsupported."""


_BINARY: dict[str, Callable[[Any, Any], Any]] = {
    "+": operator.add,
    "-": operator.sub,
    "*": operator.mul,
    "/": operator.truediv,
    "//": operator.floordiv,
    "%": operator.mod,
    "==": operator.eq,
    "!=": operator.ne,
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
}

_CALLS: dict[str, Callable[..., Any]] = {
    "len": len,
    "min": min,
    "max": max,
    "abs": abs,
    "round": round,
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
    "contains": lambda container, item: item in container,
    "get": lambda container, key: (
        container.get(key) if isinstance(container, Mapping) else None
    ),
}


def evaluate_expression(node: Any, *, read: Callable[[str], Any]) -> Any:
    """Evaluate the closed expression AST without executing source text."""

    if not isinstance(node, Mapping):
        raise StructuredExpressionError("expression must be an object")
    if "lit" in node:
        return node["lit"]
    if "var" in node:
        name = node["var"]
        if not isinstance(name, str) or not name:
            raise StructuredExpressionError("variable name must be non-empty")
        return read(name)
    if "binop" in node:
        op = node["binop"]
        if op == "and":
            left = evaluate_expression(node.get("left"), read=read)
            return evaluate_expression(node.get("right"), read=read) if left else left
        if op == "or":
            left = evaluate_expression(node.get("left"), read=read)
            return left if left else evaluate_expression(node.get("right"), read=read)
        function = _BINARY.get(str(op))
        if function is None:
            raise StructuredExpressionError(f"unsupported binary operator: {op}")
        return function(
            evaluate_expression(node.get("left"), read=read),
            evaluate_expression(node.get("right"), read=read),
        )
    if "unop" in node:
        value = evaluate_expression(node.get("operand"), read=read)
        if node["unop"] == "not":
            return not value
        if node["unop"] == "neg":
            return -value
        raise StructuredExpressionError(
            f"unsupported unary operator: {node['unop']}"
        )
    if "call" in node:
        name = str(node["call"])
        function = _CALLS.get(name)
        if function is None:
            raise StructuredExpressionError(f"function is not allowed: {name}")
        args = [
            evaluate_expression(argument, read=read)
            for argument in node.get("args", [])
        ]
        return function(*args)
    if "index" in node:
        container = evaluate_expression(node["index"], read=read)
        key = evaluate_expression(node.get("key"), read=read)
        return container[key]
    if "field" in node:
        container = evaluate_expression(node["field"], read=read)
        if not isinstance(container, Mapping):
            raise StructuredExpressionError("field target must be an object")
        return container[node.get("name")]
    raise StructuredExpressionError("unrecognized structured expression")
