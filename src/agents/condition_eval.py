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


def evaluate_condition(condition: str, values: dict[str, float], missing_ok: bool = False) -> bool:
    """Evaluate `condition` (a boolean expression over `ALLOWED_NAMES`
    using >, <, >=, <=, and, or, not, and numeric literals) against
    `values`. Raises `ValueError`, naming the offending token, for any
    syntax error, disallowed node type, disallowed operator, or name
    outside `ALLOWED_NAMES`, in both modes below — this never softens a
    malformed or unsafe condition string, only ordinary missing data.

    If `missing_ok` is False (the default, and the only behavior before
    this parameter existed), `values` must contain every name the
    condition references — a missing one raises `ValueError` immediately,
    even mid-expression.

    If `missing_ok` is True, a name the condition references but that is
    absent from `values` no longer raises: any comparison that would need
    it becomes indeterminate ("unknown"), and unknown propagates through
    `and`/`or`/`not` using the same three-valued logic SQL uses for `NULL`
    in a `WHERE` clause — a `False` anywhere in an `and` still makes the
    whole `and` `False` regardless of other unknowns, a `True` anywhere in
    an `or` still makes the whole `or` `True`, and only when nothing else
    decides it does unknown surface, at which point this function coerces
    it to `False` before returning (the return type stays `bool`, never a
    third value). This means one clause needing an unavailable factor no
    longer blocks the rest of a larger `and`/`or` expression from being
    decided by its other, available clauses — see
    plans/07_external_candidate_screening.md's Decision Log for why: an
    ETF with no reliable proxy for one factor (e.g. `bm` for a
    preferred-stock fund) should still resolve to a real signal from its
    other factors, not be blocked entirely by the one it lacks.
    """
    try:
        tree = ast.parse(condition, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"invalid syntax in condition {condition!r}: {e}") from e
    result = _eval_bool(tree.body, condition, values, missing_ok)
    return False if result is None else result


def _eval_bool(node: ast.AST, condition: str, values: dict[str, float], missing_ok: bool) -> bool | None:
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            saw_unknown = False
            for v in node.values:
                result = _eval_bool(v, condition, values, missing_ok)
                if result is False:
                    return False
                if result is None:
                    saw_unknown = True
            return None if saw_unknown else True
        if isinstance(node.op, ast.Or):
            saw_unknown = False
            for v in node.values:
                result = _eval_bool(v, condition, values, missing_ok)
                if result is True:
                    return True
                if result is None:
                    saw_unknown = True
            return None if saw_unknown else False
        raise ValueError(f"disallowed boolean operator {type(node.op).__name__!r} in condition {condition!r}")

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        inner = _eval_bool(node.operand, condition, values, missing_ok)
        return None if inner is None else not inner

    if isinstance(node, ast.Compare):
        left = _eval_num(node.left, condition, values, missing_ok)
        indeterminate = left is None
        for op, comparator in zip(node.ops, node.comparators):
            op_type = type(op)
            if op_type not in _COMPARE_OPS:
                raise ValueError(f"disallowed comparison operator {op_type.__name__!r} in condition {condition!r}")
            right = _eval_num(comparator, condition, values, missing_ok)
            if left is not None and right is not None:
                if not _COMPARE_OPS[op_type](left, right):
                    return False
            else:
                indeterminate = True
            left = right
        return None if indeterminate else True

    raise ValueError(f"disallowed syntax node {type(node).__name__!r} in condition {condition!r}")


def _eval_num(node: ast.AST, condition: str, values: dict[str, float], missing_ok: bool) -> float | None:
    if isinstance(node, ast.Name):
        if node.id not in ALLOWED_NAMES:
            raise ValueError(
                f"disallowed name {node.id!r} in condition {condition!r} (allowed: {sorted(ALLOWED_NAMES)})"
            )
        if node.id not in values:
            if missing_ok:
                return None
            raise ValueError(f"condition {condition!r} references {node.id!r}, missing from the supplied values")
        return values[node.id]

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError(
                f"disallowed constant {node.value!r} in condition {condition!r} (only numeric literals are allowed)"
            )
        return node.value

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _eval_num(node.operand, condition, values, missing_ok)
        if value is None:
            return None
        return -value if isinstance(node.op, ast.USub) else value

    raise ValueError(f"disallowed syntax node {type(node).__name__!r} in condition {condition!r}")
