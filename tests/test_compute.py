"""Tests for the safe calculate() evaluator — the one fully offline, deterministic
tool, so it's the natural CI gate. Network-backed tools are covered by the manual
smoke test (scripts/smoke_test.py)."""

import asyncio

from mcp_gateway.tools.compute import calculate


def run(expr: str) -> str:
    return asyncio.run(calculate(expr))


def test_basic_arithmetic():
    assert run("2 + 3 * 4") == "2 + 3 * 4 = 14"


def test_math_functions():
    assert run("sqrt(16)") == "sqrt(16) = 4.0"
    assert run("factorial(5)") == "factorial(5) = 120"


def test_constants():
    assert "pi = 3.14159" in run("pi")


def test_division_by_zero_is_handled():
    assert "error" in run("1/0").lower()


def test_rejects_code_injection():
    assert "error" in run('__import__("os").system("echo hi")').lower()
    assert "error" in run('open("secret.txt")').lower()


def test_rejects_unknown_names():
    assert "error" in run("foo + 1").lower()


def test_syntax_error_is_handled():
    assert "error" in run("1 +").lower()


def test_wrong_arity_is_handled():
    assert "error" in run("sqrt(1, 2)").lower()


def test_rejects_oversized_expression_and_ast():
    assert "4096" in run("1" * 4097)
    assert "128 AST nodes" in run("+".join("1" for _ in range(70)))


def test_rejects_expensive_factorial_and_power_without_formatting_crash():
    assert "factorial operand" in run("factorial(10000)")
    assert "too large" in run("10**100000")


def test_bounds_combinatoric_operands_and_output_growth():
    assert "first operand" in run("comb(10001, 2)")
    assert "too large" in run("(10**6000) * (10**6000)")
