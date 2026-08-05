"""Restricted boolean-expression evaluator for LLM-generated screening
conditions.

Both `src/agents/llm_s_apply.py`'s `apply_rule` (production rule
application) and `src/agents/llm_s/tools.py`'s `TestComplexConditionTool`
(agent-time rule testing) call `evaluate_condition`, so a condition the
agent "tests" while exploring has identical semantics to the condition
that eventually runs in production — there is exactly one code path from
an LLM-generated condition string to a boolean result anywhere in this
plan.

Deliberately does not use Python's `eval()`/`exec()` on the LLM-produced
string: `ast.parse(condition, mode="eval")` builds a syntax tree instead,
which is walked and rejected outright (`ValueError`) if it contains any
node type, operator, or name outside a small allow-list. This is what
makes it safe to run an LLM's own output against real data without
risking arbitrary code execution from a string an LLM generated.
"""

from __future__ import annotations

import ast

ALLOWED_NAMES = {"mve", "bm", "mom12m"}

_COMPARE_OPS = {
    ast.Gt: lambda a, b: a > b,
    ast.Lt: lambda a, b: a < b,
    ast.GtE: lambda a, b: a >= b,
    ast.LtE: lambda a, b: a <= b,
}


def evaluate_condition(condition: str, values: dict[str, float]) -> bool:
    """Evaluate `condition` (a boolean expression over `ALLOWED_NAMES`
    using >, <, >=, <=, and, or, not, and numeric literals) against
    `values` (must contain every name the condition references). Raises
    `ValueError`, naming the offending token, for any syntax error,
    disallowed node type, disallowed operator, or name outside
    `ALLOWED_NAMES` — never silently ignores or partially evaluates a
    malformed condition.
    """
    try:
        tree = ast.parse(condition, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"invalid syntax in condition {condition!r}: {e}") from e
    return _eval_bool(tree.body, condition, values)


def _eval_bool(node: ast.AST, condition: str, values: dict[str, float]) -> bool:
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return all(_eval_bool(v, condition, values) for v in node.values)
        if isinstance(node.op, ast.Or):
            return any(_eval_bool(v, condition, values) for v in node.values)
        raise ValueError(f"disallowed boolean operator {type(node.op).__name__!r} in condition {condition!r}")

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _eval_bool(node.operand, condition, values)

    if isinstance(node, ast.Compare):
        left = _eval_num(node.left, condition, values)
        for op, comparator in zip(node.ops, node.comparators):
            op_type = type(op)
            if op_type not in _COMPARE_OPS:
                raise ValueError(f"disallowed comparison operator {op_type.__name__!r} in condition {condition!r}")
            right = _eval_num(comparator, condition, values)
            if not _COMPARE_OPS[op_type](left, right):
                return False
            left = right
        return True

    raise ValueError(f"disallowed syntax node {type(node).__name__!r} in condition {condition!r}")


def _eval_num(node: ast.AST, condition: str, values: dict[str, float]) -> float:
    if isinstance(node, ast.Name):
        if node.id not in ALLOWED_NAMES:
            raise ValueError(
                f"disallowed name {node.id!r} in condition {condition!r} (allowed: {sorted(ALLOWED_NAMES)})"
            )
        if node.id not in values:
            raise ValueError(f"condition {condition!r} references {node.id!r}, missing from the supplied values")
        return values[node.id]

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError(
                f"disallowed constant {node.value!r} in condition {condition!r} (only numeric literals are allowed)"
            )
        return node.value

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _eval_num(node.operand, condition, values)
        return -value if isinstance(node.op, ast.USub) else value

    raise ValueError(f"disallowed syntax node {type(node).__name__!r} in condition {condition!r}")
