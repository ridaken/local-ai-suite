"""Phase 1 smoke test — exercise each gateway tool directly (no MCP client).

Run from the repo root with the venv:
    .venv/Scripts/python.exe scripts/smoke_test.py

calculate always runs offline. kb_search needs kiwix-serve up with a ZIM loaded;
web_search needs KAGI_API_KEY; pubmed/arxiv need internet. Failures are printed,
not fatal, so you can see exactly which pieces are wired.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_gateway.tools.arxiv import arxiv_search  # noqa: E402
from mcp_gateway.tools.compute import calculate  # noqa: E402
from mcp_gateway.tools.kb_search import kb_search  # noqa: E402
from mcp_gateway.tools.pubmed import pubmed_search  # noqa: E402
from mcp_gateway.tools.web_search import web_search  # noqa: E402


async def main() -> None:
    checks = [
        ("calculate", calculate("sqrt(2) * 3 + 10")),
        ("kb_search", kb_search("albert einstein", 2)),
        ("web_search", web_search("python release notes", 2)),
        ("pubmed_search", pubmed_search("crispr", 2)),
        ("arxiv_search", arxiv_search("transformers", 2)),
    ]
    for name, coro in checks:
        print(f"\n===== {name} =====")
        try:
            print(await coro)
        except Exception as exc:  # noqa: BLE001 - smoke test wants to see everything
            print(f"{name} raised {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
