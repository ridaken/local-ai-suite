"""Tool implementations for the MCP gateway.

Each module exposes plain async helper functions with the real logic (HTTP
calls, parsing, formatting). server.py wraps them in thin @mcp.tool handlers so
the MCP schema stays clean and the logic stays testable in isolation.
"""
