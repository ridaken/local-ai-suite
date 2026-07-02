"""calculate — safe arithmetic / math evaluation.

A restricted AST evaluator: numeric literals, the usual operators, and whitelisted
functions/constants from the math module. No names, attributes, calls to anything
else, comprehensions, or lambdas — so there is no path to arbitrary code. (A
fuller sandboxed python_exec with sympy/numpy is a later phase.)
"""

from __future__ import annotations

import ast
import math
import operator

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}

# Whitelisted math functions and constants only.
_NAMES: dict[str, object] = {
    k: getattr(math, k)
    for k in (
        "sqrt", "cbrt", "exp", "log", "log2", "log10", "pow",
        "sin", "cos", "tan", "asin", "acos", "atan", "atan2",
        "sinh", "cosh", "tanh", "degrees", "radians", "hypot",
        "floor", "ceil", "trunc", "factorial", "gcd", "fabs",
        "comb", "perm", "isqrt", "copysign", "fmod", "dist",
    )
    if hasattr(math, k)
}
_NAMES.update({"pi": math.pi, "e": math.e, "tau": math.tau, "inf": math.inf, "abs": abs, "round": round, "min": min, "max": max})

_MAX_POW = 1_000_000  # guard against giant exponents locking the CPU


class _CalcError(ValueError):
    pass


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise _CalcError(f"unsupported literal: {node.value!r}")
    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise _CalcError(f"unsupported operator: {type(node.op).__name__}")
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > _MAX_POW:
            raise _CalcError("exponent too large")
        return op(left, right)
    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise _CalcError(f"unsupported unary operator: {type(node.op).__name__}")
        return op(_eval(node.operand))
    if isinstance(node, ast.Name):
        if node.id in _NAMES and not callable(_NAMES[node.id]):
            return _NAMES[node.id]
        raise _CalcError(f"unknown name: {node.id}")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise _CalcError("only direct function calls are allowed")
        fn = _NAMES.get(node.func.id)
        if fn is None or not callable(fn):
            raise _CalcError(f"unknown function: {getattr(node.func, 'id', '?')}")
        if node.keywords:
            raise _CalcError("keyword arguments are not supported")
        return fn(*[_eval(a) for a in node.args])
    raise _CalcError(f"unsupported expression: {type(node).__name__}")


async def calculate(expression: str) -> str:
    """Evaluate a math expression and return the result."""
    try:
        tree = ast.parse(expression, mode="eval")
        value = _eval(tree.body)
    except _CalcError as exc:
        return f"calculate error: {exc}"
    except SyntaxError:
        return "calculate error: could not parse the expression."
    except (ValueError, OverflowError, ZeroDivisionError) as exc:
        return f"calculate error: {exc}"
    return f"{expression} = {value}"
