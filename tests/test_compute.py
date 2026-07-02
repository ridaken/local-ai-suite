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
