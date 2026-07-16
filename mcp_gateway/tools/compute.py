"""Bounded arithmetic evaluation over a small, whitelisted AST."""

from __future__ import annotations

import ast
import asyncio
import math
import operator

_MAX_EXPRESSION_CHARS = 4096
_MAX_AST_NODES = 128
_MAX_AST_DEPTH = 32
_MAX_RESULT_BITS = 32768
_MAX_OUTPUT_CHARS = 16384
_MAX_FACTORIAL_N = 2000
_MAX_COMBINATORIC_N = 10000
_CALC_TIMEOUT_SECONDS = 0.5

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
_NAMES.update(
    {
        "pi": math.pi,
        "e": math.e,
        "tau": math.tau,
        "inf": math.inf,
        "abs": abs,
        "round": round,
        "min": min,
        "max": max,
    }
)

_ARITY: dict[str, tuple[int, int]] = {
    "atan2": (2, 2),
    "copysign": (2, 2),
    "fmod": (2, 2),
    "pow": (2, 2),
    "dist": (2, 2),
    "comb": (2, 2),
    "perm": (1, 2),
    "log": (1, 2),
    "round": (1, 2),
    "hypot": (1, 16),
    "gcd": (1, 16),
    "min": (1, 16),
    "max": (1, 16),
}


class _CalcError(ValueError):
    pass


def _node_depth(node: ast.AST) -> int:
    children = list(ast.iter_child_nodes(node))
    return 1 + (max((_node_depth(child) for child in children), default=0))


def _bounded(value):  # noqa: ANN001, ANN202
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _CalcError("result is not a real number")
    if isinstance(value, int) and value.bit_length() > _MAX_RESULT_BITS:
        raise _CalcError("result is too large")
    return value


def _validate_pow(left, right) -> None:  # noqa: ANN001
    if not isinstance(right, (int, float)) or not math.isfinite(float(right)):
        raise _CalcError("invalid exponent")
    if isinstance(right, float) and not right.is_integer():
        return
    exponent = int(right)
    if exponent <= 0 or abs(left) in (0, 1):
        return
    if isinstance(left, int):
        estimated_bits = max(1, left.bit_length()) * exponent
    else:
        estimated_bits = math.log2(abs(float(left))) * exponent if left else 0
    if estimated_bits > _MAX_RESULT_BITS:
        raise _CalcError("power result is too large")


def _validate_function(name: str, args: list[object]) -> None:
    minimum, maximum = _ARITY.get(name, (1, 1))
    if not minimum <= len(args) <= maximum:
        expected = str(minimum) if minimum == maximum else f"{minimum}..{maximum}"
        raise _CalcError(f"{name} expects {expected} argument(s)")
    if name == "factorial":
        value = args[0]
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 0 <= value <= _MAX_FACTORIAL_N
        ):
            raise _CalcError(f"factorial operand must be an integer in 0..{_MAX_FACTORIAL_N}")
    if name in {"comb", "perm"}:
        if any(not isinstance(value, int) or isinstance(value, bool) for value in args):
            raise _CalcError(f"{name} operands must be integers")
        n = args[0]
        if not 0 <= n <= _MAX_COMBINATORIC_N:
            raise _CalcError(f"{name} first operand must be in 0..{_MAX_COMBINATORIC_N}")
        if len(args) == 2 and not 0 <= args[1] <= n:
            raise _CalcError(f"{name} second operand must be in 0..n")
        if name == "perm":
            count = args[1] if len(args) == 2 else n
            if count * max(1.0, math.log2(max(2, n))) > _MAX_RESULT_BITS:
                raise _CalcError("permutation result is too large")
    if name == "pow":
        _validate_pow(args[0], args[1])


def _eval(node: ast.AST):  # noqa: ANN202
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise _CalcError(f"unsupported literal: {node.value!r}")
        return _bounded(node.value)
    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise _CalcError(f"unsupported operator: {type(node.op).__name__}")
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow):
            _validate_pow(left, right)
        return _bounded(op(left, right))
    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise _CalcError(f"unsupported unary operator: {type(node.op).__name__}")
        return _bounded(op(_eval(node.operand)))
    if isinstance(node, ast.Name):
        if node.id in _NAMES and not callable(_NAMES[node.id]):
            return _bounded(_NAMES[node.id])
        raise _CalcError(f"unknown name: {node.id}")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise _CalcError("only direct function calls are allowed")
        name = node.func.id
        fn = _NAMES.get(name)
        if fn is None or not callable(fn):
            raise _CalcError(f"unknown function: {name}")
        if node.keywords:
            raise _CalcError("keyword arguments are not supported")
        args = [_eval(arg) for arg in node.args]
        _validate_function(name, args)
        return _bounded(fn(*args))
    raise _CalcError(f"unsupported expression: {type(node).__name__}")


def _calculate_sync(expression: str) -> str:
    if not isinstance(expression, str) or not expression.strip():
        raise _CalcError("expression must not be blank")
    if len(expression) > _MAX_EXPRESSION_CHARS:
        raise _CalcError(f"expression exceeds {_MAX_EXPRESSION_CHARS} characters")
    tree = ast.parse(expression, mode="eval")
    if sum(1 for _node in ast.walk(tree)) > _MAX_AST_NODES:
        raise _CalcError(f"expression exceeds {_MAX_AST_NODES} AST nodes")
    if _node_depth(tree) > _MAX_AST_DEPTH:
        raise _CalcError(f"expression nesting exceeds {_MAX_AST_DEPTH}")
    value = _eval(tree.body)
    try:
        rendered = str(value)
    except (ValueError, OverflowError) as exc:
        raise _CalcError("result could not be formatted") from exc
    output = f"{expression} = {rendered}"
    if len(output) > _MAX_OUTPUT_CHARS:
        raise _CalcError("formatted result is too large")
    return output


async def calculate(expression: str) -> str:
    """Evaluate a bounded math expression and return the result."""
    try:
        async with asyncio.timeout(_CALC_TIMEOUT_SECONDS):
            return await asyncio.to_thread(_calculate_sync, expression)
    except TimeoutError:
        return "calculate error: expression exceeded the execution-time limit"
    except _CalcError as exc:
        return f"calculate error: {exc}"
    except SyntaxError:
        return "calculate error: could not parse the expression."
    except (ValueError, TypeError, OverflowError, ZeroDivisionError) as exc:
        return f"calculate error: {exc}"
    except Exception as exc:  # noqa: BLE001 - never leak a tool exception to the transport
        return f"calculate error: evaluation failed ({type(exc).__name__})"
